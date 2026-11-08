"""Automatic backup settings, the APScheduler job and the scheduled-runs log."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException

from app.backups import create_backup, get_last_backup_time_for_tunnel
from app.cloudflare import list_tunnels
from app.config import (
    AUTO_BACKUP_JOB_ID,
    AUTO_BACKUP_TIMEZONE,
    DEFAULT_AUTO_BACKUP_CRON,
    SCHEDULED_RUNS_PAGE_SIZE,
    SCHEDULED_RUNS_PAGE_SIZE_OPTIONS,
)
from app.credentials import (
    get_account_id_source,
    get_saved_account_id,
    get_server_api_token,
    get_server_api_token_source,
)
from app.db import ScheduledRunRecord, db, get_setting, set_setting
from app.notifications import get_notification_status, queue_notification, render_notification_message
from app.profiles import DEFAULT_PROFILE_ID

logger = logging.getLogger(__name__)
auto_backup_scheduler = AsyncIOScheduler(timezone="UTC")
auto_backup_lock = asyncio.Lock()


def get_auto_backup_enabled() -> bool:
    return (get_setting("auto_backup_enabled") or "").strip().lower() in {"1", "true", "yes", "on"}


def get_auto_backup_cron() -> str:
    return (get_setting("auto_backup_cron") or DEFAULT_AUTO_BACKUP_CRON).strip()


def set_auto_backup_enabled(enabled: bool) -> None:
    set_setting("auto_backup_enabled", "1" if enabled else "0")


def set_auto_backup_cron(cron_expression: str) -> None:
    set_setting("auto_backup_cron", cron_expression.strip())


def normalize_timezone_name(timezone_name: str) -> str:
    normalized = timezone_name.strip()
    if not normalized:
        raise ValueError("Timezone is required.")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {normalized}") from exc
    return normalized


def get_saved_auto_backup_timezone() -> str | None:
    value = (get_setting("auto_backup_timezone") or "").strip()
    if not value:
        return None
    try:
        return normalize_timezone_name(value)
    except ValueError:
        logger.exception("Saved auto-backup timezone is invalid; falling back to environment/default timezone.")
        return None


def set_auto_backup_timezone(timezone_name: str) -> None:
    set_setting("auto_backup_timezone", normalize_timezone_name(timezone_name))


def get_auto_backup_timezone_name() -> str:
    if AUTO_BACKUP_TIMEZONE:
        return normalize_timezone_name(AUTO_BACKUP_TIMEZONE)
    return get_saved_auto_backup_timezone() or "UTC"


def get_auto_backup_timezone() -> ZoneInfo:
    return ZoneInfo(get_auto_backup_timezone_name())


def get_auto_backup_timezone_source() -> str:
    if AUTO_BACKUP_TIMEZONE:
        return "environment"
    if get_saved_auto_backup_timezone():
        return "browser"
    return "default"


DEFAULT_TUNNEL_SCHEDULE: dict[str, Any] = {"mode": "all", "tunnels": {}}
TUNNEL_FREQUENCY_OPTIONS = ("always", "weekly", "monthly")


def get_auto_backup_tunnel_schedule() -> dict[str, Any]:
    raw = (get_setting("auto_backup_tunnel_schedule") or "").strip()
    if not raw:
        return dict(DEFAULT_TUNNEL_SCHEDULE)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return dict(DEFAULT_TUNNEL_SCHEDULE)
        return parsed
    except json.JSONDecodeError:
        return dict(DEFAULT_TUNNEL_SCHEDULE)


def set_auto_backup_tunnel_schedule(config: dict[str, Any]) -> None:
    set_setting("auto_backup_tunnel_schedule", json.dumps(config, ensure_ascii=False))


def validate_cron_expression(cron_expression: str) -> str:
    normalized = cron_expression.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Cron expression is required.")
    try:
        CronTrigger.from_crontab(normalized, timezone=get_auto_backup_timezone())
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {exc}") from exc
    return normalized


def get_auto_backup_prerequisites() -> dict[str, Any]:
    account_id = get_saved_account_id(DEFAULT_PROFILE_ID)
    account_source = get_account_id_source(DEFAULT_PROFILE_ID)
    api_token = get_server_api_token(DEFAULT_PROFILE_ID)
    api_token_source = get_server_api_token_source(DEFAULT_PROFILE_ID)
    missing_items: list[str] = []
    if not account_id:
        missing_items.append("Server-side Account ID")
    if not api_token:
        missing_items.append("Server-side API token")
    return {
        "account_id_available": bool(account_id),
        "account_id_source": account_source,
        "api_token_available": bool(api_token),
        "api_token_source": api_token_source,
        "ready": bool(account_id and api_token),
        "missing_items": missing_items,
    }


def create_scheduled_run(account_id: str, status: str, details: str | None = None) -> int:
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(db()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO scheduled_runs (started_at, status, account_id, details)
            VALUES (?, ?, ?, ?)
            """,
            (started_at, status, account_id, details),
        )
        conn.commit()
        return int(cursor.lastrowid)


