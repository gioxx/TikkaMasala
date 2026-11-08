"""Settings page: profiles (add, rename, remove, credentials), Cloudflare Access status and app info."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.backups import get_backup_retention_status
from app.cloudflare import CredentialsInvalid
from app.config import DEMO_MODE
from app.credentials import (
    clear_saved_credentials,
    create_profile_with_credentials,
    describe_profile_credentials,
    save_profile_credentials,
)
from app.encryption import encryption_key_source
from app.profile_admin import (
    REMOVE_BUSY_MESSAGE,
    SHARED_BACKUPS_MESSAGE,
    ProfileBusy,
    RemovalPreview,
    removal_preview,
    remove_profile,
)
from app.profiles import (
    ACTIVE_PROFILE_COOKIE,
    DEFAULT_NOT_REMOVABLE,
    DEFAULT_PROFILE_ID,
    PROFILE_NAME_MAX,
    get_profile,
    list_profiles,
    parse_profile_id,
    rename_profile,
)
from app.scheduler import configure_auto_backup_job
from app.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()

SETTINGS_PATH = "/settings"

NOTICES = {
    "saved": "Credentials verified and saved.",
    "renamed": "Profile renamed.",
    "unchanged": "Nothing changed.",
    "cleared": "Saved credentials removed. Values from environment variables, if any, are used now.",
}

KEY_SOURCE_LABELS = {
    "environment": "From TOKEN_ENCRYPTION_KEY",
    "file": "Generated, stored as encryption.key in the data directory",
    "missing": "Not created yet (created when a token is first saved)",
    "invalid": "Invalid: check TOKEN_ENCRYPTION_KEY or encryption.key",
}


def _query_int(request: Request, key: str) -> int | None:
    """A non-negative whole number from the query string, else ``None``."""
    raw = request.query_params.get(key, "")
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 10 else None


def _settings_notice(request: Request) -> tuple[str | None, dict[str, object] | None]:
    """The notice text, plus the profile to offer a switch to after an add."""
    added_id = parse_profile_id(request.query_params.get("added", ""))
    if added_id is not None:
        try:
            added = get_profile(added_id)
        except LookupError:
            return None, None
        return f"Profile '{added.name}' added.", {"id": added.id, "name": added.name}
    if request.query_params.get("removed") == "1":
        deleted = _query_int(request, "deleted")
        if deleted is None:
            return None, None
        if deleted == 0:
            return "Profile removed. Its backups were kept under Backups → Other accounts.", None
        return f"Profile removed together with {deleted} backup(s).", None
    return next((text for key, text in NOTICES.items() if request.query_params.get(key) == "1"), None), None


def _render_settings(
    request: Request,
    *,
    errors: dict[int, str] | None = None,
    form_values: dict[int, str] | None = None,
    rename_open: int | None = None,
    rename_value: str = "",
    add_error: str | None = None,
    add_values: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    verifier = getattr(request.app.state, "access_verifier", None)
    key_source = encryption_key_source()
    notice, added_profile = _settings_notice(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "profiles": [describe_profile_credentials(profile.id) for profile in list_profiles()],
            "access_enabled": verifier is not None,
            "access_team_domain": verifier.settings.team_domain if verifier is not None else None,
            "key_source": key_source,
            "key_source_label": KEY_SOURCE_LABELS.get(key_source, key_source),
            "demo_mode": DEMO_MODE,
            "notice": notice,
            "added_profile": added_profile,
            "errors": errors or {},
            "form_values": form_values or {},
            "rename_open": rename_open,
            "rename_value": rename_value,
            "add_error": add_error,
            "add_values": add_values or {},
            "default_profile_id": DEFAULT_PROFILE_ID,
            "profile_name_max": PROFILE_NAME_MAX,
        },
        status_code=status_code,
    )


def _reconfigure_job(profile_id: int) -> None:
    """Re-sync the profile's job; the credentials are already saved, so a scheduler error must not fail the request."""
    try:
        configure_auto_backup_job(profile_id)
    except Exception:
        logger.exception("Could not reconfigure the automatic backup job of profile %s.", profile_id)


