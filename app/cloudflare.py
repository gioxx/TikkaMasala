"""Thin async client for the Cloudflare Tunnel API."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException

from app.config import API_BASE, REQUEST_TIMEOUT


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


async def list_tunnels(account_id: str, api_token: str) -> list[dict[str, Any]]:
    data = await cloudflare_get(account_id, "cfd_tunnel", api_token)
    result = data.get("result", [])
    if isinstance(result, list):
        return result
    return result if isinstance(result, list) else []


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
