"""Automation: schedule, Run now, notification test, tunnel filters and the runs archive."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.background import finish_or_continue
from app.backups import RETENTION_INPUT_ERROR, get_backup_retention_status, parse_retention_days_input
from app.config import AUTO_BACKUP_TIMEZONE, DEFAULT_AUTO_BACKUP_CRON, DEMO_MODE
from app.credentials import get_api_credentials
from app.destinations import list_destinations
from app.notifications import deliver_notification, env_delivery_targets, render_notification_message
from app.profiles import DEFAULT_PROFILE_ID, current_profile_id, update_profile
from app.tunnels import fetch_tunnel_list
from app.scheduler import (
    TUNNEL_FREQUENCY_OPTIONS,
    configure_all_auto_backup_jobs,
    configure_auto_backup_job,
    get_auto_backup_prerequisites,
    get_auto_backup_timezone_name,
    get_auto_backup_tunnel_schedule,
    get_saved_auto_backup_timezone,
    normalize_timezone_name,
    run_auto_backup_job,
    set_auto_backup_cron,
    set_auto_backup_enabled,
    set_auto_backup_timezone,
    set_auto_backup_tunnel_schedule,
    validate_cron_expression,
)
from app.web import render_automation_page, render_scheduled_runs_page, templates

logger = logging.getLogger(__name__)

router = APIRouter()


MISSING_CREDENTIALS_MESSAGE = (
    "Automatic backups require a server-side Account ID and API token from the database or environment."
)
RUN_SKIPPED_MESSAGE = "Automatic backup run skipped because another backup run is already in progress."
RUN_CONTINUES_MESSAGE = (
    "The run is taking a while and continues in the background. Its result will appear in the run history."
)


def _home() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=303)


@router.get("/automation", response_class=HTMLResponse)
async def automation_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    return render_automation_page(request)


@router.post("/automation/schedule", response_class=HTMLResponse)
async def automation_schedule_action(
    request: Request,
    enabled: str | None = Form(default=None),
    cron_expression: str = Form(default=DEFAULT_AUTO_BACKUP_CRON),
    browser_timezone: str = Form(default=""),
) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    logger.info("Automatic backup settings update requested.")
    profile_id = current_profile_id(request)
    enabled_flag = enabled == "on"
    try:
        # Every check runs before anything is written: a rejected form changes nothing.
        timezone_name: str | None = None
        if not AUTO_BACKUP_TIMEZONE and browser_timezone.strip():
            try:
                timezone_name = normalize_timezone_name(browser_timezone)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        normalized_cron = validate_cron_expression(cron_expression)
        if enabled_flag and not get_auto_backup_prerequisites(profile_id)["ready"]:
            raise HTTPException(status_code=400, detail=MISSING_CREDENTIALS_MESSAGE)
    except HTTPException as exc:
        logger.warning("Automatic backup settings update failed: %s", exc.detail)
        return render_automation_page(request, notice=("schedule", "error", str(exc.detail)), status_code=400)

    timezone_changed = timezone_name is not None and timezone_name != get_saved_auto_backup_timezone()
    if timezone_changed:
        set_auto_backup_timezone(timezone_name)  # type: ignore[arg-type]
    set_auto_backup_cron(profile_id, normalized_cron)
    set_auto_backup_enabled(profile_id, enabled_flag)
    if timezone_changed:
        # The timezone is global: every profile's cron trigger depends on it.
        configure_all_auto_backup_jobs()
    else:
        configure_auto_backup_job(profile_id)
    logger.info(
        "Automatic backup settings updated (enabled=%s, cron=%s, timezone=%s).",
        enabled_flag,
        normalized_cron,
        get_auto_backup_timezone_name(),
    )
    message = "Automatic backups enabled." if enabled_flag else "Automatic backups disabled."
    return render_automation_page(request, notice=("schedule", "success", message))


@router.post("/automation/retention", response_class=HTMLResponse)
async def automation_retention_action(request: Request, retention_days: str = Form(default="")) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    profile_id = current_profile_id(request)
    try:
        value = parse_retention_days_input(retention_days)
    except ValueError:
        return render_automation_page(
            request,
            notice=("retention", "error", RETENTION_INPUT_ERROR),
            retention_input=retention_days,
            status_code=400,
        )
    update_profile(profile_id, retention_days=value)
    logger.info("Backup retention of profile %s set to %s.", profile_id, value if value is not None else "default")
    if value is None:
        label = get_backup_retention_status(profile_id)["label"]
        message = f"Retention reset to the default: {label}."
    else:
        message = (
            f"Backups of this profile are now kept for {value} days. "
            "Older ones are removed when the next backup is saved."
        )
    return render_automation_page(request, notice=("retention", "success", message))


def _run_notice(result: dict[str, Any]) -> tuple[str, str, str]:
    status = result["status"]
    if status == "success":
        return (
            "run",
            "success",
            f"Automatic backup run completed: {result['backup_count']} backup(s) created from {result['tunnel_count']} tunnel(s).",
        )
    if status == "partial":
        return (
            "run",
            "error",
            f"Automatic backup run completed with errors: {result['backup_count']} backup(s) created, "
            f"{result['error_count']} error(s).",
        )
    if status == "skipped":
        return ("run", "muted", RUN_SKIPPED_MESSAGE)
    return (
        "run",
        "error",
        f"Automatic backup run failed: {result['error_count']} error(s), {result['backup_count']} backup(s) created.",
    )


@router.post("/automation/run", response_class=HTMLResponse)
async def automation_run_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    logger.info("Manual automatic-backup run requested from UI.")
    profile_id = current_profile_id(request)
    if not get_auto_backup_prerequisites(profile_id)["ready"]:
        logger.warning("Manual automatic-backup run failed before execution: %s", MISSING_CREDENTIALS_MESSAGE)
        return render_automation_page(request, notice=("run", "error", MISSING_CREDENTIALS_MESSAGE), status_code=400)

    done, result = await finish_or_continue(
        run_auto_backup_job(profile_id, trigger="manual"), name=f"auto-backup-run-{profile_id}"
    )
    if not done or result is None:
        logger.info("Manual automatic-backup run of profile %s continues in the background.", profile_id)
        return render_automation_page(request, notice=("run", "muted", RUN_CONTINUES_MESSAGE))
    logger.info(
        "Manual automatic-backup run finished (status=%s, tunnels=%s, backups=%s, errors=%s).",
        result["status"],
        result["tunnel_count"],
        result["backup_count"],
        result["error_count"],
    )
    return render_automation_page(request, notice=_run_notice(result))


@router.post("/automation/notifications/test", response_class=HTMLResponse)
async def automation_notifications_test_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    profile_id = current_profile_id(request)
    destinations = list_destinations(profile_id)
    channel_labels = [target.label for target in env_delivery_targets()] if profile_id == DEFAULT_PROFILE_ID else []
    channel_labels += [d.label for d in destinations if d.enabled and not d.unreadable]
    if not channel_labels and destinations:
        logger.warning("Notification test requested, but no destination of profile %s is usable.", profile_id)
        return render_automation_page(
            request,
            notice=(
                "notifications",
                "error",
                "No enabled destinations to test. Enable one or add a new destination.",
            ),
        )
    if not channel_labels:
        logger.warning("Notification test requested, but profile %s has no notification destination.", profile_id)
        return render_automation_page(
            request,
            notice=(
                "notifications",
                "error",
                "Notification test failed: no configured channel found. Add a destination first.",
            ),
        )

    logger.info("Notification test requested from UI (profile_id=%s, destinations=%s).", profile_id, len(channel_labels))
    details: dict[str, Any] = {"configured_channels": ", ".join(channel_labels)}
    result = await deliver_notification(
        "notification_test",
        render_notification_message("notification_test", details, profile_id=profile_id),
        details,
        level="info",
        force=True,
        profile_id=profile_id,
    )
    logger.info(
        "Notification test finished (profile_id=%s, sent=%s, failed=%s).",
        profile_id,
        result["sent_count"],
        len(result["failed_channels"]),
    )
    if result["sent_count"]:
        message = f"Notification test sent successfully via {', '.join(result['successful_channels'])}."
        if result["failed_channels"]:
            message += f" Failed channels: {', '.join(result['failed_channels'])}."
        return render_automation_page(request, notice=("notifications", "success", message))

    return render_automation_page(
        request,
        notice=("notifications", "error", "Notification test failed. Check the container logs for channel-specific errors."),
    )


def _tunnels_notice(request: Request) -> tuple[str, str] | None:
    if request.query_params.get("saved") == "1":
        return ("success", "Advanced schedule saved.")
    if request.query_params.get("error") == "1":
        return ("error", "The advanced schedule could not be saved. Please try again.")
    return None


@router.get("/automation/tunnels", response_class=HTMLResponse)
async def automation_tunnels_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    profile_id = current_profile_id(request)
    prereqs = get_auto_backup_prerequisites(profile_id)
    tunnels: list[dict[str, Any]] = []
    load_error: str | None = None
    creds = get_api_credentials(profile_id) if prereqs["ready"] else None
    if creds is not None:
        try:
            tunnels = await fetch_tunnel_list(creds)
        except HTTPException as exc:
            load_error = f"Could not load tunnels: {exc.detail}"
        except Exception:
            logger.exception("Unexpected error loading tunnels for the tunnel schedule page.")
            load_error = "Unexpected error loading tunnels from Cloudflare API."
    return templates.TemplateResponse(
        request,
        "tunnel_filters.html",
        {
            "tunnels": tunnels,
            "load_error": load_error,
            "prereqs": prereqs,
            "schedule": get_auto_backup_tunnel_schedule(profile_id),
            "frequency_options": TUNNEL_FREQUENCY_OPTIONS,
            "notice": _tunnels_notice(request),
            "demo_mode": DEMO_MODE,
        },
    )


@router.post("/automation/tunnels", response_class=HTMLResponse)
async def automation_tunnels_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
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
        set_auto_backup_tunnel_schedule(current_profile_id(request), new_schedule)
        logger.info(
            "Tunnel backup schedule updated (mode=%s, tunnel_overrides=%s).",
            mode,
            len(tunnels_cfg),
        )
        return RedirectResponse(url="/automation/tunnels?saved=1", status_code=303)
    except Exception:
        logger.exception("Unexpected error saving tunnel backup schedule.")
        return RedirectResponse(url="/automation/tunnels?error=1", status_code=303)


@router.get("/automation/runs", response_class=HTMLResponse)
async def automation_runs_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return _home()
    return render_scheduled_runs_page(request)
