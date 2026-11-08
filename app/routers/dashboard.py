"""Dashboard: tunnel listing and manual backup with the profile's saved credentials."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.backups import create_backup
from app.cloudflare import list_tunnels
from app.config import BACKUP_DIR, BACKUPS_PAGE_SIZE, DEMO_MODE
from app.credentials import require_api_credentials
from app.notifications import queue_notification, render_notification_message
from app.profiles import current_profile_id
from app.web import render_index_page

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=BACKUPS_PAGE_SIZE),
    message: str | None = None,
    error: str | None = None,
    success_target: str | None = "tunnels",
) -> HTMLResponse:
    return render_index_page(
        request,
        message=message,
        error=error,
        success_target=success_target,
        backup_page=page,
        backup_page_size=page_size,
    )


@router.post("/list-tunnels", response_class=HTMLResponse)
async def list_tunnels_action(request: Request) -> HTMLResponse:
    profile_id = current_profile_id(request)
    try:
        creds = require_api_credentials(profile_id)
        logger.info("Tunnel listing requested (account_id=%s).", creds.account_id)
        tunnels = await list_tunnels(creds.account_id, creds.api_token)
        logger.info("Tunnel listing completed (account_id=%s, tunnels=%s).", creds.account_id, len(tunnels))
        return render_index_page(
            request,
            message=f"Loaded {len(tunnels)} tunnel(s).",
            success_target="tunnels",
            tunnels=tunnels,
        )
    except HTTPException as exc:
        logger.warning("Tunnel listing failed: %s", exc.detail)
        return await index(request, error=exc.detail, success_target="tunnels")


@router.post("/backup", response_model=None)
async def backup_action(
    request: Request,
    tunnel_id: str = Form(...),
    notes: str | None = Form(default=None),
) -> HTMLResponse | FileResponse:
    profile_id = current_profile_id(request)
    account_id: str | None = None
    try:
        creds = require_api_credentials(profile_id)
        account_id = creds.account_id
        logger.info("Manual backup requested (account_id=%s, tunnel_id=%s).", account_id, tunnel_id)
        backup = await create_backup(account_id, tunnel_id, creds.api_token, notes)
        logger.info(
            "Manual backup created successfully (backup_id=%s, account_id=%s, tunnel_id=%s, tunnel_name=%s).",
            backup.id,
            account_id,
            tunnel_id,
            backup.tunnel_name,
        )
        _mbs_details = {
            "backup_id": backup.id,
            "account_id": account_id,
            "tunnel_id": tunnel_id,
            "tunnel_name": backup.tunnel_name,
            "route_count": backup.route_count,
        }
        queue_notification(
            "manual_backup_success",
            render_notification_message("manual_backup_success", _mbs_details),
            _mbs_details,
            level="info",
        )
        if DEMO_MODE:
            file_path = BACKUP_DIR / backup.filename
            return FileResponse(path=file_path, filename=backup.filename, media_type="application/json")
        return await index(
            request,
            message=f"Backup created: #{backup.id} for tunnel {backup.tunnel_name}.",
            success_target="backup",
        )
    except HTTPException as exc:
        logger.warning("Manual backup failed (tunnel_id=%s): %s", tunnel_id, exc.detail)
        _mbf_details = {
            "account_id": account_id,
            "tunnel_id": tunnel_id.strip() or None,
            "error": str(exc.detail),
        }
        queue_notification(
            "manual_backup_failed",
            render_notification_message("manual_backup_failed", _mbf_details),
            _mbf_details,
            level="warning",
        )
        return await index(request, error=exc.detail, success_target="backup")