def _ensure_profile(profile_id: int) -> None:
    try:
        get_profile(profile_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Profile not found.") from None


@router.get(SETTINGS_PATH, response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return _render_settings(request)


@router.post(f"{SETTINGS_PATH}/profiles/{{profile_id}}/credentials", response_model=None)
async def save_credentials(
    request: Request,
    profile_id: int,
    account_id: str = Form(""),
    api_token: str = Form(""),
) -> Response:
    if DEMO_MODE:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    _ensure_profile(profile_id)
    account_id = account_id.strip()
    try:
        changed = await save_profile_credentials(profile_id, account_id=account_id, api_token=api_token)
    except CredentialsInvalid as exc:
        # The submitted Account ID is echoed back; the token never is.
        return _render_settings(
            request,
            errors={profile_id: exc.message},
            form_values={profile_id: account_id},
            status_code=400,
        )
    _reconfigure_job(profile_id)
    return RedirectResponse(f"{SETTINGS_PATH}?{'saved' if changed else 'unchanged'}=1", status_code=303)


@router.post(f"{SETTINGS_PATH}/profiles/{{profile_id}}/clear-credentials", response_model=None)
async def clear_credentials(request: Request, profile_id: int) -> Response:
    if DEMO_MODE:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    _ensure_profile(profile_id)
    clear_saved_credentials(profile_id)
    _reconfigure_job(profile_id)
    return RedirectResponse(f"{SETTINGS_PATH}?cleared=1", status_code=303)


@router.post(f"{SETTINGS_PATH}/profiles", response_model=None)
async def add_profile(
    request: Request,
    name: str = Form(""),
    account_id: str = Form(""),
    api_token: str = Form(""),
) -> Response:
    if DEMO_MODE:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    try:
        profile = await create_profile_with_credentials(name, account_id, api_token)
    except CredentialsInvalid as exc:
        # Name and Account ID are echoed back; the token never is.
        return _render_settings(
            request,
            add_error=exc.message,
            add_values={"name": name.strip(), "account_id": account_id.strip()},
            status_code=400,
        )
    return RedirectResponse(f"{SETTINGS_PATH}?added={profile.id}#profile-{profile.id}", status_code=303)


@router.post(f"{SETTINGS_PATH}/profiles/{{profile_id}}/name", response_model=None)
async def rename_profile_submit(request: Request, profile_id: int, name: str = Form("")) -> Response:
    if DEMO_MODE:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    _ensure_profile(profile_id)
    try:
        renamed = rename_profile(profile_id, name)
    except LookupError:
        raise HTTPException(status_code=404, detail="Profile not found.") from None
    except ValueError as exc:
        return _render_settings(
            request,
            errors={profile_id: str(exc)},
            rename_open=profile_id,
            rename_value=name.strip(),
            status_code=400,
        )
    return RedirectResponse(f"{SETTINGS_PATH}?renamed=1#profile-{renamed.id}", status_code=303)


def _render_remove_page(
    request: Request, preview: RemovalPreview, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "profile_remove.html",
        {
            "preview": preview,
            "other_retention": get_backup_retention_status(None),
            "shared_backups_message": SHARED_BACKUPS_MESSAGE,
            "error": error,
        },
        status_code=status_code,
    )


def _preview_or_404(profile_id: int) -> RemovalPreview:
    try:
        return removal_preview(profile_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Profile not found.") from None


@router.get(f"{SETTINGS_PATH}/profiles/{{profile_id}}/remove", response_class=HTMLResponse, response_model=None)
async def remove_profile_page(request: Request, profile_id: int) -> Response:
    if DEMO_MODE or profile_id == DEFAULT_PROFILE_ID:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    return _render_remove_page(request, _preview_or_404(profile_id))


@router.post(f"{SETTINGS_PATH}/profiles/{{profile_id}}/remove", response_model=None)
async def remove_profile_submit(request: Request, profile_id: int, delete_backups: str = Form("")) -> Response:
    # ``async def`` on purpose: ``remove_profile`` is synchronous and runs on the event loop, so the
    # run-lock check and the delete cannot interleave with a run starting. Never move it to a threadpool.
    if DEMO_MODE:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    if profile_id == DEFAULT_PROFILE_ID:
        return _render_settings(request, errors={DEFAULT_PROFILE_ID: DEFAULT_NOT_REMOVABLE}, status_code=400)
    _preview_or_404(profile_id)
    asked = delete_backups.strip()
    expected_backup_count: int | None = None
    if asked:
        if not (asked.isascii() and asked.isdigit() and len(asked) <= 10):
            raise HTTPException(status_code=400, detail="Invalid backup count.")
        expected_backup_count = int(asked)
    # Read before the removal: once the profile is gone the cookie no longer parses.
    cookie_names_removed = parse_profile_id(request.cookies.get(ACTIVE_PROFILE_COOKIE)) == profile_id
    try:
        deleted = remove_profile(
            profile_id, delete_backups=bool(asked), expected_backup_count=expected_backup_count
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Profile not found.") from None
    except ProfileBusy:
        return _render_remove_page(request, _preview_or_404(profile_id), error=REMOVE_BUSY_MESSAGE, status_code=409)
    except ValueError as exc:  # BackupCountChanged or shared backups: show the fresh numbers.
        return _render_remove_page(request, _preview_or_404(profile_id), error=str(exc), status_code=409)
    response = RedirectResponse(f"{SETTINGS_PATH}?removed=1&deleted={deleted}", status_code=303)
    if cookie_names_removed:
        response.delete_cookie(ACTIVE_PROFILE_COOKIE, path="/", httponly=True, samesite="lax")
    return response
