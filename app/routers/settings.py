"""Settings page: profile credentials, Cloudflare Access status and app info."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.access import cross_site_rejected, is_cross_site_request
from app.cloudflare import CredentialsInvalid
from app.config import DEMO_MODE
from app.credentials import clear_saved_credentials, describe_profile_credentials, save_profile_credentials
from app.encryption import encryption_key_source
from app.profiles import get_profile, list_profiles
from app.web import templates

router = APIRouter()

SETTINGS_PATH = "/settings"

NOTICES = {
    "saved": "Credentials verified and saved.",
    "unchanged": "Nothing changed.",
    "cleared": "Saved credentials removed. Values from environment variables, if any, are used now.",
}

KEY_SOURCE_LABELS = {
    "environment": "From TOKEN_ENCRYPTION_KEY",
    "file": "Generated, stored as encryption.key in the data directory",
    "missing": "Not created yet (created when a token is first saved)",
    "invalid": "Invalid: check TOKEN_ENCRYPTION_KEY or encryption.key",
}


def _render_settings(
    request: Request,
    *,
    errors: dict[int, str] | None = None,
    form_values: dict[int, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    verifier = getattr(request.app.state, "access_verifier", None)
    key_source = encryption_key_source()
    notice = next((text for key, text in NOTICES.items() if request.query_params.get(key) == "1"), None)
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
            "errors": errors or {},
            "form_values": form_values or {},
        },
        status_code=status_code,
    )


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
    if is_cross_site_request(request):
        return cross_site_rejected()
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
    return RedirectResponse(f"{SETTINGS_PATH}?{'saved' if changed else 'unchanged'}=1", status_code=303)


@router.post(f"{SETTINGS_PATH}/profiles/{{profile_id}}/clear-credentials", response_model=None)
async def clear_credentials(request: Request, profile_id: int) -> Response:
    if is_cross_site_request(request):
        return cross_site_rejected()
    if DEMO_MODE:
        return RedirectResponse(SETTINGS_PATH, status_code=303)
    _ensure_profile(profile_id)
    clear_saved_credentials(profile_id)
    return RedirectResponse(f"{SETTINGS_PATH}?cleared=1", status_code=303)
