"""Dashboard: token check, tunnel listing, manual backup, saved-auth reset."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.backups import create_backup
from app.cloudflare import list_tunnels, verify_token
from app.config import BACKUP_DIR, BACKUPS_PAGE_SIZE, DEFAULT_ACCOUNT_ID, DEFAULT_API_TOKEN, DEMO_MODE
from app.credentials import (
    build_prefill_status_from_sources,
    clear_api_token_cookie,
    get_account_id_source,
    get_saved_account_id,
    remember_account_id,
    remember_api_token,
    resolve_account_id,
    resolve_api_token,
    set_api_token_cookie,
)
from app.db import delete_setting
from app.notifications import queue_notification, render_notification_message
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
    prefill_status: dict[str, Any] | None = None,
    prefill_account_id: str | None = None,
    prefill_api_token: str | None = None,
) -> HTMLResponse:
    return render_index_page(
        request,
        message=message,
        error=error,
        success_target=success_target,
        prefill_status=prefill_status,
        prefill_account_id=prefill_account_id,
        prefill_api_token=prefill_api_token,
        backup_page=page,
        backup_page_size=page_size,
    )


@router.post("/verify-token", response_class=HTMLResponse)
async def verify_token_action(request: Request, api_token: str = Form(default="")) -> HTMLResponse:
    try:
        api_token = resolve_api_token(request, api_token)
        logger.info("Token verification requested.")
        result = await verify_token(api_token)
        status = result.get("result", {}).get("status", "unknown")
        message = f"Token verified successfully. Status: {status}."
        logger.info("Token verification completed successfully (status=%s).", status)
        remember_api_token(api_token)
        response = await index(
            request,
            message=message,
            success_target="guide",
            prefill_status=build_prefill_status_from_sources(get_account_id_source(), "browser"),
            prefill_account_id=get_saved_account_id(),
            prefill_api_token=api_token,
        )
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        logger.warning("Token verification failed: %s", exc.detail)
        return await index(request, error=exc.detail, success_target="guide")


@router.post("/list-tunnels", response_class=HTMLResponse)
async def list_tunnels_action(request: Request, account_id: str = Form(default=""), api_token: str = Form(default="")) -> HTMLResponse:
    try:
        account_id = resolve_account_id(account_id)
        api_token = resolve_api_token(request, api_token)
        logger.info("Tunnel listing requested (account_id=%s).", account_id)
        tunnels = await list_tunnels(account_id, api_token)
        logger.info("Tunnel listing completed (account_id=%s, tunnels=%s).", account_id, len(tunnels))
        remember_account_id(account_id)
        remember_api_token(api_token)
        response = render_index_page(
            request,
            message=f"Loaded {len(tunnels)} tunnel(s).",
            success_target="tunnels",
            tunnels=tunnels,
            prefill_status=build_prefill_status_from_sources("database", "browser"),
            prefill_account_id=account_id,
            prefill_api_token=api_token,
        )
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        logger.warning("Tunnel listing failed: %s", exc.detail)
        return await index(request, error=exc.detail, success_target="tunnels")


@router.post("/backup", response_model=None)
async def backup_action(
    request: Request,
    account_id: str = Form(default=""),
    tunnel_id: str = Form(...),
    api_token: str = Form(default=""),
    notes: str | None = Form(default=None),
) -> HTMLResponse | FileResponse:
    try:
        account_id = resolve_account_id(account_id)
        api_token = resolve_api_token(request, api_token)
        logger.info("Manual backup requested (account_id=%s, tunnel_id=%s).", account_id, tunnel_id)
        backup = await create_backup(account_id, tunnel_id, api_token, notes)
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
        remember_account_id(account_id)
        remember_api_token(api_token)
        if DEMO_MODE:
            file_path = BACKUP_DIR / backup.filename
            return FileResponse(path=file_path, filename=backup.filename, media_type="application/json")
        response = await index(
            request,
            message=f"Backup created: #{backup.id} for tunnel {backup.tunnel_name}.",
            success_target="backup",
            prefill_status=build_prefill_status_from_sources("database", "browser"),
            prefill_account_id=account_id,
            prefill_api_token=api_token,
        )
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        logger.warning("Manual backup failed (tunnel_id=%s): %s", tunnel_id, exc.detail)
        _mbf_details = {
            "account_id": account_id.strip() or None,
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


@router.post("/clear-saved-auth", response_class=HTMLResponse)
async def clear_saved_auth(request: Request) -> HTMLResponse:
    logger.info("Clearing saved authentication data from database and browser cookie.")
    delete_setting("account_id")
    delete_setting("api_token")
    response = await index(
        request,
        message="Saved authentication data removed from the database. Backups were left untouched.",
        success_target="guide",
        prefill_status=build_prefill_status_from_sources(
            "environment" if DEFAULT_ACCOUNT_ID else None,
            "environment" if DEFAULT_API_TOKEN else None,
        ),
        prefill_account_id=DEFAULT_ACCOUNT_ID,
        prefill_api_token=DEFAULT_API_TOKEN,
    )
    clear_api_token_cookie(response)
    return response
