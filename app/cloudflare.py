"""Thin async client for the Cloudflare Tunnel API."""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import API_BASE, REQUEST_TIMEOUT

# The Account ID is interpolated into API URL paths: accept only the dashboard format.
ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


class CredentialsInvalid(Exception):
    """Credentials were rejected; ``message`` is safe to show to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def headers(api_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def cloudflare_get(account_id: str, path: str, api_token: str) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers(api_token))
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=message)
    return data


async def cloudflare_put(account_id: str, path: str, api_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.put(url, headers=headers(api_token), json=payload)
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=message)
    return data


async def verify_token(api_token: str) -> dict[str, Any]:
    url = f"{API_BASE}/user/tokens/verify"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers(api_token))
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=f"Token verification failed: {message}")
    return data


async def fetch_tunnel_configuration(account_id: str, tunnel_id: str, api_token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    tunnel_data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}", api_token)
    config_data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token)
    return tunnel_data, config_data


TUNNELS_PER_PAGE = 50
MAX_TUNNEL_PAGES = 100  # safety stop: 5000 tunnels


async def list_tunnels(account_id: str, api_token: str) -> list[dict[str, Any]]:
    """Return every non-deleted tunnel of the account, following the API's pagination.

    Without ``is_deleted=false`` Cloudflare also returns deleted tunnels, which
    cannot be backed up or inspected.
    """

    tunnels: list[dict[str, Any]] = []
    for page in range(1, MAX_TUNNEL_PAGES + 1):
        data = await cloudflare_get(account_id, f"cfd_tunnel?is_deleted=false&page={page}&per_page={TUNNELS_PER_PAGE}", api_token)
        result = data.get("result", [])
        if not isinstance(result, list):
            break
        tunnels.extend(result)
        info = data.get("result_info")
        total_pages = info.get("total_pages") if isinstance(info, dict) else None
        if isinstance(total_pages, int):
            if page >= total_pages:
                break
        elif len(result) < TUNNELS_PER_PAGE:
            break
    return tunnels


async def check_credentials(account_id: str, api_token: str) -> int:
    """Validate an Account ID / API token pair against Cloudflare and return the tunnel count.

    Raises ``CredentialsInvalid`` with a user-facing message. ``verify_token`` and
    ``list_tunnels`` are looked up as module globals so tests can patch them.
    """
    if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
        raise CredentialsInvalid(
            "Account ID must be the 32-character hexadecimal ID shown in the Cloudflare dashboard."
        )
    try:
        try:
            await verify_token(api_token)
        except HTTPException as exc:
            raise CredentialsInvalid(str(exc.detail)) from exc
        try:
            tunnels = await list_tunnels(account_id, api_token)
        except HTTPException as exc:
            raise CredentialsInvalid(
                f"Token is valid but cannot list tunnels for this account: {exc.detail}"
            ) from exc
    except httpx.HTTPError as exc:
        raise CredentialsInvalid(f"Could not reach the Cloudflare API ({type(exc).__name__}).") from exc
    except ValueError as exc:
        raise CredentialsInvalid("Unexpected response from the Cloudflare API.") from exc
    return len(tunnels)


def extract_error_message(payload: dict[str, Any]) -> str:
    errors = payload.get("errors") or []
    if errors:
        first = errors[0]
        code = first.get("code")
        message = first.get("message", "Unknown Cloudflare API error")
        return f"{message} (code {code})" if code else message
    messages = payload.get("messages") or []
    if messages:
        first = messages[0]
        return first.get("message", "Unknown Cloudflare API response")
    return "Unknown Cloudflare API error"
