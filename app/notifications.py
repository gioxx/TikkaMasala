"""Webhook / Telegram delivery and customizable notification messages."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from string import Formatter
from typing import Any

import httpx

from app.config import (
    APP_VERSION,
    APP_VERSION_RAW,
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
from app.destinations import DeliveryTarget, delivery_targets, webhook_host
from app.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles

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
            target_hint=webhook_host(NOTIFICATION_WEBHOOK_URL) if webhook_configured else None,
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


async def _post_json(url: str, payload: dict[str, Any]) -> httpx.Response:
    """The only HTTP call of this module (tests replace it)."""
    async with httpx.AsyncClient(timeout=min(REQUEST_TIMEOUT, 10.0)) as client:
        return await client.post(url, json=payload)


def env_delivery_targets() -> list[DeliveryTarget]:
    """The webhook and Telegram destinations set in the environment, when configured (Default only)."""
    targets: list[DeliveryTarget] = []
    if NOTIFICATION_WEBHOOK_URL:
        targets.append(
            DeliveryTarget(
                key="env-webhook",
                label="Webhook (environment)",
                kind="webhook",
                events=frozenset(get_webhook_notification_events()),
                secret=NOTIFICATION_WEBHOOK_URL,
                chat_id="",
            )
        )
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        targets.append(
            DeliveryTarget(
                key="env-telegram",
                label="Telegram (environment)",
                kind="telegram",
                events=frozenset(get_telegram_notification_events()),
                secret=TELEGRAM_BOT_TOKEN,
                chat_id=TELEGRAM_CHAT_ID,
            )
        )
    return targets


def _telegram_text(event: str, payload: dict[str, Any]) -> str:
    details = payload.get("details") or {}
    lines = [
        f"Tikka Masala {APP_VERSION}",
        humanize_notification_event(event),
        str(payload.get("message", "")),
    ]
    if details and not get_telegram_compact_notifications():
        lines.append("")
        for key, value in details.items():
            rendered = format_notification_detail_value(value)
            if not rendered:
                continue
            lines.append(f"{key}: {rendered}")
    return "\n".join(lines)[:4096]


async def send_to_target(target: DeliveryTarget, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one notification to one destination; never raises.

    Failures are logged with the destination label and the HTTP status or the exception type only:
    the URL, the bot token and the exception text (which can quote the URL) never reach the log.
    """
    result: dict[str, Any] = {"key": target.key, "label": target.label, "attempted": True, "success": False, "reason": None}
    try:
        if target.kind == "telegram":
            url = f"https://api.telegram.org/bot{target.secret}/sendMessage"
            body: dict[str, Any] = {
                "chat_id": target.chat_id,
                "text": _telegram_text(event, payload),
                "disable_web_page_preview": True,
            }
        else:
            url = target.secret
            body = payload
        response = await _post_json(url, body)
        if response.status_code >= 400:
            result["reason"] = f"HTTP {response.status_code}"
        else:
            result["success"] = True
    except Exception as exc:  # noqa: BLE001 - one destination must never break the others
        result["reason"] = f"could not connect ({type(exc).__name__})"
    if result["success"]:
        logger.info("Notification to %s sent (event=%s).", target.label, event)
    else:
        logger.warning("Notification to %s failed: %s", target.label, result["reason"])
    return result


async def deliver_notification(
    event: str,
    message: str,
    details: dict[str, Any] | None = None,
    level: str = "info",
    force: bool = False,
    *,
    profile_id: int | None,
    only: str | None = None,
) -> dict[str, Any]:
    """Send ``event`` to the destinations of ``profile_id`` (plus the env ones for Default or ``None``).

    ``only`` keeps the one target with that key. Unless ``force``, a target receives only the events
    it subscribes to. A failing destination never stops the others.
    """
    targets: list[DeliveryTarget] = []
    if profile_id is None or profile_id == DEFAULT_PROFILE_ID:
        targets.extend(env_delivery_targets())
    targets.extend(delivery_targets(profile_id))
    if only is not None:
        targets = [target for target in targets if target.key == only]
    if not force:
        targets = [target for target in targets if event in target.events]

    payload = {
        "app": "Tikka Masala",
        # Display form for people, raw build value (tag or git describe) for machines.
        "version": APP_VERSION,
        "version_raw": APP_VERSION_RAW,
        "event": event,
        "title": humanize_notification_event(event),
        "level": level,
        "message": message,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "details": details or {},
        "profile": {"id": profile_id, "name": notification_profile_name(profile_id)},
    }
    results = list(await asyncio.gather(*(send_to_target(target, event, payload) for target in targets)))
    successful_channels = [result["label"] for result in results if result["success"]]
    failed_channels = [result["label"] for result in results if not result["success"]]
    return {
        "attempted": bool(results),
        "sent_count": len(successful_channels),
        "successful_channels": successful_channels,
        "failed_channels": failed_channels,
        "results": results,
    }