def complete_scheduled_run(
    run_id: int,
    status: str,
    tunnel_count: int,
    backup_count: int,
    error_count: int,
    details: str | None = None,
) -> None:
    finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(db()) as conn:
        conn.execute(
            """
            UPDATE scheduled_runs
            SET finished_at = ?, status = ?, tunnel_count = ?, backup_count = ?, error_count = ?, details = ?
            WHERE id = ?
            """,
            (finished_at, status, tunnel_count, backup_count, error_count, details, run_id),
        )
        conn.commit()


def get_recent_scheduled_runs(limit: int = 5) -> list[ScheduledRunRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, status, account_id, tunnel_count, backup_count, error_count, details
            FROM scheduled_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [ScheduledRunRecord(**dict(row)) for row in rows]


def get_all_scheduled_runs() -> list[ScheduledRunRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, status, account_id, tunnel_count, backup_count, error_count, details
            FROM scheduled_runs
            ORDER BY id DESC
            """
        ).fetchall()
    return [ScheduledRunRecord(**dict(row)) for row in rows]


def get_scheduled_runs_stats() -> dict[str, int]:
    with closing(db()) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS run_count,
                COALESCE(SUM(backup_count), 0) AS total_backup_count
            FROM scheduled_runs
            """
        ).fetchone()
    return {
        "run_count": int(row["run_count"] or 0),
        "total_backup_count": int(row["total_backup_count"] or 0),
    }


def get_scheduled_runs_page(page: int, page_size: int | None = SCHEDULED_RUNS_PAGE_SIZE) -> tuple[list[ScheduledRunRecord], dict[str, Any]]:
    page = max(page, 1)

    with closing(db()) as conn:
        total_count = int(conn.execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0])

        if page_size is None:
            rows = conn.execute(
                """
                SELECT id, started_at, finished_at, status, account_id, tunnel_count, backup_count, error_count, details
                FROM scheduled_runs
                ORDER BY id DESC
                """
            ).fetchall()
            runs = [ScheduledRunRecord(**dict(row)) for row in rows]
            return runs, {
                "current_page": 1,
                "page_size": total_count,
                "page_size_param": "all",
                "total_count": total_count,
                "total_pages": 1,
                "has_previous": False,
                "has_next": False,
                "previous_page": 1,
                "next_page": 1,
                "start_index": 1 if total_count else 0,
                "end_index": total_count,
                "is_all": True,
            }

        resolved_page_size = page_size if page_size in SCHEDULED_RUNS_PAGE_SIZE_OPTIONS else SCHEDULED_RUNS_PAGE_SIZE
        total_pages = max((total_count + resolved_page_size - 1) // resolved_page_size, 1)
        current_page = min(page, total_pages)
        offset = (current_page - 1) * resolved_page_size

        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, status, account_id, tunnel_count, backup_count, error_count, details
            FROM scheduled_runs
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (resolved_page_size, offset),
        ).fetchall()

    runs = [ScheduledRunRecord(**dict(row)) for row in rows]
    start_index = offset + 1 if total_count and runs else 0
    end_index = offset + len(runs)

    return runs, {
        "current_page": current_page,
        "page_size": resolved_page_size,
        "page_size_param": str(resolved_page_size),
        "total_count": total_count,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
        "previous_page": current_page - 1,
        "next_page": current_page + 1,
        "start_index": start_index,
        "end_index": end_index,
        "is_all": False,
    }


