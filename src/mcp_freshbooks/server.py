"""MCP server for FreshBooks — 53 tools for accounting, invoicing, and business management."""

import json
import functools
import threading
from decimal import Decimal, InvalidOperation
from mcp.server.fastmcp import FastMCP

import httpx

from . import auth, client

mcp = FastMCP(
    "freshbooks",
    instructions="Production-grade MCP server for FreshBooks. Manage invoices, clients, expenses, payments, time tracking, projects, estimates, and reports.",
)


# ─── Formatting helpers ───

def _fmt(data: dict | list | bool, label: str = "") -> str:
    """Format API response for clean tool output."""
    if isinstance(data, bool):
        return f"{label}: {'success' if data else 'failed'}"
    return json.dumps(data, indent=2, default=str)


def _summarize_list(result: dict, resource_key: str, fields: list[str]) -> str:
    """Summarize a list response with key fields."""
    items = result.get(resource_key, [])
    total = result.get("total", len(items))
    page = result.get("page", 1)
    pages = result.get("pages", 1)
    lines = [f"Page {page}/{pages} ({total} total)\n"]
    for item in items:
        parts = []
        for f in fields:
            val = item.get(f)
            if val is not None:
                if isinstance(val, dict) and "amount" in val:
                    val = f"${val['amount']} {val.get('code', '')}"
                parts.append(f"{f}: {val}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _handle_errors(func):
    """Decorator to catch API errors and return clean messages."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 403:
                return f"Error: Access denied (HTTP 403). This feature may require a paid FreshBooks plan."
            if status == 404:
                return f"Error: Resource not found (HTTP 404)."
            if status == 401:
                return f"Error: Authentication expired. Run freshbooks_authenticate again."
            try:
                body = e.response.json()
                errors = body.get("response", {}).get("errors", [])
                if errors:
                    msgs = [err.get("message", str(err)) for err in errors]
                    return f"Error: {'; '.join(msgs)}"
            except Exception:
                pass
            return f"Error: HTTP {status} — {e.response.text[:200]}"
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
    return wrapper


# ─── Auth Tools ───

@mcp.tool()
def freshbooks_authenticate() -> str:
    """Start FreshBooks OAuth2 authentication. Returns a URL to open in your browser. After authorizing, tokens are saved automatically."""
    config = auth.get_config()
    url = auth.get_auth_url(config)
    port = int(config["redirect_uri"].split(":")[-1].split("/")[0])

    def _run_server():
        auth.start_callback_server(config, port)

    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()

    return (
        f"Open this URL in your browser to authorize:\n\n{url}\n\n"
        f"Waiting for callback on localhost:{port}..."
    )


@mcp.tool()
def freshbooks_authenticate_with_code(code: str) -> str:
    """Complete authentication with an authorization code (if callback server isn't used). Paste the code from the redirect URL."""
    config = auth.get_config()
    tokens = auth.exchange_code(config, code)
    if tokens:
        identity = auth.get_identity(tokens["access_token"])
        return f"Authenticated as {identity['first_name']} {identity['last_name']} ({identity['email']})\nBusiness: {identity['business_name']}\nAccount ID: {identity['account_id']}"
    return "Authentication failed."


@mcp.tool()
@_handle_errors
async def freshbooks_whoami() -> str:
    """Get the current authenticated user's identity, account ID, and business info."""
    identity = await client.whoami()
    return _fmt(identity)


# ─── Invoice Tools ───

@mcp.tool()
@_handle_errors
async def list_invoices(
    page: int = 1,
    per_page: int = 25,
    status: str | None = None,
    customer_id: int | None = None,
) -> str:
    """List invoices with optional filters. Status: draft, sent, viewed, outstanding, paid."""
    filters = {}
    if status:
        filters["display_status"] = status
    if customer_id:
        filters["customerid"] = customer_id
    result = await client.accounting_list(
        "invoices/invoices", page, per_page, filters, includes=["lines"]
    )
    return _summarize_list(result, "invoices", ["id", "invoice_number", "display_status", "amount", "outstanding", "customerid", "due_date"])


@mcp.tool()
@_handle_errors
async def get_invoice(invoice_id: int) -> str:
    """Get full details of a specific invoice including line items."""
    result = await client.accounting_get("invoices/invoices", invoice_id, includes=["lines"])
    return _fmt(result.get("invoice", result))


@mcp.tool()
@_handle_errors
async def create_invoice(
    customer_id: int,
    lines: list[dict],
    due_offset_days: int = 30,
    currency_code: str = "USD",
    notes: str = "",
    po_number: str = "",
) -> str:
    """Create a new invoice. Lines format: [{"name": "Service", "description": "...", "qty": 1, "unit_cost": {"amount": "100.00", "code": "USD"}}]"""
    data = {
        "customerid": customer_id,
        "create_date": _today(),
        "due_offset_days": due_offset_days,
        "currency_code": currency_code,
        "lines": lines,
    }
    if notes:
        data["notes"] = notes
    if po_number:
        data["po_number"] = po_number
    result = await client.accounting_create("invoices/invoices", "invoice", data)
    inv = result.get("invoice", result)
    return f"Invoice #{inv.get('invoice_number', '?')} created (ID: {inv.get('id')}). Amount: ${inv.get('amount', {}).get('amount', '0')}"


@mcp.tool()
@_handle_errors
async def update_invoice(invoice_id: int, updates: dict) -> str:
    """Update an invoice. Pass any writable invoice fields as updates dict."""
    result = await client.accounting_update("invoices/invoices", invoice_id, "invoice", updates)
    inv = result.get("invoice", result)
    return f"Invoice #{inv.get('invoice_number', '?')} updated."


@mcp.tool()
@_handle_errors
async def send_invoice(invoice_id: int) -> str:
    """Send an invoice by email to the client."""
    result = await client.accounting_update(
        "invoices/invoices", invoice_id, "invoice",
        {"action_email": True}
    )
    inv = result.get("invoice", result)
    return f"Invoice #{inv.get('invoice_number', '?')} sent to client."


@mcp.tool()
@_handle_errors
async def delete_invoice(invoice_id: int) -> str:
    """Delete an invoice permanently."""
    await client.accounting_delete("invoices/invoices", invoice_id)
    return f"Invoice {invoice_id} deleted."


# ─── Recurring Invoice Tools ───

@mcp.tool()
@_handle_errors
async def list_recurring_invoices(page: int = 1, per_page: int = 25) -> str:
    """List recurring invoice profiles (templates for automatic billing)."""
    result = await client.accounting_list("invoice_profiles/invoice_profiles", page, per_page)
    return _summarize_list(
        result, "invoice_profiles",
        ["id", "profileid", "frequency", "customerid", "amount", "currency_code", "send_email", "numberRecurring"],
    )


@mcp.tool()
@_handle_errors
async def create_recurring_invoice(
    customer_id: int,
    lines: list[dict],
    frequency: str = "m",
    create_date: str = "",
    send_email: bool = True,
    currency_code: str = "USD",
    number_of_occurrences: int = 0,
    notes: str = "",
) -> str:
    """Create a recurring invoice profile. Frequency: w (weekly), 2w (biweekly), m (monthly), 2m (bimonthly), 3m (quarterly), 6m (semiannual), y (yearly). Occurrences 0 = infinite. Lines format same as invoices."""
    data = {
        "customerid": customer_id,
        "frequency": frequency,
        "create_date": create_date or _today(),
        "send_email": send_email,
        "currency_code": currency_code,
        "numberRecurring": number_of_occurrences,
        "lines": lines,
    }
    if notes:
        data["notes"] = notes
    result = await client.accounting_create("invoice_profiles/invoice_profiles", "invoice_profile", data)
    profile = result.get("invoice_profile", result)
    return f"Recurring invoice created (ID: {profile.get('id')}). Frequency: {frequency}, client: {customer_id}"


@mcp.tool()
@_handle_errors
async def update_recurring_invoice(profile_id: int, updates: dict) -> str:
    """Update a recurring invoice profile. Pass any writable fields (frequency, lines, send_email, disable, etc.)."""
    result = await client.accounting_update(
        "invoice_profiles/invoice_profiles", profile_id, "invoice_profile", updates
    )
    profile = result.get("invoice_profile", result)
    return f"Recurring invoice {profile.get('id')} updated."


# ─── Client Tools ───

@mcp.tool()
@_handle_errors
async def list_clients(
    page: int = 1,
    per_page: int = 25,
    search: str | None = None,
) -> str:
    """List clients. Optional search by name or organization."""
    filters = {}
    if search:
        filters["organization_like"] = search
    result = await client.accounting_list("users/clients", page, per_page, filters)
    return _summarize_list(result, "clients", ["id", "fname", "lname", "organization", "email"])


@mcp.tool()
@_handle_errors
async def get_client(client_id: int) -> str:
    """Get full details of a specific client."""
    result = await client.accounting_get("users/clients", client_id)
    return _fmt(result.get("client", result))


@mcp.tool()
@_handle_errors
async def create_client(
    email: str,
    first_name: str = "",
    last_name: str = "",
    organization: str = "",
    phone: str = "",
    currency_code: str = "USD",
) -> str:
    """Create a new client."""
    data = {"email": email, "currency_code": currency_code}
    if first_name:
        data["fname"] = first_name
    if last_name:
        data["lname"] = last_name
    if organization:
        data["organization"] = organization
    if phone:
        data["mob_phone"] = phone
    result = await client.accounting_create("users/clients", "client", data)
    c = result.get("client", result)
    return f"Client created: {c.get('fname', '')} {c.get('lname', '')} (ID: {c.get('id')})"


@mcp.tool()
@_handle_errors
async def update_client(client_id: int, updates: dict) -> str:
    """Update a client. Pass any writable client fields."""
    result = await client.accounting_update("users/clients", client_id, "client", updates)
    c = result.get("client", result)
    return f"Client {c.get('id')} updated."


@mcp.tool()
@_handle_errors
async def delete_client(client_id: int) -> str:
    """Delete (archive) a client. Removes from active lists but preserves history."""
    await client.accounting_soft_delete("users/clients", client_id, "client")
    return f"Client {client_id} archived."


# ─── Expense Tools ───

@mcp.tool()
@_handle_errors
async def list_expenses(
    page: int = 1,
    per_page: int = 25,
    client_id: int | None = None,
) -> str:
    """List expenses with optional client filter."""
    filters = {}
    if client_id:
        filters["clientid"] = client_id
    result = await client.accounting_list("expenses/expenses", page, per_page, filters)
    return _summarize_list(result, "expenses", ["id", "vendor", "amount", "date", "status", "categoryid"])


@mcp.tool()
@_handle_errors
async def get_expense(expense_id: int) -> str:
    """Get full details of a specific expense."""
    result = await client.accounting_get("expenses/expenses", expense_id)
    return _fmt(result.get("expense", result))


@mcp.tool()
@_handle_errors
async def create_expense(
    category_id: int,
    staff_id: int,
    amount: str,
    date: str,
    vendor: str = "",
    notes: str = "",
    currency_code: str = "USD",
    client_id: int | None = None,
) -> str:
    """Create a new expense. Amount as string (e.g. '150.00'). Date as YYYY-MM-DD."""
    data = {
        "categoryid": category_id,
        "staffid": staff_id,
        "amount": {"amount": amount, "code": currency_code},
        "date": date,
    }
    if vendor:
        data["vendor"] = vendor
    if notes:
        data["notes"] = notes
    if client_id:
        data["clientid"] = client_id
    result = await client.accounting_create("expenses/expenses", "expense", data)
    e = result.get("expense", result)
    return f"Expense created (ID: {e.get('id')}). Amount: ${amount}"


@mcp.tool()
@_handle_errors
async def update_expense(expense_id: int, updates: dict) -> str:
    """Update an expense. Pass any writable expense fields (vendor, amount, notes, date, categoryid, etc.)."""
    result = await client.accounting_update("expenses/expenses", expense_id, "expense", updates)
    e = result.get("expense", result)
    return f"Expense {e.get('id')} updated."


@mcp.tool()
@_handle_errors
async def delete_expense(expense_id: int) -> str:
    """Delete (archive) an expense."""
    await client.accounting_soft_delete("expenses/expenses", expense_id, "expense")
    return f"Expense {expense_id} deleted."


_ALLOWED_RECEIPT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
_MAX_RECEIPT_BYTES = 25 * 1024 * 1024  # 25 MB
_EXT_FOR_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "application/pdf": ".pdf",
}


@mcp.tool()
@_handle_errors
async def attach_receipt_to_expense(
    expense_id: int,
    receipt_data: str | None = None,
    mime_type: str | None = None,
    filename: str | None = None,
    receipt_url: str | None = None,
) -> str:
    """Attach a receipt (image or PDF) to an existing expense.

    Pass EITHER receipt_data (base64-encoded file bytes — use this for Gmail attachments,
    where the Gmail tool returns the attachment as base64) OR receipt_url (a publicly-fetchable
    URL — use this for vendor receipt links like Amazon/Stripe).

    For receipt_data, mime_type is required (e.g. 'application/pdf', 'image/jpeg') and
    filename is recommended. The Gmail message_part metadata includes both.

    Allowed types: image/png, image/jpeg, image/gif, image/webp, application/pdf. Max 25 MB.
    """
    import base64

    if (receipt_data is None) == (receipt_url is None):
        return "Error: Pass exactly one of receipt_data (base64) or receipt_url."

    if receipt_data is not None:
        if not mime_type:
            return "Error: mime_type is required when passing receipt_data."
        try:
            file_bytes = base64.b64decode(receipt_data, validate=False)
        except Exception as e:
            return f"Error: receipt_data is not valid base64 ({e})."
        mime_type = mime_type.split(";")[0].strip().lower()
    else:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
            resp = await http.get(receipt_url)
            resp.raise_for_status()
            file_bytes = resp.content
            mime_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not filename:
            url_tail = receipt_url.rsplit("/", 1)[-1].split("?")[0]
            filename = url_tail if "." in url_tail else None

    if mime_type not in _ALLOWED_RECEIPT_TYPES:
        return f"Error: Unsupported receipt type '{mime_type}'. Allowed: {sorted(_ALLOWED_RECEIPT_TYPES)}."
    if len(file_bytes) > _MAX_RECEIPT_BYTES:
        return f"Error: Receipt is {len(file_bytes)} bytes; limit is {_MAX_RECEIPT_BYTES}."
    if not filename:
        filename = f"receipt-{expense_id}{_EXT_FOR_MIME[mime_type]}"

    uploaded = await client.upload_attachment(file_bytes, filename, mime_type)
    await client.accounting_update(
        "expenses/expenses",
        expense_id,
        "expense",
        {"attachment": {"jwt": uploaded["jwt"], "media_type": uploaded["media_type"]}},
    )
    return f"Receipt attached to expense {expense_id} ({filename}, {len(file_bytes)} bytes, {mime_type})."


# ─── Payment Tools ───

@mcp.tool()
@_handle_errors
async def list_payments(
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List all payments."""
    result = await client.accounting_list("payments/payments", page, per_page)
    return _summarize_list(result, "payments", ["id", "invoiceid", "amount", "date", "type"])


@mcp.tool()
@_handle_errors
async def create_payment(
    invoice_id: int,
    amount: str,
    date: str,
    payment_type: str = "Other",
    note: str = "",
    currency_code: str = "USD",
) -> str:
    """Record a payment against an invoice. Amount as string. Date as YYYY-MM-DD. Types: Check, Credit, Cash, Bank Transfer, Credit Card, PayPal, ACH, Other."""
    data = {
        "invoiceid": invoice_id,
        "amount": {"amount": amount, "code": currency_code},
        "date": date,
        "type": payment_type,
    }
    if note:
        data["note"] = note
    result = await client.accounting_create("payments/payments", "payment", data)
    p = result.get("payment", result)
    return f"Payment of ${amount} recorded against invoice {invoice_id} (Payment ID: {p.get('id')})"


@mcp.tool()
@_handle_errors
async def get_payment(payment_id: int) -> str:
    """Get full details of a specific payment."""
    result = await client.accounting_get("payments/payments", payment_id)
    return _fmt(result.get("payment", result))


# ─── Time Entry Tools ───

@mcp.tool()
@_handle_errors
async def list_time_entries(
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List time entries."""
    result = await client.projects_list("time_entries", page, per_page)
    entries = result.get("time_entries", [])
    lines = [f"Found {len(entries)} time entries\n"]
    for e in entries:
        dur = e.get("duration", 0)
        hours = dur // 3600
        mins = (dur % 3600) // 60
        lines.append(
            f"ID: {e.get('id')} | {hours}h{mins}m | "
            f"project: {e.get('project_id', '-')} | "
            f"client: {e.get('client_id', '-')} | "
            f"date: {e.get('started_at', '')[:10]} | "
            f"note: {e.get('note', '')[:50]}"
        )
    return "\n".join(lines)


@mcp.tool()
@_handle_errors
async def create_time_entry(
    started_at: str,
    duration_seconds: int,
    client_id: int | None = None,
    project_id: int | None = None,
    note: str = "",
    billable: bool = True,
) -> str:
    """Create a time entry. started_at as ISO8601 (e.g. '2026-03-20T09:00:00'). Duration in seconds."""
    data = {
        "started_at": started_at,
        "duration": duration_seconds,
        "is_logged": True,
        "billable": billable,
    }
    if client_id:
        data["client_id"] = client_id
    if project_id:
        data["project_id"] = project_id
    if note:
        data["note"] = note
    result = await client.projects_create("time_entries", "time_entry", data)
    te = result.get("time_entry", result)
    hours = duration_seconds // 3600
    mins = (duration_seconds % 3600) // 60
    return f"Time entry created (ID: {te.get('id')}). Duration: {hours}h{mins}m"


@mcp.tool()
@_handle_errors
async def get_time_entry(time_entry_id: int) -> str:
    """Get full details of a specific time entry."""
    result = await client.projects_get("time_entries", time_entry_id)
    te = result.get("time_entry", result)
    return _fmt(te)


@mcp.tool()
@_handle_errors
async def update_time_entry(time_entry_id: int, updates: dict) -> str:
    """Update a time entry. Updatable fields: duration (seconds), note, started_at, billable, client_id, project_id."""
    result = await client.projects_update("time_entries", time_entry_id, "time_entry", updates)
    te = result.get("time_entry", result)
    return f"Time entry {te.get('id')} updated."


@mcp.tool()
@_handle_errors
async def delete_time_entry(time_entry_id: int) -> str:
    """Delete a time entry."""
    await client.projects_delete("time_entries", time_entry_id)
    return f"Time entry {time_entry_id} deleted."


# ─── Project Tools ───

@mcp.tool()
@_handle_errors
async def list_projects(
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List projects."""
    result = await client.projects_list("projects", page, per_page)
    projects = result.get("projects", [])
    lines = [f"Found {len(projects)} projects\n"]
    for p in projects:
        lines.append(
            f"ID: {p.get('id')} | {p.get('title', 'Untitled')} | "
            f"client: {p.get('client_id', '-')} | "
            f"type: {p.get('project_type', '-')} | "
            f"active: {p.get('active', '-')}"
        )
    return "\n".join(lines)


@mcp.tool()
@_handle_errors
async def create_project(
    title: str,
    client_id: int | None = None,
    project_type: str = "hourly_rate",
    billing_method: str = "project_rate",
    description: str = "",
    budget: float | None = None,
    due_date: str | None = None,
) -> str:
    """Create a project. project_type: hourly_rate or fixed_price. billing_method: business_rate, project_rate, service_rate, team_member_rate."""
    data = {
        "title": title,
        "project_type": project_type,
        "billing_method": billing_method,
    }
    if client_id:
        data["client_id"] = client_id
    if description:
        data["description"] = description
    if budget is not None:
        data["budget"] = budget
    if due_date:
        data["due_date"] = due_date
    result = await client.projects_create("projects", "project", data)
    p = result.get("project", result)
    return f"Project '{title}' created (ID: {p.get('id')})"


@mcp.tool()
@_handle_errors
async def get_project(project_id: int) -> str:
    """Get full details of a specific project."""
    result = await client.projects_get("projects", project_id)
    p = result.get("project", result)
    return _fmt(p)


@mcp.tool()
@_handle_errors
async def update_project(project_id: int, updates: dict) -> str:
    """Update a project. Pass any writable project fields (title, description, due_date, budget, etc.)."""
    result = await client.projects_update("projects", project_id, "project", updates)
    p = result.get("project", result)
    return f"Project '{p.get('title', '?')}' updated."


# ─── Estimate Tools ───

@mcp.tool()
@_handle_errors
async def list_estimates(
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List estimates."""
    result = await client.accounting_list("estimates/estimates", page, per_page)
    return _summarize_list(result, "estimates", ["id", "estimate_number", "display_status", "amount", "customerid"])


@mcp.tool()
@_handle_errors
async def create_estimate(
    customer_id: int,
    lines: list[dict],
    currency_code: str = "USD",
    notes: str = "",
) -> str:
    """Create an estimate. Lines format same as invoices."""
    data = {
        "customerid": customer_id,
        "create_date": _today(),
        "currency_code": currency_code,
        "lines": lines,
    }
    if notes:
        data["notes"] = notes
    result = await client.accounting_create("estimates/estimates", "estimate", data)
    est = result.get("estimate", result)
    return f"Estimate #{est.get('estimate_number', '?')} created (ID: {est.get('id')})"


@mcp.tool()
@_handle_errors
async def get_estimate(estimate_id: int) -> str:
    """Get full details of a specific estimate including line items."""
    result = await client.accounting_get("estimates/estimates", estimate_id)
    return _fmt(result.get("estimate", result))


@mcp.tool()
@_handle_errors
async def update_estimate(estimate_id: int, updates: dict) -> str:
    """Update an estimate. Pass any writable estimate fields."""
    result = await client.accounting_update("estimates/estimates", estimate_id, "estimate", updates)
    est = result.get("estimate", result)
    return f"Estimate #{est.get('estimate_number', '?')} updated."


@mcp.tool()
@_handle_errors
async def send_estimate(estimate_id: int) -> str:
    """Send an estimate by email to the client."""
    result = await client.accounting_update(
        "estimates/estimates", estimate_id, "estimate",
        {"action_email": True}
    )
    est = result.get("estimate", result)
    return f"Estimate #{est.get('estimate_number', '?')} sent to client."


# ─── Report Tools ───

@mcp.tool()
@_handle_errors
async def get_profit_loss(start_date: str, end_date: str) -> str:
    """Get Profit & Loss report for a date range. Dates as YYYY-MM-DD."""
    result = await client.get_report("profitloss_entity", {"start_date": start_date, "end_date": end_date})
    return _fmt(result)


@mcp.tool()
@_handle_errors
async def get_tax_summary(start_date: str, end_date: str, cash_based: bool = False) -> str:
    """Get tax summary report for a date range. cash_based=True recognizes tax on PAYMENT date
    (matches what state sales-tax filings — NY ST-100, MA ST-9, CT OS-114 — actually want).
    Default False = accrual (invoice-creation date)."""
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "cash_based": "true" if cash_based else "false",
    }
    result = await client.get_report("taxsummary", params)
    return _fmt(result)


@mcp.tool()
@_handle_errors
async def get_accounts_aging() -> str:
    """Get accounts aging report — shows overdue invoices grouped by 0-30, 31-60, 61-90, 90+ days."""
    result = await client.get_report("accounts_aging")
    return _fmt(result)


@mcp.tool()
@_handle_errors
async def get_balance_sheet(start_date: str, end_date: str) -> str:
    """Get balance sheet report for a date range."""
    result = await client.get_report("balance_sheet", {"start_date": start_date, "end_date": end_date})
    return _fmt(result)


@mcp.tool()
@_handle_errors
async def get_payments_collected(start_date: str, end_date: str) -> str:
    """Get payments collected report for a date range. Shows cash flow from client payments."""
    result = await client.get_report("payments_collected", {"start_date": start_date, "end_date": end_date})
    return _fmt(result)


@mcp.tool()
@_handle_errors
async def get_invoice_details_report(start_date: str, end_date: str) -> str:
    """Pull the invoice_details report — every invoice with line-level detail in one call for a date range. Dates as YYYY-MM-DD."""
    result = await client.get_report("invoice_details", {"start_date": start_date, "end_date": end_date})
    return _fmt(result)


def _dec(val) -> Decimal:
    """Parse a FreshBooks amount field (string or dict like {'amount': '12.34'}) to Decimal."""
    if val is None:
        return Decimal("0")
    if isinstance(val, dict):
        val = val.get("amount", "0")
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return Decimal("0")


async def _list_invoices_in_range(start_date: str, end_date: str) -> list[dict]:
    """Walk pagination and return all non-draft, non-deleted invoices created in [start_date, end_date]."""
    invoices: list[dict] = []
    page = 1
    while True:
        result = await client.accounting_list(
            "invoices/invoices",
            page=page,
            per_page=100,
            filters={"date_min": start_date, "date_max": end_date},
            includes=["lines"],
        )
        batch = result.get("invoices", [])
        invoices.extend(batch)
        pages = result.get("pages", 1) or 1
        if page >= pages or not batch:
            break
        page += 1
    # Defensive client-side filter: keep active (vis_state == 0) and skip drafts.
    return [
        inv for inv in invoices
        if inv.get("vis_state", 0) == 0
        and inv.get("display_status") != "draft"
    ]


def _line_taxes(line: dict) -> list[tuple[str, Decimal]]:
    """Return list of (tax_name, tax_percentage) for a line — only entries where both fields are set."""
    out = []
    for n, a in (("taxName1", "taxAmount1"), ("taxName2", "taxAmount2")):
        name = line.get(n)
        amount = line.get(a)
        if name and amount not in (None, "", 0, "0"):
            out.append((name, _dec(amount)))
    return out


@mcp.tool()
@_handle_errors
async def find_invoices_missing_tax(
    start_date: str,
    end_date: str,
    expected_tax_name: str | None = None,
) -> str:
    """Flag invoices in a date range where no line has any tax applied (or, if expected_tax_name is given,
    invoices where no line carries that specific tax name). Useful for catching billing errors before
    sales tax filing. Dates as YYYY-MM-DD."""
    invoices = await _list_invoices_in_range(start_date, end_date)
    flagged = []
    for inv in invoices:
        lines = inv.get("lines", [])
        if not lines:
            continue
        if expected_tax_name:
            has_expected = any(
                name == expected_tax_name
                for line in lines
                for name, _ in _line_taxes(line)
            )
            if has_expected:
                continue
            reason = f"missing '{expected_tax_name}' on every line"
        else:
            has_any_tax = any(_line_taxes(line) for line in lines)
            if has_any_tax:
                continue
            reason = "no tax on any line"
        flagged.append({
            "id": inv.get("id"),
            "invoice_number": inv.get("invoice_number"),
            "create_date": inv.get("create_date"),
            "customerid": inv.get("customerid"),
            "amount": inv.get("amount"),
            "display_status": inv.get("display_status"),
            "reason": reason,
        })
    if not flagged:
        scope = f"with '{expected_tax_name}'" if expected_tax_name else "with any tax"
        return f"All {len(invoices)} invoices in {start_date} – {end_date} are tagged {scope}. No issues found."
    lines_out = [f"Found {len(flagged)} of {len(invoices)} invoices missing expected tax in {start_date} – {end_date}:\n"]
    for f in flagged:
        amt = f.get("amount", {})
        amt_str = f"${amt.get('amount', '?')} {amt.get('code', '')}".strip() if isinstance(amt, dict) else str(amt)
        lines_out.append(
            f"  #{f['invoice_number']} (id {f['id']}) — {f['create_date']} — {amt_str} — {f['display_status']} — {f['reason']}"
        )
    return "\n".join(lines_out)


@mcp.tool()
@_handle_errors
async def verify_tax_summary(start_date: str, end_date: str, cash_based: bool = False) -> str:
    """Recompute the sales-tax-summary from every invoice's line items and compare to FreshBooks's
    taxsummary report. cash_based=True asks FreshBooks for payment-date recognition (what NY ST-100,
    MA ST-9, CT OS-114 actually want); default False = accrual. Returns a side-by-side per-tax
    breakdown plus exempt sales, and flags any divergence. Dates as YYYY-MM-DD."""
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "cash_based": "true" if cash_based else "false",
    }
    raw = await client.get_report("taxsummary", params)
    # FreshBooks wraps the report under a "taxsummary" key. Unwrap defensively (also handles
    # any future flattening on FreshBooks's side).
    report = raw.get("taxsummary", raw) if isinstance(raw, dict) else {}
    invoices = await _list_invoices_in_range(start_date, end_date)

    gross_pretax = Decimal("0")
    per_tax_taxable: dict[str, Decimal] = {}
    per_tax_collected: dict[str, Decimal] = {}
    exempt = Decimal("0")
    contributing_invoice_ids_by_tax: dict[str, list[int]] = {}

    for inv in invoices:
        for line in inv.get("lines", []):
            subtotal = _dec(line.get("amount"))
            if subtotal == 0:
                qty = _dec(line.get("qty"))
                unit = _dec(line.get("unit_cost"))
                subtotal = qty * unit
            gross_pretax += subtotal
            line_taxes = _line_taxes(line)
            if not line_taxes:
                exempt += subtotal
                continue
            for name, pct in line_taxes:
                per_tax_taxable[name] = per_tax_taxable.get(name, Decimal("0")) + subtotal
                per_tax_collected[name] = per_tax_collected.get(name, Decimal("0")) + (subtotal * pct / Decimal("100"))
                contributing_invoice_ids_by_tax.setdefault(name, []).append(inv.get("id"))

    total_tax_recomputed = sum(per_tax_collected.values(), Decimal("0"))
    invoice_total_recomputed = gross_pretax + total_tax_recomputed

    reported_by_name: dict[str, dict] = {}
    for t in report.get("taxes", []):
        name = t.get("tax_name", "")
        # Prefer net_taxable_amount (FreshBooks's primary taxable basis); fall back to
        # taxable_amount_collected if the response uses the older shape.
        net_taxable = t.get("net_taxable_amount") if t.get("net_taxable_amount") is not None else t.get("taxable_amount_collected")
        reported_by_name[name] = {
            "taxable_amount": _dec(net_taxable),
            "tax_collected": _dec(t.get("tax_collected")),
        }
    reported_total_invoiced = _dec(report.get("total_invoiced"))

    basis = "cash" if cash_based else "accrual"
    out = [
        f"Sales Tax Verification: {start_date} – {end_date}  (basis: {basis})",
        f"Invoices considered: {len(invoices)} (non-draft, vis_state=0; filtered by create_date)",
        "",
        f"{'Metric':<40} {'Reported':>15} {'Recomputed':>15} {'Δ':>12}",
        "-" * 84,
    ]

    def _row(label: str, reported: Decimal, recomputed: Decimal, *, flag: bool = True) -> str:
        delta = recomputed - reported
        marker = " ⚠" if (flag and abs(delta) >= Decimal("0.01")) else ""
        return f"{label:<40} {str(reported):>15} {str(recomputed):>15} {str(delta):>12}{marker}"

    out.append(_row("Invoice total (incl. tax)", reported_total_invoiced, invoice_total_recomputed))
    out.append(f"{'  └─ Pre-tax line subtotal':<40} {'':>15} {str(gross_pretax):>15}")
    all_tax_names = sorted(set(per_tax_taxable) | set(reported_by_name))
    for name in all_tax_names:
        rep = reported_by_name.get(name, {})
        out.append(_row(
            f"  {name}: taxable sales",
            rep.get("taxable_amount", Decimal("0")),
            per_tax_taxable.get(name, Decimal("0")),
        ))
        out.append(_row(
            f"  {name}: tax collected",
            rep.get("tax_collected", Decimal("0")),
            per_tax_collected.get(name, Decimal("0")),
        ))
    out.append(_row("Untaxed line subtotal (incl. discounts)", Decimal("0"), exempt, flag=False))

    discrepancies = []
    if abs(invoice_total_recomputed - reported_total_invoiced) >= Decimal("0.01"):
        discrepancies.append("invoice_total")
    for name in all_tax_names:
        rep = reported_by_name.get(name, {})
        if abs(per_tax_taxable.get(name, Decimal("0")) - rep.get("taxable_amount", Decimal("0"))) >= Decimal("0.01"):
            discrepancies.append(f"{name} taxable")
        if abs(per_tax_collected.get(name, Decimal("0")) - rep.get("tax_collected", Decimal("0"))) >= Decimal("0.01"):
            discrepancies.append(f"{name} collected")

    if cash_based:
        out.append("")
        out.append("ℹ cash_based=True: the Reported column reflects payment-date recognition,")
        out.append("  but the Recomputed column walks invoices by CREATE date. A divergence may simply")
        out.append("  indicate timing — invoices issued in one period and paid in another.")

    if discrepancies:
        out.append("")
        out.append(f"⚠ Discrepancies in: {', '.join(discrepancies)}")
        out.append("Contributing invoice IDs by tax (use get_invoice to drill in):")
        for name, ids in contributing_invoice_ids_by_tax.items():
            unique_ids = sorted(set(ids))
            out.append(f"  {name}: {unique_ids}")
    else:
        out.append("")
        out.append("✓ Recomputed totals match the FreshBooks taxsummary report within $0.01.")

    return "\n".join(out)


# ─── Item Tools ───

@mcp.tool()
@_handle_errors
async def list_items(page: int = 1, per_page: int = 25) -> str:
    """List items in your product/service catalog. Items can be used as invoice line items."""
    result = await client.accounting_list("items/items", page, per_page)
    return _summarize_list(result, "items", ["id", "name", "description", "unit_cost", "inventory"])


@mcp.tool()
@_handle_errors
async def create_item(
    name: str,
    description: str = "",
    unit_cost: str = "0.00",
    currency_code: str = "USD",
    inventory: int | None = None,
    tax1: int | None = None,
    tax2: int | None = None,
) -> str:
    """Create a catalog item for reuse on invoices. unit_cost as string (e.g. '100.00'). tax1/tax2 are tax IDs."""
    data = {
        "name": name,
        "unit_cost": {"amount": unit_cost, "code": currency_code},
    }
    if description:
        data["description"] = description
    if inventory is not None:
        data["inventory"] = inventory
    if tax1 is not None:
        data["tax1"] = tax1
    if tax2 is not None:
        data["tax2"] = tax2
    result = await client.accounting_create("items/items", "item", data)
    item = result.get("item", result)
    return f"Item '{name}' created (ID: {item.get('id')})"


# ─── Expense Category Tools ───

@mcp.tool()
@_handle_errors
async def list_expense_categories(page: int = 1, per_page: int = 25) -> str:
    """List expense categories for organizing expenses."""
    result = await client.accounting_list("expenses/categories", page, per_page)
    return _summarize_list(result, "categories", ["id", "category", "parentid"])


# ─── Tax Tools ───

@mcp.tool()
@_handle_errors
async def list_taxes(page: int = 1, per_page: int = 25) -> str:
    """List configured tax rates."""
    result = await client.accounting_list("taxes/taxes", page, per_page)
    return _summarize_list(result, "taxes", ["id", "name", "amount", "number"])


# ─── Workflow Tools ───

@mcp.tool()
@_handle_errors
async def convert_estimate_to_invoice(estimate_id: int) -> str:
    """Convert an accepted estimate into an invoice. Copies line items, client, and currency from the estimate."""
    est_result = await client.accounting_get("estimates/estimates", estimate_id)
    est = est_result.get("estimate", est_result)
    lines = []
    for line in est.get("lines", []):
        lines.append({
            "name": line.get("name", ""),
            "description": line.get("description", ""),
            "qty": line.get("qty", 1),
            "unit_cost": line.get("unit_cost", {"amount": "0.00", "code": "USD"}),
        })
    if not lines:
        return f"Error: Estimate #{estimate_id} has no line items to convert."
    inv_data = {
        "customerid": est.get("customerid"),
        "create_date": _today(),
        "currency_code": est.get("currency_code", "USD"),
        "lines": lines,
        "notes": est.get("notes", ""),
    }
    result = await client.accounting_create("invoices/invoices", "invoice", inv_data)
    inv = result.get("invoice", result)
    return (
        f"Invoice #{inv.get('invoice_number', '?')} created from Estimate #{est.get('estimate_number', '?')} "
        f"(ID: {inv.get('id')}). Amount: ${inv.get('amount', {}).get('amount', '0')}"
    )


@mcp.tool()
@_handle_errors
async def get_overdue_invoices() -> str:
    """Get all overdue invoices — clients who owe you money past the due date."""
    result = await client.accounting_list(
        "invoices/invoices", 1, 100,
        filters={"display_status": "overdue"},
        includes=["lines"],
    )
    invoices = result.get("invoices", [])
    if not invoices:
        return "No overdue invoices. All caught up!"
    total_overdue = sum(
        float(inv.get("outstanding", {}).get("amount", 0))
        for inv in invoices
    )
    lines = [f"{len(invoices)} overdue invoice(s) — ${total_overdue:.2f} outstanding\n"]
    for inv in invoices:
        lines.append(
            f"#{inv.get('invoice_number', '?')} | "
            f"client: {inv.get('customerid')} | "
            f"outstanding: ${inv.get('outstanding', {}).get('amount', '0')} | "
            f"due: {inv.get('due_date', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
@_handle_errors
async def get_unbilled_time(client_id: int | None = None, project_id: int | None = None) -> str:
    """Get unbilled time entries — money left on the table. Optionally filter by client or project."""
    all_entries = []
    page = 1
    while True:
        result = await client.projects_list("time_entries", page, 100)
        entries = result.get("time_entries", [])
        if not entries:
            break
        all_entries.extend(entries)
        if len(entries) < 100:
            break
        page += 1
    unbilled = [
        e for e in all_entries
        if not e.get("billed") and e.get("billable", True)
    ]
    if client_id:
        unbilled = [e for e in unbilled if e.get("client_id") == client_id]
    if project_id:
        unbilled = [e for e in unbilled if e.get("project_id") == project_id]
    if not unbilled:
        return "No unbilled time entries found."
    total_seconds = sum(e.get("duration", 0) for e in unbilled)
    hours = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    lines = [f"{len(unbilled)} unbilled entries — {hours}h{mins}m total\n"]
    for e in unbilled:
        dur = e.get("duration", 0)
        h = dur // 3600
        m = (dur % 3600) // 60
        lines.append(
            f"ID: {e.get('id')} | {h}h{m}m | "
            f"project: {e.get('project_id', '-')} | "
            f"client: {e.get('client_id', '-')} | "
            f"date: {e.get('started_at', '')[:10]} | "
            f"note: {e.get('note', '')[:50]}"
        )
    return "\n".join(lines)


@mcp.tool()
@_handle_errors
async def invoice_from_time(
    client_id: int,
    hourly_rate: str,
    project_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    currency_code: str = "USD",
) -> str:
    """Create an invoice from unbilled time entries for a client. Groups by project. Marks entries as billed. hourly_rate as string (e.g. '150.00'). Optional date range filter (YYYY-MM-DD)."""
    # Fetch unbilled time
    all_entries = []
    page = 1
    while True:
        result = await client.projects_list("time_entries", page, 100)
        entries = result.get("time_entries", [])
        if not entries:
            break
        all_entries.extend(entries)
        if len(entries) < 100:
            break
        page += 1
    unbilled = [
        e for e in all_entries
        if not e.get("billed") and e.get("billable", True) and e.get("client_id") == client_id
    ]
    if project_id:
        unbilled = [e for e in unbilled if e.get("project_id") == project_id]
    if start_date:
        unbilled = [e for e in unbilled if (e.get("started_at", "") or "")[:10] >= start_date]
    if end_date:
        unbilled = [e for e in unbilled if (e.get("started_at", "") or "")[:10] <= end_date]
    if not unbilled:
        return "No unbilled time entries found for this client."
    # Group by project
    by_project: dict[int | str, list] = {}
    for e in unbilled:
        pid = e.get("project_id") or "no_project"
        by_project.setdefault(pid, []).append(e)
    # Build invoice lines
    lines = []
    for pid, entries in by_project.items():
        total_secs = sum(e.get("duration", 0) for e in entries)
        total_hours = round(total_secs / 3600, 2)
        date_range = ""
        dates = sorted([(e.get("started_at", "") or "")[:10] for e in entries])
        if dates:
            date_range = f" ({dates[0]} to {dates[-1]})" if dates[0] != dates[-1] else f" ({dates[0]})"
        name = f"Project {pid}" if pid != "no_project" else "Services"
        lines.append({
            "name": name,
            "description": f"{total_hours}h @ ${hourly_rate}/hr{date_range}",
            "qty": total_hours,
            "unit_cost": {"amount": hourly_rate, "code": currency_code},
        })
    # Create invoice
    inv_data = {
        "customerid": client_id,
        "create_date": _today(),
        "currency_code": currency_code,
        "lines": lines,
    }
    inv_result = await client.accounting_create("invoices/invoices", "invoice", inv_data)
    inv = inv_result.get("invoice", inv_result)
    # Mark entries as billed
    billed_count = 0
    for e in unbilled:
        try:
            await client.projects_update("time_entries", e["id"], "time_entry", {"billed": True})
            billed_count += 1
        except Exception:
            pass
    total_hours = round(sum(e.get("duration", 0) for e in unbilled) / 3600, 2)
    return (
        f"Invoice #{inv.get('invoice_number', '?')} created (ID: {inv.get('id')}). "
        f"Amount: ${inv.get('amount', {}).get('amount', '0')}. "
        f"{total_hours}h across {len(unbilled)} entries. "
        f"{billed_count}/{len(unbilled)} entries marked as billed."
    )


@mcp.tool()
@_handle_errors
async def client_summary(client_id: int) -> str:
    """Get a full summary of a client — contact info, invoices, outstanding balance, and unbilled time."""
    # Client details
    client_result = await client.accounting_get("users/clients", client_id)
    c = client_result.get("client", client_result)
    # Invoices for this client
    inv_result = await client.accounting_list(
        "invoices/invoices", 1, 100,
        filters={"customerid": client_id},
    )
    invoices = inv_result.get("invoices", [])
    total_invoiced = sum(float(i.get("amount", {}).get("amount", 0)) for i in invoices)
    total_outstanding = sum(float(i.get("outstanding", {}).get("amount", 0)) for i in invoices)
    overdue = [i for i in invoices if i.get("display_status") == "overdue"]
    paid = [i for i in invoices if i.get("display_status") == "paid"]
    # Unbilled time
    all_time = []
    page = 1
    while True:
        result = await client.projects_list("time_entries", page, 100)
        entries = result.get("time_entries", [])
        if not entries:
            break
        all_time.extend(entries)
        if len(entries) < 100:
            break
        page += 1
    unbilled = [
        e for e in all_time
        if e.get("client_id") == client_id and not e.get("billed") and e.get("billable", True)
    ]
    unbilled_secs = sum(e.get("duration", 0) for e in unbilled)
    unbilled_h = unbilled_secs // 3600
    unbilled_m = (unbilled_secs % 3600) // 60
    # Format
    name = f"{c.get('fname', '')} {c.get('lname', '')}".strip()
    org = c.get("organization", "")
    header = f"{org} ({name})" if org else name
    lines = [
        f"=== {header} ===",
        f"Email: {c.get('email', '-')}",
        f"Phone: {c.get('mob_phone', '-')}",
        "",
        f"Invoices: {len(invoices)} total, {len(paid)} paid, {len(overdue)} overdue",
        f"Total invoiced: ${total_invoiced:.2f}",
        f"Outstanding: ${total_outstanding:.2f}",
        f"Unbilled time: {unbilled_h}h{unbilled_m}m ({len(unbilled)} entries)",
    ]
    if overdue:
        lines.append(f"\nOverdue invoices:")
        for inv in overdue:
            lines.append(
                f"  #{inv.get('invoice_number', '?')} — "
                f"${inv.get('outstanding', {}).get('amount', '0')} due {inv.get('due_date', 'N/A')}"
            )
    return "\n".join(lines)


# ─── Helpers ───

def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def main():
    """Entry point for the MCP server."""
    import os
    standby_port = os.environ.get("ACTOR_STANDBY_PORT")
    if standby_port:
        os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")
        os.environ.setdefault("FASTMCP_PORT", standby_port)
        os.environ.setdefault("FASTMCP_STREAMABLE_HTTP_PATH", "/mcp")
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