async def send_notification(
    event: str,
    message: str,
    details: dict[str, Any] | None = None,
    level: str = "info",
    *,
    profile_id: int | None,
) -> None:
    """Deliver a notification in the background: any error is logged (type only), never raised."""
    try:
        await deliver_notification(event, message, details, level, profile_id=profile_id)
    except Exception as exc:  # noqa: BLE001 - a notification must never fail the operation it reports
        logger.warning(
            "Notification delivery failed (event=%s, profile_id=%s): %s", event, profile_id, type(exc).__name__
        )


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


def queue_notification(
    event: str,
    message: str,
    details: dict[str, Any] | None = None,
    level: str = "info",
    *,
    profile_id: int | None,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("Skipping queued notification because no running event loop is available (event=%s).", event)
        return

    task = loop.create_task(send_notification(event, message, details, level, profile_id=profile_id))
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
NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS = ["version", "timestamp", "profile"]
OTHER_ACCOUNTS_LABEL = "Other accounts"


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


def notification_profile_name(profile_id: int | None) -> str:
    """The profile name shown in notifications: "Other accounts" for ``None``, a fallback when it is gone."""
    if profile_id is None:
        return OTHER_ACCOUNTS_LABEL
    try:
        return get_profile(profile_id).name
    except LookupError:
        return f"Profile {profile_id}"


FORMAT_SPEC_NUMBER_MAX = 1000
_SPEC_NUMBER_RE = re.compile(r"\d+")


def _format_value(value: Any, spec: str) -> str:
    """``format(value, spec)``, or ``str(value)`` when the spec is invalid or asks for a huge width or
    precision: a custom template must never exhaust memory or break the backup it reports."""
    for number in _SPEC_NUMBER_RE.findall(spec):
        digits = number.lstrip("0")
        # Length first: ``int()`` refuses strings of more than 4300 digits.
        if len(digits) > len(str(FORMAT_SPEC_NUMBER_MAX)) or int(digits or "0") > FORMAT_SPEC_NUMBER_MAX:
            return str(value)
    try:
        return format(value, spec)
    except (TypeError, ValueError, OverflowError, MemoryError):
        return str(value)


def _format_template(template: str, ctx: dict[str, Any]) -> str:
    """``str.format`` limited to plain names: unknown, dotted or indexed fields are left as written.

    Attribute and index lookups are never evaluated, so a template cannot reach object internals,
    and a substituted value (a profile name with braces, say) is never formatted again.
    """
    try:
        parts = list(Formatter().parse(template))
    except ValueError:
        return template
    out: list[str] = []
    for literal, field_name, spec, conversion in parts:
        out.append(literal)
        if field_name is None:
            continue
        if field_name.isidentifier() and field_name in ctx:
            value = ctx[field_name]
            if conversion == "r":
                value = repr(value)
            elif conversion == "a":
                value = ascii(value)
            elif conversion == "s":
                value = str(value)
            out.append(_format_value(value, spec or ""))
        else:
            out.append(
                "{" + field_name + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "") + "}"
            )
    return "".join(out)


def render_notification_message(event: str, details: dict[str, Any] | None = None, *, profile_id: int | None) -> str:
    """The message of ``event``: the custom template if any, else the built-in one.

    Built-in templates start with ``[{profile}] `` when more than one profile exists; custom templates
    are left as written (they can use ``{profile}``). Unknown placeholders stay in the text as written.
    """
    templates = get_notification_message_templates()
    custom = templates.get(event, "")
    template = custom.strip() if isinstance(custom, str) else ""
    if not template:
        template = DEFAULT_NOTIFICATION_MESSAGES.get(event, event)
        if len(list_profiles()) > 1:
            template = "[{profile}] " + template
    ctx: dict[str, Any] = dict(details or {})
    ctx.setdefault("version", APP_VERSION)
    ctx.setdefault("timestamp", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    ctx["profile"] = notification_profile_name(profile_id)
    return _format_template(template, ctx)