def summarize_scheduled_run_details(details: str | None) -> str | None:
    if not details:
        return None
    try:
        payload = json.loads(details)
    except json.JSONDecodeError:
        return details
    if isinstance(payload, dict):
        if isinstance(payload.get("message"), str):
            return payload["message"]
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = str(first.get("message") or "Unknown error")
                if len(errors) > 1:
                    return f"{message} (+{len(errors) - 1} more)"
                return message
    return details


def get_next_auto_backup_run() -> str | None:
    job = auto_backup_scheduler.get_job(AUTO_BACKUP_JOB_ID)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def get_last_auto_backup_run_at() -> str | None:
    value = (get_setting("auto_backup_last_run_at") or "").strip()
    return value or None


def get_last_auto_backup_status() -> str | None:
    value = (get_setting("auto_backup_last_status") or "").strip()
    return value or None


def build_auto_backup_status() -> dict[str, Any]:
    prereqs = get_auto_backup_prerequisites()
    enabled = get_auto_backup_enabled()
    cron_expression = get_auto_backup_cron()
    timezone_name = get_auto_backup_timezone_name()
    timezone_source = get_auto_backup_timezone_source()
    tunnel_schedule = get_auto_backup_tunnel_schedule()
    tunnel_schedule_mode = tunnel_schedule.get("mode", "all")
    tunnel_schedule_count = len(tunnel_schedule.get("tunnels", {}))
    return {
        "enabled": enabled,
        "cron": cron_expression,
        "timezone_name": timezone_name,
        "timezone_source": timezone_source,
        "timezone_locked_by_env": timezone_source == "environment",
        "last_run_at": get_last_auto_backup_run_at(),
        "last_status": get_last_auto_backup_status(),
        "next_run_at": get_next_auto_backup_run(),
        "recent_runs": get_recent_scheduled_runs(),
        "prereqs": prereqs,
        "notifications": get_notification_status(),
        "tunnel_schedule_mode": tunnel_schedule_mode,
        "tunnel_schedule_count": tunnel_schedule_count,
    }


def configure_auto_backup_job() -> None:
    existing_job = auto_backup_scheduler.get_job(AUTO_BACKUP_JOB_ID)
    if existing_job is not None:
        auto_backup_scheduler.remove_job(AUTO_BACKUP_JOB_ID)

    if not get_auto_backup_enabled():
        return

    if not get_auto_backup_prerequisites()["ready"]:
        logger.warning("Automatic backups are enabled but server-side prerequisites are missing; scheduler job not registered.")
        return

    cron_expression = get_auto_backup_cron()
    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=get_auto_backup_timezone())
    except ValueError:
        logger.exception("Skipping auto-backup scheduler registration because the cron expression is invalid.")
        return

    auto_backup_scheduler.add_job(
        run_auto_backup_job,
        trigger=trigger,
        id=AUTO_BACKUP_JOB_ID,
        replace_existing=True,
        kwargs={"trigger": "schedule"},
        coalesce=True,
        misfire_grace_time=300,
        max_instances=1,
    )


