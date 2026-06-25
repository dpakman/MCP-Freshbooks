"""FreshBooks OAuth2 authentication flow with token persistence."""

import json
import ssl
import tempfile
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

AUTHORIZE_URL = "https://auth.freshbooks.com/oauth/authorize"
TOKEN_URL = "https://api.freshbooks.com/auth/oauth/token"
IDENTITY_URL = "https://api.freshbooks.com/auth/api/v1/users/me"

TOKEN_DIR = Path.home() / ".mcp-freshbooks"
TOKEN_FILE = TOKEN_DIR / "tokens.json"


def get_config() -> dict:
    """Load OAuth config from environment."""
    import os
    client_id = os.environ.get("FRESHBOOKS_CLIENT_ID", "")
    client_secret = os.environ.get("FRESHBOOKS_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("FRESHBOOKS_REDIRECT_URI", "https://localhost:8555/callback")  # Override for hosted: https://api.guardiacontent.com/freshbooks/callback
    if not client_id or not client_secret:
        raise ValueError(
            "FRESHBOOKS_CLIENT_ID and FRESHBOOKS_CLIENT_SECRET must be set. "
            "Get credentials at https://my.freshbooks.com/#/developer"
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


def get_auth_url(config: dict) -> str:
    """Build the OAuth2 authorization URL.

    Requests the full set of scopes the package's tool surface needs.
    Omitting `scope` here causes FreshBooks to issue a token with only
    `user:profile:read`, which makes every non-identity tool return 403.
    """
    scopes = " ".join([
        "user:profile:read",
        "user:clients:read", "user:clients:write",
        "user:invoices:read", "user:invoices:write",
        "user:expenses:read", "user:expenses:write",
        "user:payments:read", "user:payments:write",
        "user:projects:read", "user:projects:write",
        "user:reports:read",
        "user:taxes:read",
        "user:time_entries:read", "user:time_entries:write",
        "user:estimates:read", "user:estimates:write",
        "user:billable_items:read",
        "user:uploads:read", "user:uploads:write",
        # Accounting / reconciliation (added v0.3.0, scopes corrected v0.3.1 against live
        # insufficient_scope errors). Requesting these requires a re-authentication;
        # existing tokens keep working for everything else.
        "user:account:read",  # ledger_accounts + general_ledger / trial_balance / account_entry_details reports
        "user:journal_entries:read", "user:journal_entries:write",
        "user:bills:read", "user:bills:write",
        "user:bill_payments:read", "user:bill_payments:write",
        "user:bill_vendors:read", "user:bill_vendors:write",
        "user:credit_notes:read", "user:credit_notes:write",
        "user:other_income:read", "user:other_income:write",
    ])
    params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "redirect_uri": config["redirect_uri"],
        "scope": scopes,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(config: dict, code: str) -> dict:
    """Exchange authorization code for tokens."""
    resp = httpx.post(TOKEN_URL, json={
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "authorization_code",
        "redirect_uri": config["redirect_uri"],
        "code": code,
    })
    resp.raise_for_status()
    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data.get("created_at", int(time.time())) + data.get("expires_in", 43200),
    }
    _save_tokens(tokens)
    return tokens


def refresh_tokens(config: dict, refresh_token: str) -> dict:
    """Refresh an expired access token."""
    resp = httpx.post(TOKEN_URL, json={
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "grant_type": "refresh_token",
        "redirect_uri": config["redirect_uri"],
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data.get("created_at", int(time.time())) + data.get("expires_in", 43200),
    }
    _save_tokens(tokens)
    return tokens


def get_valid_token(config: dict) -> str:
    """Get a valid access token, refreshing if needed."""
    tokens = _load_tokens()
    if not tokens:
        raise ValueError("Not authenticated. Run freshbooks_authenticate first.")
    if time.time() >= tokens.get("expires_at", 0) - 60:
        tokens = refresh_tokens(config, tokens["refresh_token"])
    return tokens["access_token"]


def get_identity(access_token: str) -> dict:
    """Fetch user identity to get account_id and business_id."""
    resp = httpx.get(IDENTITY_URL, headers={"Authorization": f"Bearer {access_token}"})
    resp.raise_for_status()
    data = resp.json()["response"]
    memberships = data.get("business_memberships", [])
    if not memberships:
        raise ValueError("No business memberships found on this FreshBooks account.")
    biz = memberships[0]["business"]
    return {
        "user_id": data["id"],
        "email": data.get("email", ""),
        "first_name": data.get("first_name", ""),
        "last_name": data.get("last_name", ""),
        "account_id": biz["account_id"],
        "business_id": biz["id"],
        "business_uuid": biz.get("business_uuid", ""),
        "business_name": biz.get("name", ""),
    }


def _generate_self_signed_cert() -> tuple[str, str]:
    """Generate a temporary self-signed cert for the local HTTPS callback server."""
    import subprocess
    cert_dir = tempfile.mkdtemp()
    cert_file = f"{cert_dir}/cert.pem"
    key_file = f"{cert_dir}/key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_file, "-out", cert_file,
        "-days", "1", "-nodes",
        "-subj", "/CN=localhost",
    ], capture_output=True, check=True)
    return cert_file, key_file


def start_callback_server(config: dict, port: int = 8555) -> dict | None:
    """Start a local HTTPS server to catch the OAuth callback. Blocks until callback received."""
    result = {"code": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                result["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Authenticated! You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                error = params.get("error", ["unknown"])[0]
                self.wfile.write(f"<h2>Error: {error}</h2>".encode())
            threading.Thread(target=self.server.shutdown).start()

        def log_message(self, format, *args):
            pass  # Suppress logs

    use_https = config["redirect_uri"].startswith("https://")
    server = HTTPServer(("localhost", port), CallbackHandler)

    if use_https:
        cert_file, key_file = _generate_self_signed_cert()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    server.timeout = 120
    server.handle_request()

    if result["code"]:
        return exchange_code(config, result["code"])
    return None


def _save_tokens(tokens: dict):
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))


def _load_tokens() -> dict | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def is_authenticated() -> bool:
    return _load_tokens() is not None
