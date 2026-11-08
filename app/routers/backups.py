"""Backup detail, download and restore (with preview)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.backups import get_backup, load_backup_json, record_restore
from app.cloudflare import cloudflare_put, fetch_tunnel_configuration
from app.config import BACKUP_DIR, DEMO_MODE
from app.credentials import resolve_restore_target
from app.ingress import config_settings_delta, diff_ingress, extract_ingress
from app.notifications import queue_notification, render_notification_message
from app.web import render_backup_page, render_backups_page

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    return render_backups_page(request)


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
