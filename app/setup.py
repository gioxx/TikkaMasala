"""First-run setup: the setup screen, the setup gate and the one-time cookie migration.

The gate runs as HTTP middleware *inside* the Cloudflare Access middleware: a
request first passes Access, then (if it still carries the legacy token cookie)
the cookie token is moved into the database, then the request is sent to
``/setup`` while the Default profile has no usable credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import credentials
from app.cloudflare import CredentialsInvalid
from app.config import API_TOKEN_COOKIE_NAME, DEMO_MODE
from app.credentials import describe_profile_credentials, get_api_credentials, save_profile_credentials
from app.encryption import EncryptionKeyError
from app.profiles import DEFAULT_PROFILE_ID

logger = logging.getLogger(__name__)

SETUP_PATH = "/setup"
GATE_EXEMPT_PATHS = frozenset({"/healthz", SETUP_PATH})
GATE_EXEMPT_PREFIXES = ("/static/",)

router = APIRouter()


def setup_required() -> bool:
    """Whether the Default profile lacks usable credentials (never in demo mode)."""
    return not DEMO_MODE and get_api_credentials(DEFAULT_PROFILE_ID) is None


def migrate_cookie_token(token: str) -> bool:
    """Save the legacy cookie token on the Default profile if it has no readable token.

    Skipped when ``CLOUDFLARE_API_TOKEN`` is set: the old app copied that token into the
    cookie, so saving it would pin it in the database and hide later env changes.
    Returns whether the token was saved. Never logs the token.
    """
    token = (token or "").strip()
    if DEMO_MODE or not token:
        return False
    if credentials._env_api_token(DEFAULT_PROFILE_ID):
        return False
    if credentials._read_stored_api_token(DEFAULT_PROFILE_ID):
        return False
    try:
        credentials.remember_api_token(DEFAULT_PROFILE_ID, token)
    except EncryptionKeyError as exc:
        logger.warning("Unable to migrate the API token from the browser cookie: %s", exc)
        return False
    logger.info("Migrated the API token from the browser cookie into the database.")
    return True


def _default_has_usable_token() -> bool:
    """Whether the Default profile already has a token besides the cookie (env or readable DB copy)."""
    return bool(credentials._env_api_token(DEFAULT_PROFILE_ID) or credentials._read_stored_api_token(DEFAULT_PROFILE_ID))


def settle_cookie_token(token: str) -> bool:
    """Try to migrate the legacy cookie token; return whether the cookie can now be deleted.

    The cookie is only dropped when it is no longer the sole copy of the token: it was
    migrated, another usable token exists for Default, demo mode ignores it, or it is empty.
    When migration fails (e.g. a broken encryption key) the cookie is kept so a later
    request retries once the key is fixed.
    """
    if migrate_cookie_token(token):
        return True
    if DEMO_MODE or not (token or "").strip():
        return True
    return _default_has_usable_token()


def _is_exempt(path: str) -> bool:
    return path in GATE_EXEMPT_PATHS or path.startswith(GATE_EXEMPT_PREFIXES)


def _setup_redirect(request: Request) -> Response:
    if request.headers.get("HX-Request"):
        # htmx would swap a followed redirect into the page: ask it to navigate instead.
        return Response(status_code=200, headers={"HX-Redirect": SETUP_PATH})
    return RedirectResponse(SETUP_PATH, status_code=303)


async def credentials_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if _is_exempt(request.url.path):
        return await call_next(request)
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME)
    drop_cookie = cookie_token is not None and settle_cookie_token(cookie_token)
    if setup_required():
        response = _setup_redirect(request)
    else:
        response = await call_next(request)
    if drop_cookie:
        response.delete_cookie(API_TOKEN_COOKIE_NAME, samesite="lax")
    return response


def _render_setup(
    request: Request,
    *,
    error: str | None = None,
    account_id: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    from app.web import templates  # deferred: app.web pulls in the whole template stack

    view = describe_profile_credentials(DEFAULT_PROFILE_ID)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "error": error,
            "account_id": account_id if account_id is not None else view.account_id.value,
            "account_id_in_env": view.account_id.env_value_present,
            "api_token_in_env": view.api_token.env_value_present,
            "token_available": bool(view.api_token.value),
            "token_hint": view.api_token.value,
            "saved_token_unreadable": view.saved_token_unreadable,
        },
        status_code=status_code,
    )


@router.get(SETUP_PATH, response_model=None)
async def setup_page(request: Request) -> Response:
    if DEMO_MODE or not setup_required():
        return RedirectResponse("/", status_code=303)
    return _render_setup(request)


@router.post(SETUP_PATH, response_model=None)
async def setup_submit(
    request: Request,
    account_id: str = Form(""),
    api_token: str = Form(""),
) -> Response:
    # Cross-site posts are rejected by app.access.cross_site_guard.
    # Setup is for the first run only: once credentials work, changes go through Settings.
    if DEMO_MODE or not setup_required():
        return RedirectResponse("/", status_code=303)
    account_id = account_id.strip()
    if not account_id:
        return _render_setup(request, error="Account ID is required.", account_id=account_id, status_code=400)
    if not api_token.strip() and not describe_profile_credentials(DEFAULT_PROFILE_ID).api_token.value:
        return _render_setup(
            request, error="Account ID and API token are required.", account_id=account_id, status_code=400
        )
    try:
        await save_profile_credentials(DEFAULT_PROFILE_ID, account_id=account_id, api_token=api_token)
    except CredentialsInvalid as exc:
        return _render_setup(request, error=exc.message, account_id=account_id, status_code=400)
    return RedirectResponse("/", status_code=303)
