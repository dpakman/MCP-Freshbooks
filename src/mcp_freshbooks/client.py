"""FreshBooks API client with auto-refresh and error handling."""

from typing import Any

import httpx

from .auth import get_config, get_valid_token, get_identity

ACCOUNTING_BASE = "https://api.freshbooks.com/accounting/account"
PROJECTS_BASE = "https://api.freshbooks.com/projects/business"
UPLOADS_BASE = "https://api.freshbooks.com/uploads/account"
# Newer accounting API, keyed by business_uuid (journal entries, ledger accounts,
# general ledger / trial balance / account-entry-details reports).
BUSINESSES_BASE = "https://api.freshbooks.com/accounting/businesses"
JE_API_VERSION = "2023-09-25"  # required x-api-version for journal_entries CRUD

_identity_cache: dict | None = None


async def _get_headers() -> dict:
    config = get_config()
    token = get_valid_token(config)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def get_ids() -> tuple[str, str]:
    """Get (account_id, business_id), cached after first call."""
    global _identity_cache
    if _identity_cache is None:
        config = get_config()
        token = get_valid_token(config)
        _identity_cache = get_identity(token)
    return _identity_cache["account_id"], _identity_cache["business_id"]


async def get_business_uuid() -> str:
    """Get the business_uuid (for the newer /accounting/businesses API), cached."""
    await get_ids()  # ensures _identity_cache is populated
    uuid = (_identity_cache or {}).get("business_uuid")
    if not uuid:
        raise ValueError(
            "business_uuid not available on this account's identity. "
            "Re-run freshbooks_authenticate to refresh identity."
        )
    return uuid


async def whoami() -> dict:
    """Get current user identity."""
    global _identity_cache
    config = get_config()
    token = get_valid_token(config)
    _identity_cache = get_identity(token)
    return _identity_cache


async def businesses_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    api_version: str | None = None,
) -> dict:
    """Call the newer /accounting/businesses/<business_uuid>/<path> API.

    Returns the raw decoded JSON — callers unwrap, since these endpoints differ:
    reports nest under response.result.<key>, while journal_entries / ledger_accounts
    return their payload at the top level.
    """
    business_uuid = await get_business_uuid()
    url = f"{BUSINESSES_BASE}/{business_uuid}/{path}"
    headers = await _get_headers()
    if api_version:
        headers["x-api-version"] = api_version
    async with httpx.AsyncClient() as http:
        resp = await http.request(
            method, url, headers=headers, params=params or {}, json=json_body
        )
        resp.raise_for_status()
        return resp.json()


async def businesses_get(path: str, params: dict | None = None, api_version: str | None = None) -> dict:
    return await businesses_request("GET", path, params=params, api_version=api_version)


async def businesses_post(path: str, json_body: dict, api_version: str | None = None) -> dict:
    return await businesses_request("POST", path, json_body=json_body, api_version=api_version)


async def businesses_put(path: str, json_body: dict, api_version: str | None = None) -> dict:
    return await businesses_request("PUT", path, json_body=json_body, api_version=api_version)


def _build_search_params(filters: dict | None) -> dict:
    """Convert filter dict to FreshBooks search[key]=value format."""
    if not filters:
        return {}
    params = {}
    for key, value in filters.items():
        if isinstance(value, list):
            for v in value:
                params.setdefault(f"search[{key}][]", []).append(str(v))
        else:
            params[f"search[{key}]"] = str(value)
    return params


async def accounting_list(
    resource: str,
    page: int = 1,
    per_page: int = 25,
    filters: dict | None = None,
    includes: list[str] | None = None,
    sort: str | None = None,
) -> dict:
    """List an accounting resource (invoices, clients, expenses, etc.)."""
    account_id, _ = await get_ids()
    url = f"{ACCOUNTING_BASE}/{account_id}/{resource}"
    params: dict[str, Any] = {"page": page, "per_page": min(per_page, 100)}
    params.update(_build_search_params(filters))
    if includes:
        for inc in includes:
            params.setdefault("include[]", [])
            if isinstance(params["include[]"], list):
                params["include[]"].append(inc)
    if sort:
        params["sort"] = sort

    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()["response"]["result"]


