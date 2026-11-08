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
from app.profiles import DEFAULT_PROFILE_ID, get_profile, update_profile

logger = logging.getLogger(__name__)


def _env_account_id(profile_id: int) -> str:
    """Environment fallback: only the Default profile ever reads ``CLOUDFLARE_ACCOUNT_ID``."""
    return DEFAULT_ACCOUNT_ID.strip() if profile_id == DEFAULT_PROFILE_ID else ""


def _env_api_token(profile_id: int) -> str:
    """Environment fallback: only the Default profile ever reads ``CLOUDFLARE_API_TOKEN``."""
    return DEFAULT_API_TOKEN.strip() if profile_id == DEFAULT_PROFILE_ID else ""


def _stored_account_id(profile_id: int) -> str:
    return (get_profile(profile_id).account_id or "").strip()


def get_saved_account_id(profile_id: int) -> str:
    if DEMO_MODE:
        return _env_account_id(profile_id)
    return _stored_account_id(profile_id) or _env_account_id(profile_id)


def get_account_id_source(profile_id: int) -> str | None:
    if DEMO_MODE:
        return "environment" if _env_account_id(profile_id) else None
    if _stored_account_id(profile_id):
        return "database"
    if _env_account_id(profile_id):
        return "environment"
    return None


def resolve_account_id(profile_id: int, account_id: str | None) -> str:
    resolved = (account_id or "").strip() or get_saved_account_id(profile_id)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="Account ID is required. Set it in the form or via CLOUDFLARE_ACCOUNT_ID.",
        )
    return resolved


def remember_account_id(profile_id: int, account_id: str) -> None:
    if DEMO_MODE:
        return
    normalized = account_id.strip()
    if normalized:
        update_profile(profile_id, account_id=normalized)


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


def _read_stored_api_token(profile_id: int) -> str | None:
    """Return the profile's saved token in clear, or ``None`` if unset or unreadable.

    A token still stored in plaintext (legacy install) is re-saved encrypted
    when an encryption key is configured.
    """
    stored_token = (get_profile(profile_id).api_token or "").strip()
    if not stored_token:
        return None
    try:
        decrypted = decrypt_secret(stored_token).strip()
    except RuntimeError:
        logger.exception(
            "Saved API token of profile %s is not readable with the current key; falling back.",
            profile_id,
        )
        return None
    if decrypted and not stored_token.startswith(ENCRYPTED_VALUE_PREFIX):
        try:
            remember_api_token(profile_id, decrypted)
        except RuntimeError:
            logger.exception("Unable to encrypt the plaintext API token of profile %s.", profile_id)
    return decrypted or None


def get_saved_api_token(request: Request, profile_id: int) -> str:
    if DEMO_MODE:
        return _env_api_token(profile_id)
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return cookie_token
    return get_server_api_token(profile_id)


def get_api_token_source(request: Request, profile_id: int) -> str | None:
    if DEMO_MODE:
        return "environment" if _env_api_token(profile_id) else None
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return "browser"
    return get_server_api_token_source(profile_id)


def get_server_api_token(profile_id: int) -> str:
    """The token for server-side work (no browser cookie): database first, then environment."""
    if DEMO_MODE:
        return _env_api_token(profile_id)
    return _read_stored_api_token(profile_id) or _env_api_token(profile_id)


def get_server_api_token_source(profile_id: int) -> str | None:
    if DEMO_MODE:
        return "environment" if _env_api_token(profile_id) else None
    if _read_stored_api_token(profile_id):
        return "database"
    if _env_api_token(profile_id):
        return "environment"
    return None


def resolve_api_token(request: Request, profile_id: int, api_token: str | None) -> str:
    resolved = (api_token or "").strip() or get_saved_api_token(request, profile_id)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="API token is required. Set it in the form or via CLOUDFLARE_API_TOKEN.",
        )
    return resolved


def remember_api_token(profile_id: int, api_token: str) -> None:
    if DEMO_MODE:
        return
    normalized = api_token.strip()
    if normalized and TOKEN_ENCRYPTION_KEY:
        update_profile(profile_id, api_token=encrypt_secret(normalized))


def clear_saved_credentials(profile_id: int) -> None:
    """Forget the Account ID and API token saved on the profile (environment values stay)."""
    update_profile(profile_id, account_id=None, api_token=None)


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


def build_prefill_status(request: Request, profile_id: int) -> dict[str, Any]:
    return build_prefill_status_from_sources(
        get_account_id_source(profile_id), get_api_token_source(request, profile_id)
    )
