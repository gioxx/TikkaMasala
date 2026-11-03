"""Account ID / API token resolution, encryption at rest and the token cookie."""

from __future__ import annotations

import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    API_TOKEN_COOKIE_MAX_AGE,
    API_TOKEN_COOKIE_NAME,
    DEFAULT_ACCOUNT_ID,
    DEFAULT_API_TOKEN,
    DEMO_MODE,
    ENCRYPTED_VALUE_PREFIX,
    TOKEN_ENCRYPTION_KEY,
)
from app.db import get_setting, set_setting

logger = logging.getLogger(__name__)


def get_saved_account_id() -> str:
    if DEMO_MODE:
        return DEFAULT_ACCOUNT_ID
    return (get_setting("account_id") or DEFAULT_ACCOUNT_ID).strip()


def get_account_id_source() -> str | None:
    if DEMO_MODE:
        return "environment" if DEFAULT_ACCOUNT_ID else None
    stored_value = (get_setting("account_id") or "").strip()
    if stored_value:
        return "database"
    if DEFAULT_ACCOUNT_ID:
        return "environment"
    return None


def resolve_account_id(account_id: str | None) -> str:
    resolved = (account_id or "").strip() or get_saved_account_id()
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="Account ID is required. Set it in the form or via CLOUDFLARE_ACCOUNT_ID.",
        )
    return resolved


def remember_account_id(account_id: str) -> None:
    if DEMO_MODE:
        return
    normalized = account_id.strip()
    if normalized:
        set_setting("account_id", normalized)


def get_fernet() -> Fernet | None:
    if not TOKEN_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode("utf-8"))
    except (ValueError, TypeError):
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is invalid. Provide a valid Fernet key.")


def encrypt_secret(value: str) -> str:
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to persist API tokens securely.")
    token = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    if not value.startswith(ENCRYPTED_VALUE_PREFIX):
        return value
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to read encrypted API tokens from the database.")
    encrypted = value.removeprefix(ENCRYPTED_VALUE_PREFIX).encode("utf-8")
    try:
        return fernet.decrypt(encrypted).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Failed to decrypt stored API token. Check TOKEN_ENCRYPTION_KEY.") from exc


def get_saved_api_token(request: Request) -> str:
    if DEMO_MODE:
        return DEFAULT_API_TOKEN.strip()
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return cookie_token
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypted = decrypt_secret(stored_token).strip()
            if decrypted and not stored_token.startswith(ENCRYPTED_VALUE_PREFIX):
                remember_api_token(decrypted)
            return decrypted
        except RuntimeError:
            logger.exception("Unable to decrypt saved API token; falling back to environment/default prefill.")
    return DEFAULT_API_TOKEN.strip()


def get_api_token_source(request: Request) -> str | None:
    if DEMO_MODE:
        return "environment" if DEFAULT_API_TOKEN else None
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return "browser"
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypt_secret(stored_token)
            return "database"
        except RuntimeError:
            logger.exception("Saved API token exists in the database but is not readable with the current key.")
    if DEFAULT_API_TOKEN:
        return "environment"
    return None


def get_server_api_token() -> str:
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            return decrypt_secret(stored_token).strip()
        except RuntimeError:
            logger.exception("Unable to decrypt saved API token for server-side scheduler; falling back to environment token.")
    return DEFAULT_API_TOKEN.strip()


def get_server_api_token_source() -> str | None:
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypt_secret(stored_token)
            return "database"
        except RuntimeError:
            logger.exception("Saved API token exists in the database but is not readable for the scheduler.")
    if DEFAULT_API_TOKEN:
        return "environment"
    return None


def resolve_api_token(request: Request, api_token: str | None) -> str:
    resolved = (api_token or "").strip() or get_saved_api_token(request)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="API token is required. Set it in the form or via CLOUDFLARE_API_TOKEN.",
        )
    return resolved


def remember_api_token(api_token: str) -> None:
    if DEMO_MODE:
        return
    normalized = api_token.strip()
    if normalized and TOKEN_ENCRYPTION_KEY:
        set_setting("api_token", encrypt_secret(normalized))


def set_api_token_cookie(response: HTMLResponse | RedirectResponse, api_token: str) -> None:
    if DEMO_MODE:
        return
    response.set_cookie(
        key=API_TOKEN_COOKIE_NAME,
        value=api_token.strip(),
        max_age=API_TOKEN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_api_token_cookie(response: HTMLResponse | RedirectResponse) -> None:
    response.delete_cookie(key=API_TOKEN_COOKIE_NAME, samesite="lax")


def build_prefill_status_from_sources(account_source: str | None, api_token_source: str | None) -> dict[str, Any]:
    return {
        "account_id_source": account_source,
        "api_token_source": api_token_source,
        "has_prefill": bool(account_source or api_token_source),
        "uses_environment_prefill": "environment" in {account_source, api_token_source},
        "uses_database_prefill": "database" in {account_source, api_token_source},
        "uses_browser_prefill": api_token_source == "browser",
        "can_persist_api_token": bool(TOKEN_ENCRYPTION_KEY),
    }


def build_prefill_status(request: Request) -> dict[str, Any]:
    return build_prefill_status_from_sources(get_account_id_source(), get_api_token_source(request))