async def run_auto_backup_job(trigger: str = "schedule") -> dict[str, Any]:
    if auto_backup_lock.locked():
        logger.info("Automatic backup run skipped because another run is already in progress.")
        details = json.dumps({"message": "Skipped because another automatic backup is already running."}, ensure_ascii=False)
        run_id = create_scheduled_run(get_saved_account_id(DEFAULT_PROFILE_ID) or "unknown", "skipped", details)
        complete_scheduled_run(run_id, "skipped", 0, 0, 0, details)
        set_setting("auto_backup_last_run_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        set_setting("auto_backup_last_status", "skipped")
        return {"status": "skipped", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

    async with auto_backup_lock:
        account_id = get_saved_account_id(DEFAULT_PROFILE_ID)
        api_token = get_server_api_token(DEFAULT_PROFILE_ID)
        if not account_id or not api_token:
            logger.warning("Automatic backup run skipped because server-side credentials are missing.")
            details = json.dumps(
                {"message": "Missing server-side account ID or API token for automatic backups."},
                ensure_ascii=False,
            )
            run_id = create_scheduled_run(account_id or "unknown", "skipped", details)
            complete_scheduled_run(run_id, "skipped", 0, 0, 0, details)
            set_setting("auto_backup_last_run_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
            set_setting("auto_backup_last_status", "skipped")
            return {"status": "skipped", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

        run_id = create_scheduled_run(account_id, "running")
        logger.info("Starting automatic backup run (trigger=%s, account_id=%s).", trigger, account_id)
        tunnel_count = 0
        backup_count = 0
        errors: list[dict[str, str]] = []
        backed_up_names: list[str] = []
        skipped_items: list[dict[str, str]] = []
        details: str | None = None
        status = "failed"

        try:
            tunnels = await list_tunnels(account_id, api_token)
            tunnel_count = len(tunnels)
            note = f"Automatic backup triggered by {trigger}."

            tunnel_schedule = get_auto_backup_tunnel_schedule()
            schedule_mode = tunnel_schedule.get("mode", "all")
            tunnel_overrides: dict[str, Any] = tunnel_schedule.get("tunnels", {})
            now = datetime.now(timezone.utc)

            for tunnel in tunnels:
                tunnel_id = str(tunnel.get("id") or "").strip()
                tunnel_name = str(tunnel.get("name") or "Unknown tunnel").strip()
                if not tunnel_id:
                    errors.append({"tunnel": tunnel_name, "message": "Tunnel ID missing in API response."})
                    continue

                if schedule_mode == "selected" and tunnel_id not in tunnel_overrides:
                    logger.debug("Skipping tunnel %s (%s): not in selected-tunnel list.", tunnel_id, tunnel_name)
                    skipped_items.append({"tunnel": tunnel_name, "reason": "not selected"})
                    continue

                tunnel_cfg = tunnel_overrides.get(tunnel_id, {})
                frequency = tunnel_cfg.get("frequency", "always")
                if frequency in ("weekly", "monthly"):
                    days_threshold = 7 if frequency == "weekly" else 30
                    last_backup = get_last_backup_time_for_tunnel(tunnel_id)
                    if last_backup and (now - last_backup).days < days_threshold:
                        logger.debug(
                            "Skipping tunnel %s (%s): last backup was %s day(s) ago (threshold: %s).",
                            tunnel_id, tunnel_name, (now - last_backup).days, days_threshold,
                        )
                        skipped_items.append({"tunnel": tunnel_name, "reason": f"{frequency} cooldown"})
                        continue

                try:
                    await create_backup(account_id, tunnel_id, api_token, note)
                    backup_count += 1
                    backed_up_names.append(tunnel_name)
                except HTTPException as exc:
                    errors.append({"tunnel": tunnel_name, "tunnel_id": tunnel_id, "message": str(exc.detail)})

            if errors and backup_count:
                status = "partial"
            elif errors and not backup_count:
                status = "failed"
            else:
                status = "success"
        except HTTPException as exc:
            errors.append({"message": str(exc.detail)})
            status = "failed"
        except Exception as exc:
            logger.exception("Unexpected error during automatic backup run.")
            errors.append({"message": f"Unexpected error: {exc}"})
            status = "failed"

        if errors:
            details = json.dumps({"errors": errors}, ensure_ascii=False)

        complete_scheduled_run(run_id, status, tunnel_count, backup_count, len(errors), details)
        set_setting("auto_backup_last_run_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        set_setting("auto_backup_last_status", status)
        logger.info(
            "Automatic backup run completed (trigger=%s, status=%s, tunnels=%s, backups=%s, errors=%s).",
            trigger,
            status,
            tunnel_count,
            backup_count,
            len(errors),
        )
        if status in {"success", "partial", "failed"}:
            _ab_details = {
                "trigger": trigger,
                "account_id": account_id,
                "tunnel_count": tunnel_count,
                "backup_count": backup_count,
                "error_count": len(errors),
                "skipped_count": len(skipped_items),
                "processed_count": backup_count + len(errors),
                "backed_up_tunnels": ", ".join(backed_up_names) if backed_up_names else "—",
                "skipped_tunnels": ", ".join(f"{s['tunnel']} ({s['reason']})" for s in skipped_items) if skipped_items else "—",
                "errors": errors,
            }
            queue_notification(
                f"auto_backup_{status}",
                render_notification_message(f"auto_backup_{status}", _ab_details),
                _ab_details,
                level="warning" if status in {"partial", "failed"} else "info",
            )
        return {
            "status": status,
            "tunnel_count": tunnel_count,
            "backup_count": backup_count,
            "error_count": len(errors),
        }
