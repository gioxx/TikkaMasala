"""Webhook / Telegram delivery and customizable notification messages."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import (
    APP_VERSION,
    DEFAULT_NOTIFICATION_EVENTS,
    NOTIFICATION_WEBHOOK_EVENTS_RAW,
    NOTIFICATION_WEBHOOK_URL,
    REQUEST_TIMEOUT,
    SUPPORTED_NOTIFICATION_EVENTS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_NOTIFICATION_EVENTS_RAW,
)
from app.db import get_setting, set_setting

logger = logging.getLogger(__name__)
background_tasks: set[asyncio.Task[Any]] = set()


@lru_cache(maxsize=1)
def parse_notification_events(raw_value: str, source_name: str) -> set[str]:
    if not raw_value:
        return set(DEFAULT_NOTIFICATION_EVENTS)

    requested_events = {
        item.strip().lower().replace("-", "_")
        for item in raw_value.split(",")
        if item.strip()
    }
    valid_events = requested_events & SUPPORTED_NOTIFICATION_EVENTS
    invalid_events = requested_events - SUPPORTED_NOTIFICATION_EVENTS
    if invalid_events:
        logger.warning(
            "Ignoring unsupported notification events for %s: %s",
            source_name,
            ", ".join(sorted(invalid_events)),
        )
    return valid_events


@lru_cache(maxsize=1)
def get_webhook_notification_events() -> set[str]:
    return parse_notification_events(NOTIFICATION_WEBHOOK_EVENTS_RAW, "webhook")


@lru_cache(maxsize=1)
def get_telegram_notification_events() -> set[str]:
    return parse_notification_events(TELEGRAM_NOTIFICATION_EVENTS_RAW, "telegram")


def get_notification_status() -> dict[str, Any]:
    webhook_enabled = bool(NOTIFICATION_WEBHOOK_URL)
    telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    webhook_events = get_webhook_notification_events() if webhook_enabled else set()
    telegram_events = get_telegram_notification_events() if telegram_enabled else set()
    configured = (webhook_enabled and bool(webhook_events)) or (telegram_enabled and bool(telegram_events))
    active_events = sorted(webhook_events | telegram_events)

    channel_labels: list[str] = []
    if webhook_enabled and webhook_events:
        channel_labels.append("Webhook")
    if telegram_enabled and telegram_events:
        channel_labels.append("Telegram")

    if channel_labels:
        summary_label = " + ".join(channel_labels)
        summary = f"{summary_label} notifications enabled for {len(active_events)} event(s)."
    elif webhook_enabled or telegram_enabled:
        summary_label = "Incomplete"
        summary = "A notification channel is configured, but no valid notification events are enabled."
    else:
        summary_label = "Disabled"
        summary = "Notifications are disabled."

    return {
        "webhook_enabled": webhook_enabled,
        "telegram_enabled": telegram_enabled,
        "configured": configured,
        "summary_label": summary_label,
        "channel_label": build_channel_label(bool(webhook_events), bool(telegram_events)),
        "event_count": len(active_events),
        "events": active_events,
        "webhook_event_count": len(webhook_events),
        "telegram_event_count": len(telegram_events),
        "summary": summary,
    }


@dataclass(frozen=True)
class EnvDestination:
    """A notification destination set from the environment, shown read-only (no secret in it)."""

    kind: str
    label: str
    configured: bool
    target_hint: str | None
    events: list[str]
    env_names: str


def env_notification_destinations() -> list[EnvDestination]:
    """The webhook and Telegram destinations as the UI may show them.

    Only the webhook host and the last four characters of the Telegram chat ID are exposed:
    the webhook path and the bot token are secrets.
    """
    webhook_configured = bool(NOTIFICATION_WEBHOOK_URL)
    telegram_configured = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    webhook_events = get_webhook_notification_events() if webhook_configured else set()
    telegram_events = get_telegram_notification_events() if telegram_configured else set()
    return [
        EnvDestination(
            kind="webhook",
            label="Webhook",
            configured=webhook_configured,
            target_hint=urlsplit(NOTIFICATION_WEBHOOK_URL).hostname if webhook_configured else None,
            events=sorted(humanize_notification_event(event) for event in webhook_events),
            env_names="NOTIFICATION_WEBHOOK_URL, NOTIFICATION_WEBHOOK_EVENTS",
        ),
        EnvDestination(
            kind="telegram",
            label="Telegram",
            configured=telegram_configured,
            target_hint=f"chat ending in {TELEGRAM_CHAT_ID[-4:]}" if telegram_configured else None,
            events=sorted(humanize_notification_event(event) for event in telegram_events),
            env_names="TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_NOTIFICATION_EVENTS",
        ),
    ]


def humanize_notification_event(event: str) -> str:
    mapping = {
        "notification_test": "Notification test",
        "manual_backup_success": "Manual backup completed",
        "manual_backup_failed": "Manual backup failed",
        "auto_backup_success": "Automatic backup completed",
        "auto_backup_partial": "Automatic backup completed with errors",
        "auto_backup_failed": "Automatic backup failed",
        "restore_success": "Restore completed",
        "restore_failed": "Restore failed",
        "retention_cleanup": "Retention cleanup completed",
    }
    return mapping.get(event, event.replace("_", " ").title())


def build_channel_label(webhook: bool, telegram: bool) -> str:
    labels: list[str] = []
    if webhook:
        labels.append("Webhook")
    if telegram:
        labels.append("Telegram")
    return " + ".join(labels) if labels else "Disabled"


def format_notification_detail_value(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            rendered_items = []
            for item in value[:5]:
                tunnel = item.get("tunnel") or item.get("tunnel_id") or "item"
                message = item.get("message") or "Unknown issue"
                rendered_items.append(f"- {tunnel}: {message}")
            if len(value) > 5:
                rendered_items.append(f"- ... and {len(value) - 5} more")
            return "\n".join(rendered_items)
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}={val}" for key, val in value.items())
    return str(value)


async def send_webhook_notification(event: str, payload: dict[str, Any], force: bool = False) -> dict[str, Any]:
    if not NOTIFICATION_WEBHOOK_URL:
        return {"channel": "webhook", "attempted": False, "success": False, "reason": "not_configured"}
    if not force and event not in get_webhook_notification_events():
        return {"channel": "webhook", "attempted": False, "success": False, "reason": "event_disabled"}
    try:
        async with httpx.AsyncClient(timeout=min(REQUEST_TIMEOUT, 10.0)) as client:
            response = await client.post(NOTIFICATION_WEBHOOK_URL, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "Notification webhook returned HTTP %s for event %s: %s",
                response.status_code,
                event,
                response.text[:300].strip(),
            )
            return {"channel": "webhook", "attempted": True, "success": False, "reason": f"http_{response.status_code}"}
        logger.info("Webhook notification sent successfully (event=%s).", event)
        return {"channel": "webhook", "attempted": True, "success": True}
    except Exception:
        logger.exception("Failed to send webhook notification (event=%s).", event)
        return {"channel": "webhook", "attempted": True, "success": False, "reason": "exception"}


async def send_telegram_notification(event: str, payload: dict[str, Any], force: bool = False) -> dict[str, Any]:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return {"channel": "telegram", "attempted": False, "success": False, "reason": "not_configured"}
    if not force and event not in get_telegram_notification_events():
        return {"channel": "telegram", "attempted": False, "success": False, "reason": "event_disabled"}

    message = payload["message"]
    details = payload.get("details") or {}
    event_title = humanize_notification_event(event)
    lines = [
        f"Tikka Masala {APP_VERSION}",
        event_title,
        message,
    ]
    if details and not get_telegram_compact_notifications():
        lines.append("")
        for key, value in details.items():
            rendered = format_notification_detail_value(value)
            if not rendered:
                continue
            lines.append(f"{key}: {rendered}")

    telegram_payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines)[:4096],
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=min(REQUEST_TIMEOUT, 10.0)) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=telegram_payload,
            )
        if response.status_code >= 400:
            logger.warning(
                "Telegram notification returned HTTP %s for event %s: %s",
                response.status_code,
                event,
                response.text[:300].strip(),
            )
            return {"channel": "telegram", "attempted": True, "success": False, "reason": f"http_{response.status_code}"}
        logger.info("Telegram notification sent successfully (event=%s).", event)
        return {"channel": "telegram", "attempted": True, "success": True}
    except Exception:
        logger.exception("Failed to send Telegram notification (event=%s).", event)
        return {"channel": "telegram", "attempted": True, "success": False, "reason": "exception"}


async def deliver_notification(
    event: str,
    message: str,
    details: dict[str, Any] | None = None,
    level: str = "info",
    force: bool = False,
) -> dict[str, Any]:
    status = get_notification_status()
    if not force and (event not in status["events"] or not status["configured"]):
        return {"attempted": False, "sent_count": 0, "successful_channels": [], "failed_channels": []}

    payload = {
        "app": "Tikka Masala",
        "version": APP_VERSION,
        "event": event,
        "title": humanize_notification_event(event),
        "level": level,
        "message": message,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "details": details or {},
    }
    results = await asyncio.gather(
        send_webhook_notification(event, payload, force=force),
        send_telegram_notification(event, payload, force=force),
    )
    attempted = [result for result in results if result.get("attempted")]
    successful_channels = [result["channel"] for result in attempted if result.get("success")]
    failed_channels = [result["channel"] for result in attempted if not result.get("success")]
    return {
        "attempted": bool(attempted),
        "sent_count": len(successful_channels),
        "successful_channels": successful_channels,
        "failed_channels": failed_channels,
    }


async def send_notification(event: str, message: str, details: dict[str, Any] | None = None, level: str = "info") -> None:
    await deliver_notification(event, message, details, level)


def get_notification_log_context() -> dict[str, Any]:
    status = get_notification_status()
    return {
        "summary": status["summary_label"],
        "events": ", ".join(status["events"]) if status["events"] else "none",
        "webhook_enabled": status["webhook_enabled"],
        "telegram_enabled": status["telegram_enabled"],
        "webhook_event_count": status["webhook_event_count"],
        "telegram_event_count": status["telegram_event_count"],
    }


def queue_notification(event: str, message: str, details: dict[str, Any] | None = None, level: str = "info") -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("Skipping queued notification because no running event loop is available (event=%s).", event)
        return

    task = loop.create_task(send_notification(event, message, details, level))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


DEFAULT_NOTIFICATION_MESSAGES: dict[str, str] = {
    "notification_test": "This is a test notification from Tikka Masala {version}.",
    "manual_backup_success": "Created backup #{backup_id} for tunnel {tunnel_name}.",
    "manual_backup_failed": "Failed to create a manual backup for tunnel {tunnel_id}.",
    "auto_backup_success": "Backed up {backup_count} tunnel(s): {backed_up_tunnels}\nSkipped: {skipped_count} • Discovered: {tunnel_count}",
    "auto_backup_partial": "Backed up {backup_count} tunnel(s): {backed_up_tunnels}\nErrors: {error_count} • Skipped: {skipped_count} • Discovered: {tunnel_count}",
    "auto_backup_failed": "Automatic backup failed. Errors: {error_count} • Attempted: {processed_count} • Discovered: {tunnel_count}",
    "restore_success": "Backup #{backup_id} was restored to tunnel {tunnel_id}.",
    "restore_failed": "Failed to restore backup #{backup_id} to tunnel {tunnel_id}.",
    "retention_cleanup": "Retention cleanup deleted {deleted_count} backup(s).",
}
NOTIFICATION_MESSAGE_PLACEHOLDERS: dict[str, list[str]] = {
    "notification_test": [],
    "manual_backup_success": ["backup_id", "account_id", "tunnel_id", "tunnel_name", "route_count"],
    "manual_backup_failed": ["account_id", "tunnel_id", "error"],
    "auto_backup_success": ["trigger", "account_id", "tunnel_count", "backup_count", "error_count", "skipped_count", "processed_count", "backed_up_tunnels", "skipped_tunnels"],
    "auto_backup_partial": ["trigger", "account_id", "tunnel_count", "backup_count", "error_count", "skipped_count", "processed_count", "backed_up_tunnels", "skipped_tunnels"],
    "auto_backup_failed": ["trigger", "account_id", "tunnel_count", "backup_count", "error_count", "skipped_count", "processed_count", "backed_up_tunnels", "skipped_tunnels"],
    "restore_success": ["backup_id", "account_id", "tunnel_id"],
    "restore_failed": ["backup_id", "account_id", "tunnel_id", "error"],
    "retention_cleanup": ["deleted_count", "retention_days", "cutoff"],
}
NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS = ["version", "timestamp"]


class _SafeFormatDict(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def get_notification_message_templates() -> dict[str, str]:
    raw = (get_setting("notification_message_templates") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def set_notification_message_templates(templates: dict[str, str]) -> None:
    set_setting("notification_message_templates", json.dumps(templates, ensure_ascii=False))


def get_telegram_compact_notifications() -> bool:
    raw = (get_setting("telegram_compact_notifications") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def set_telegram_compact_notifications(enabled: bool) -> None:
    set_setting("telegram_compact_notifications", "true" if enabled else "false")


def render_notification_message(event: str, details: dict[str, Any] | None = None) -> str:
    templates = get_notification_message_templates()
    template = templates.get(event, "").strip()
    if not template:
        template = DEFAULT_NOTIFICATION_MESSAGES.get(event, event)
    ctx = _SafeFormatDict(details or {})
    ctx.setdefault("version", APP_VERSION)
    ctx.setdefault("timestamp", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    try:
        return template.format_map(ctx)
    except (ValueError, KeyError):
        return template
