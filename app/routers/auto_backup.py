"""Automatic backup settings, manual run, tunnel filters and the runs archive."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.cloudflare import list_tunnels
from app.config import AUTO_BACKUP_TIMEZONE, DEFAULT_AUTO_BACKUP_CRON, DEMO_MODE
from app.credentials import get_saved_account_id, get_server_api_token
from app.scheduler import (
    TUNNEL_FREQUENCY_OPTIONS,
    configure_auto_backup_job,
    get_auto_backup_prerequisites,
    get_auto_backup_timezone_name,
    get_auto_backup_tunnel_schedule,
    run_auto_backup_job,
    set_auto_backup_cron,
    set_auto_backup_enabled,
    set_auto_backup_timezone,
    set_auto_backup_tunnel_schedule,
    validate_cron_expression,
)
from app.web import render_index_page, render_scheduled_runs_page, templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/auto-backup/settings", response_class=HTMLResponse)
async def auto_backup_settings_action(
    request: Request,
    enabled: str | None = Form(default=None),
    cron_expression: str = Form(default=DEFAULT_AUTO_BACKUP_CRON),
    browser_timezone: str = Form(default=""),
) -> HTMLResponse:
    if DEMO_MODE:
        return render_index_page(request, error="Automatic backups are not available in Demo mode.", success_target="auto-backup")
    try:
        logger.info("Automatic backup settings update requested.")
        if not AUTO_BACKUP_TIMEZONE and browser_timezone.strip():
            try:
                set_auto_backup_timezone(browser_timezone)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        normalized_cron = validate_cron_expression(cron_expression)
        enabled_flag = enabled == "on"
        prereqs = get_auto_backup_prerequisites()

        if enabled_flag and not prereqs["ready"]:
            raise HTTPException(
                status_code=400,
                detail="Automatic backups require a server-side Account ID and API token from the database or environment.",
            )

        set_auto_backup_cron(normalized_cron)
        set_auto_backup_enabled(enabled_flag)
        configure_auto_backup_job()
        logger.info(
            "Automatic backup settings updated (enabled=%s, cron=%s, timezone=%s).",
            enabled_flag,
            normalized_cron,
            get_auto_backup_timezone_name(),
        )

        message = "Automatic backups enabled." if enabled_flag else "Automatic backups disabled."
        return render_index_page(request, message=message, success_target="auto-backup")
    except HTTPException as exc:
        logger.warning("Automatic backup settings update failed: %s", exc.detail)
        return render_index_page(request, error=exc.detail, success_target="auto-backup")


@router.post("/auto-backup/run", response_class=HTMLResponse)
async def auto_backup_run_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return render_index_page(request, error="Automatic backups are not available in Demo mode.", success_target="auto-backup")
    try:
        logger.info("Manual automatic-backup run requested from UI.")
        prereqs = get_auto_backup_prerequisites()
        if not prereqs["ready"]:
            raise HTTPException(
                status_code=400,
                detail="Automatic backups require a server-side Account ID and API token from the database or environment.",
            )

        result = await run_auto_backup_job(trigger="manual")
        status = result["status"]
        logger.info(
            "Manual automatic-backup run finished (status=%s, tunnels=%s, backups=%s, errors=%s).",
            status,
            result["tunnel_count"],
            result["backup_count"],
            result["error_count"],
        )
        if status == "success":
            message = f"Automatic backup run completed: {result['backup_count']} backup(s) created from {result['tunnel_count']} tunnel(s)."
            return render_index_page(request, message=message, success_target="auto-backup")
        elif status == "partial":
            error = (
                f"Automatic backup run completed with errors: {result['backup_count']} backup(s) created, "
                f"{result['error_count']} error(s)."
            )
            return render_index_page(request, error=error, success_target="auto-backup")
        elif status == "skipped":
            message = "Automatic backup run skipped because another run is already in progress."
            return render_index_page(request, message=message, success_target="auto-backup")
        else:
            error = (
                f"Automatic backup run failed: {result['error_count']} error(s), "
                f"{result['backup_count']} backup(s) created."
            )
            return render_index_page(request, error=error, success_target="auto-backup")
    except HTTPException as exc:
        logger.warning("Manual automatic-backup run failed before execution: %s", exc.detail)
        return render_index_page(request, error=exc.detail, success_target="auto-backup")


@router.get("/auto-backup/tunnel-filters", response_class=HTMLResponse)
async def auto_backup_tunnel_filters_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/#auto-backup-section", status_code=303)
    prereqs = get_auto_backup_prerequisites()
    tunnels: list[dict[str, Any]] = []
    load_error: str | None = None
    if prereqs["ready"]:
        account_id = get_saved_account_id()
        api_token = get_server_api_token()
        try:
            tunnels = await list_tunnels(account_id, api_token)  # type: ignore[arg-type]
        except HTTPException as exc:
            load_error = f"Could not load tunnels: {exc.detail}"
        except Exception:
            logger.exception("Unexpected error loading tunnels for tunnel-filters page.")
            load_error = "Unexpected error loading tunnels from Cloudflare API."
    current_schedule = get_auto_backup_tunnel_schedule()
    return templates.TemplateResponse(
        request,
        "tunnel_filters.html",
        {
            "tunnels": tunnels,
            "load_error": load_error,
            "prereqs": prereqs,
            "schedule": current_schedule,
            "frequency_options": TUNNEL_FREQUENCY_OPTIONS,
            "demo_mode": DEMO_MODE,
        },
    )


@router.post("/auto-backup/tunnel-filters", response_class=HTMLResponse)
async def auto_backup_tunnel_filters_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/#auto-backup-section", status_code=303)
    try:
        form = await request.form()
        mode = str(form.get("mode", "all")).strip()
        if mode not in ("all", "selected"):
            mode = "all"

        tunnel_ids_raw = form.getlist("tunnel_ids")
        tunnel_ids = [t.strip() for t in tunnel_ids_raw if t.strip()]

        tunnels_cfg: dict[str, Any] = {}
        for tid in tunnel_ids:
            freq = str(form.get(f"freq_{tid}", "always")).strip()
            if freq not in TUNNEL_FREQUENCY_OPTIONS:
                freq = "always"
            name = str(form.get(f"name_{tid}", "")).strip()
            if mode == "all":
                if freq != "always":
                    tunnels_cfg[tid] = {"name": name, "frequency": freq}
            else:
                included = form.get(f"include_{tid}")
                if included:
                    tunnels_cfg[tid] = {"name": name, "frequency": freq}

        new_schedule: dict[str, Any] = {"mode": mode, "tunnels": tunnels_cfg}
        set_auto_backup_tunnel_schedule(new_schedule)
        logger.info(
            "Tunnel backup schedule updated (mode=%s, tunnel_overrides=%s).",
            mode,
            len(tunnels_cfg),
        )
        return RedirectResponse(url="/#auto-backup-section", status_code=303)
    except Exception:
        logger.exception("Unexpected error saving tunnel backup schedule.")
        return RedirectResponse(url="/auto-backup/tunnel-filters?error=1", status_code=303)


@router.get("/auto-backup/runs", response_class=HTMLResponse)
async def auto_backup_runs(request: Request) -> HTMLResponse:
    return render_scheduled_runs_page(request)