async def accounting_get(resource: str, resource_id: int | str, includes: list[str] | None = None) -> dict:
    """Get a single accounting resource."""
    account_id, _ = await get_ids()
    url = f"{ACCOUNTING_BASE}/{account_id}/{resource}/{resource_id}"
    params: dict[str, Any] = {}
    if includes:
        params["include[]"] = list(includes)
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()["response"]["result"]


async def accounting_create(resource: str, wrapper_key: str, data: dict) -> dict:
    """Create an accounting resource."""
    account_id, _ = await get_ids()
    url = f"{ACCOUNTING_BASE}/{account_id}/{resource}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json={wrapper_key: data})
        resp.raise_for_status()
        return resp.json()["response"]["result"]


async def accounting_update(resource: str, resource_id: int | str, wrapper_key: str, data: dict) -> dict:
    """Update an accounting resource."""
    account_id, _ = await get_ids()
    url = f"{ACCOUNTING_BASE}/{account_id}/{resource}/{resource_id}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.put(url, headers=headers, json={wrapper_key: data})
        resp.raise_for_status()
        return resp.json()["response"]["result"]


async def accounting_delete(resource: str, resource_id: int | str) -> bool:
    """Hard-delete an accounting resource (invoices, estimates)."""
    account_id, _ = await get_ids()
    url = f"{ACCOUNTING_BASE}/{account_id}/{resource}/{resource_id}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
        resp.raise_for_status()
        return True


async def accounting_soft_delete(resource: str, resource_id: int | str, wrapper_key: str) -> dict:
    """Soft-delete (vis_state=1) an accounting resource (clients, expenses)."""
    return await accounting_update(resource, resource_id, wrapper_key, {"vis_state": 1})


async def projects_list(
    resource: str,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    """List a projects-type resource (projects, time_entries)."""
    _, business_id = await get_ids()
    url = f"{PROJECTS_BASE}/{business_id}/{resource}"
    params = {"page": page, "per_page": min(per_page, 100)}
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def projects_get(resource: str, resource_id: int | str) -> dict:
    """Get a single projects-type resource."""
    _, business_id = await get_ids()
    url = f"{PROJECTS_BASE}/{business_id}/{resource}/{resource_id}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def projects_create(resource: str, wrapper_key: str, data: dict) -> dict:
    """Create a projects-type resource."""
    _, business_id = await get_ids()
    url = f"{PROJECTS_BASE}/{business_id}/{resource}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json={wrapper_key: data})
        resp.raise_for_status()
        return resp.json()


async def projects_update(resource: str, resource_id: int | str, wrapper_key: str, data: dict) -> dict:
    """Update a projects-type resource."""
    _, business_id = await get_ids()
    url = f"{PROJECTS_BASE}/{business_id}/{resource}/{resource_id}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.put(url, headers=headers, json={wrapper_key: data})
        resp.raise_for_status()
        return resp.json()


async def projects_delete(resource: str, resource_id: int | str) -> bool:
    """Delete a projects-type resource."""
    _, business_id = await get_ids()
    url = f"{PROJECTS_BASE}/{business_id}/{resource}/{resource_id}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
        resp.raise_for_status()
        return True


async def upload_attachment(file_bytes: bytes, filename: str, mime_type: str) -> dict:
    """Upload a file to FreshBooks' attachments endpoint. Returns dict with jwt + media_type."""
    account_id, _ = await get_ids()
    config = get_config()
    token = get_valid_token(config)
    url = f"{UPLOADS_BASE}/{account_id}/attachments"
    headers = {"Authorization": f"Bearer {token}"}
    files = {"content": (filename, file_bytes, mime_type)}
    async with httpx.AsyncClient() as http:
        resp = await http.post(url, headers=headers, files=files)
        resp.raise_for_status()
        return resp.json()["attachment"]


async def get_report(report_type: str, params: dict | None = None) -> dict:
    """Fetch an accounting report."""
    account_id, _ = await get_ids()
    url = f"{ACCOUNTING_BASE}/{account_id}/reports/accounting/{report_type}"
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params or {})
        resp.raise_for_status()
        return resp.json()["response"]["result"]
