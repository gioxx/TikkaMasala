"""Backup detail, download and restore (with preview)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.backups import NOTES_MAX_LENGTH, clean_notes, get_backup, load_backup_json, record_restore
from app.cloudflare import cloudflare_put, fetch_tunnel_configuration
from app.config import BACKUP_DIR, DEMO_MODE
from app.credentials import get_api_credentials, resolve_restore_target
from app.ingress import config_settings_delta, diff_ingress, extract_ingress
from app.notifications import queue_notification, render_notification_message
from app.profiles import current_profile_id
from app.tunnels import back_up_tunnel, fetch_tunnel_list, is_valid_tunnel_id
from app.web import render_backup_page, render_backups_page, templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    return render_backups_page(request)


async def _render_new_backup_page(
    request: Request,
    *,
    selected_tunnel: str | None = None,
    form_notes: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The "Back up with a note" form: a picker over the cached tunnel list and an optional note."""
    creds = get_api_credentials(current_profile_id(request))
    tunnels: list[dict[str, str]] = []
    list_error: str | None = None
    if creds is not None:
        try:
            raw_list = await fetch_tunnel_list(creds)
        except HTTPException as exc:
            list_error = str(exc.detail)
        except Exception:
            logger.exception("Unexpected error while listing tunnels (account_id=%s).", creds.account_id)
            list_error = "Unexpected error while listing tunnels."
        else:
            tunnels = sorted(
                (
                    {"id": str(tunnel["id"]), "name": str(tunnel.get("name") or "Unknown tunnel")}
                    for tunnel in raw_list
                    if isinstance(tunnel, dict) and tunnel.get("id")
                ),
                key=lambda tunnel: (tunnel["name"].casefold(), tunnel["id"]),
            )
    return templates.TemplateResponse(
        request,
        "backup_new.html",
        {
            "credentials_missing": creds is None,
            "list_error": list_error,
            "tunnels": tunnels,
            "selected_tunnel": (selected_tunnel or "").strip().lower() or None,
            "form_notes": form_notes,
            "notes_max_length": NOTES_MAX_LENGTH,
            "error": error,
            "demo_mode": DEMO_MODE,
        },
        status_code=status_code,
    )


@router.get("/backups/new", response_class=HTMLResponse)
async def new_backup_page(request: Request, tunnel: str | None = None) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    return await _render_new_backup_page(request, selected_tunnel=tunnel)


@router.post("/backups/new", response_class=HTMLResponse)
async def new_backup_action(
    request: Request,
    tunnel_id: str = Form(""),
    notes: str = Form(""),
) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    tunnel_id = tunnel_id.strip()

    async def fail(message: str | None, status_code: int) -> HTMLResponse:
        return await _render_new_backup_page(
            request, selected_tunnel=tunnel_id, form_notes=notes, error=message, status_code=status_code
        )

    if not is_valid_tunnel_id(tunnel_id):
        return await fail("Unknown tunnel ID.", 400)
    try:
        cleaned_notes = clean_notes(notes)
    except ValueError as exc:
        return await fail(str(exc), 400)
    creds = get_api_credentials(current_profile_id(request))
    if creds is None:
        # The page shows the credentials notice in place of the form.
        return await fail(None, 400)
    try:
        backup = await back_up_tunnel(creds, tunnel_id.lower(), cleaned_notes)
    except HTTPException as exc:
        return await fail(f"Backup failed: {exc.detail}", 502)
    return RedirectResponse(url=f"/backup/{backup.id}?created=1", status_code=303)


@router.get("/backup/{backup_id}", response_class=HTMLResponse)
async def backup_details(request: Request, backup_id: int, created: str | None = None) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    return render_backup_page(request, backup_id, created=created == "1")


@router.get("/backup/{backup_id}/download", response_model=None)
async def download_backup(backup_id: int) -> FileResponse | RedirectResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    backup = get_backup(backup_id)
    file_path = BACKUP_DIR / backup.filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    logger.info("Backup download requested (backup_id=%s, filename=%s).", backup_id, backup.filename)
    return FileResponse(path=file_path, filename=backup.filename, media_type="application/json")


@router.post("/backup/{backup_id}/restore", response_class=HTMLResponse)
async def restore_backup(
    request: Request,
    backup_id: int,
    tunnel_id: str = Form(...),
    mode: str = Form("apply"),
) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    backup = get_backup(backup_id)
    target = resolve_restore_target(backup.account_id)
    credentials = target.credentials
    if credentials is None:
        return render_backup_page(
            request,
            backup_id,
            error=f"No profile has credentials for account {backup.account_id}. Add them in Settings to restore this backup.",
            form_tunnel_id=tunnel_id.strip() or None,
        )
    account_id = credentials.account_id
    api_token = credentials.api_token
    try:
        logger.info(
            "Restore %s requested (backup_id=%s, account_id=%s, tunnel_id=%s).",
            "preview" if mode == "preview" else "apply",
            backup_id,
            account_id,
            tunnel_id,
        )
        payload = load_backup_json(backup_id)
        configuration = payload.get("configuration", {})
        config_body = configuration.get("config")
        if not isinstance(config_body, dict):
            raise HTTPException(status_code=400, detail="Backup file does not contain a restorable tunnel configuration")
        if mode == "preview":
            _, live_config = await fetch_tunnel_configuration(account_id, tunnel_id, api_token)
            live_result = live_config.get("result", {}) if isinstance(live_config, dict) else {}
            live_config_body = live_result.get("config") if isinstance(live_result, dict) else {}
            diff = diff_ingress(extract_ingress(live_result), extract_ingress(payload))
            settings_delta = config_settings_delta(live_config_body, config_body)
            return render_backup_page(
                request,
                backup_id,
                message="Preview only — nothing was applied.",
                ingress_diff=diff,
                settings_delta=settings_delta,
                form_tunnel_id=tunnel_id,
            )
        await cloudflare_put(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token, {"config": config_body})
        record_restore(backup_id, account_id, tunnel_id)
        logger.info("Restore completed successfully (backup_id=%s, account_id=%s, tunnel_id=%s).", backup_id, account_id, tunnel_id)
        _rs_details = {
            "backup_id": backup_id,
            "account_id": account_id,
            "tunnel_id": tunnel_id,
        }
        queue_notification(
            "restore_success",
            render_notification_message("restore_success", _rs_details),
            _rs_details,
            level="info",
        )
        return render_backup_page(
            request,
            backup_id,
            message="Backup restored successfully.",
            form_tunnel_id=tunnel_id,
        )
    except HTTPException as exc:
        if mode == "preview":
            logger.warning("Restore preview failed (backup_id=%s): %s", backup_id, exc.detail)
            return render_backup_page(
                request,
                backup_id,
                error=exc.detail,
                form_tunnel_id=tunnel_id.strip() or None,
            )
        logger.warning("Restore failed (backup_id=%s): %s", backup_id, exc.detail)
        _rf_details = {
            "backup_id": backup_id,
            "account_id": account_id,
            "tunnel_id": tunnel_id.strip() or None,
            "error": str(exc.detail),
        }
        queue_notification(
            "restore_failed",
            render_notification_message("restore_failed", _rf_details),
            _rf_details,
            level="warning",
        )
        return render_backup_page(
            request,
            backup_id,
            error=exc.detail,
            form_tunnel_id=tunnel_id.strip() or None,
        )
