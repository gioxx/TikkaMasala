from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "app.db"
API_BASE = os.getenv("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
DEFAULT_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
DEFAULT_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
AUTO_BACKUP_TIMEZONE = os.getenv("AUTO_BACKUP_TIMEZONE", "").strip()
BACKUP_RETENTION_DAYS_RAW = os.getenv("BACKUP_RETENTION_DAYS", "").strip()
NOTIFICATION_WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "").strip()
NOTIFICATION_WEBHOOK_EVENTS_RAW = os.getenv("NOTIFICATION_WEBHOOK_EVENTS", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_NOTIFICATION_EVENTS_RAW = os.getenv("TELEGRAM_NOTIFICATION_EVENTS", "").strip()
DEMO_MODE = os.getenv("DEMO", "false").strip().lower() in ("true", "1", "yes")
API_TOKEN_COOKIE_NAME = "cf_api_token"
API_TOKEN_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
ENCRYPTED_VALUE_PREFIX = "enc:"
AUTO_BACKUP_JOB_ID = "auto-backup-all-tunnels"
DEFAULT_AUTO_BACKUP_CRON = "0 3 * * *"
BACKUPS_PAGE_SIZE = 5
BACKUPS_PAGE_SIZE_OPTIONS = {5, 10, 25, 50, 75, 100}
SCHEDULED_RUNS_PAGE_SIZE = 15
SCHEDULED_RUNS_PAGE_SIZE_OPTIONS = (15, 30, 60, 90)
APP_VERSION = os.getenv("APP_VERSION", "dev")
SUPPORTED_NOTIFICATION_EVENTS = frozenset(
    {
        "notification_test",
        "manual_backup_success",
        "manual_backup_failed",
        "auto_backup_success",
        "auto_backup_partial",
        "auto_backup_failed",
        "restore_success",
        "restore_failed",
        "retention_cleanup",
    }
)
DEFAULT_NOTIFICATION_EVENTS = frozenset(
    {
        "auto_backup_success",
        "auto_backup_partial",
        "auto_backup_failed",
        "restore_failed",
        "retention_cleanup",
    }
)

BACKUP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(title="Tikka Masala")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["app_version"] = APP_VERSION
logger = logging.getLogger(__name__)
auto_backup_scheduler = AsyncIOScheduler(timezone="UTC")
auto_backup_lock = asyncio.Lock()
background_tasks: set[asyncio.Task[Any]] = set()


@dataclass
class BackupRecord:
    id: int
    created_at: str
    account_id: str
    tunnel_id: str
    tunnel_name: str | None
    route_count: int
    filename: str
    notes: str | None


@dataclass
class RestoreRecord:
    id: int
    backup_id: int
    restored_at: str
    account_id: str
    tunnel_id: str


@dataclass
class ScheduledRunRecord:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    account_id: str
    tunnel_count: int
    backup_count: int
    error_count: int
    details: str | None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                account_id TEXT NOT NULL,
                tunnel_id TEXT NOT NULL,
                tunnel_name TEXT,
                route_count INTEGER NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS restores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_id INTEGER NOT NULL,
                restored_at TEXT NOT NULL,
                account_id TEXT NOT NULL,
                tunnel_id TEXT NOT NULL,
                FOREIGN KEY (backup_id) REFERENCES backups(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                account_id TEXT NOT NULL,
                tunnel_count INTEGER NOT NULL DEFAULT 0,
                backup_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                details TEXT
            )
            """
        )
        conn.commit()


init_db()


def headers(api_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_setting(key: str) -> str | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def delete_setting(key: str) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def get_saved_account_id() -> str:
    if DEMO_MODE:
        return DEFAULT_ACCOUNT_ID
    return (get_setting("account_id") or DEFAULT_ACCOUNT_ID).strip()


def get_account_id_source() -> str | None:
    if DEMO_MODE:
        return "environment" if DEFAULT_ACCOUNT_ID else None
    stored_value = (get_setting("account_id") or "").strip()
    if stored_value:
        return "database"
    if DEFAULT_ACCOUNT_ID:
        return "environment"
    return None


def resolve_account_id(account_id: str | None) -> str:
    resolved = (account_id or "").strip() or get_saved_account_id()
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="Account ID is required. Set it in the form or via CLOUDFLARE_ACCOUNT_ID.",
        )
    return resolved


def remember_account_id(account_id: str) -> None:
    if DEMO_MODE:
        return
    normalized = account_id.strip()
    if normalized:
        set_setting("account_id", normalized)


def get_fernet() -> Fernet | None:
    if not TOKEN_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode("utf-8"))
    except (ValueError, TypeError):
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is invalid. Provide a valid Fernet key.")


def encrypt_secret(value: str) -> str:
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to persist API tokens securely.")
    token = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    if not value.startswith(ENCRYPTED_VALUE_PREFIX):
        return value
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to read encrypted API tokens from the database.")
    encrypted = value.removeprefix(ENCRYPTED_VALUE_PREFIX).encode("utf-8")
    try:
        return fernet.decrypt(encrypted).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Failed to decrypt stored API token. Check TOKEN_ENCRYPTION_KEY.") from exc


def get_saved_api_token(request: Request) -> str:
    if DEMO_MODE:
        return DEFAULT_API_TOKEN.strip()
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return cookie_token
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypted = decrypt_secret(stored_token).strip()
            if decrypted and not stored_token.startswith(ENCRYPTED_VALUE_PREFIX):
                remember_api_token(decrypted)
            return decrypted
        except RuntimeError:
            logger.exception("Unable to decrypt saved API token; falling back to environment/default prefill.")
    return DEFAULT_API_TOKEN.strip()


def get_api_token_source(request: Request) -> str | None:
    if DEMO_MODE:
        return "environment" if DEFAULT_API_TOKEN else None
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return "browser"
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypt_secret(stored_token)
            return "database"
        except RuntimeError:
            logger.exception("Saved API token exists in the database but is not readable with the current key.")
    if DEFAULT_API_TOKEN:
        return "environment"
    return None


def get_server_api_token() -> str:
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            return decrypt_secret(stored_token).strip()
        except RuntimeError:
            logger.exception("Unable to decrypt saved API token for server-side scheduler; falling back to environment token.")
    return DEFAULT_API_TOKEN.strip()


def get_server_api_token_source() -> str | None:
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypt_secret(stored_token)
            return "database"
        except RuntimeError:
            logger.exception("Saved API token exists in the database but is not readable for the scheduler.")
    if DEFAULT_API_TOKEN:
        return "environment"
    return None


def get_backup_retention_days() -> int | None:
    if not BACKUP_RETENTION_DAYS_RAW:
        return None
    try:
        days = int(BACKUP_RETENTION_DAYS_RAW)
    except ValueError:
        logger.warning("Ignoring BACKUP_RETENTION_DAYS because it is not a valid integer: %s", BACKUP_RETENTION_DAYS_RAW)
        return None
    if days <= 0:
        logger.warning("Ignoring BACKUP_RETENTION_DAYS because it must be greater than zero: %s", BACKUP_RETENTION_DAYS_RAW)
        return None
    return days


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
    if details:
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


def resolve_api_token(request: Request, api_token: str | None) -> str:
    resolved = (api_token or "").strip() or get_saved_api_token(request)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="API token is required. Set it in the form or via CLOUDFLARE_API_TOKEN.",
        )
    return resolved


def remember_api_token(api_token: str) -> None:
    if DEMO_MODE:
        return
    normalized = api_token.strip()
    if normalized and TOKEN_ENCRYPTION_KEY:
        set_setting("api_token", encrypt_secret(normalized))


def set_api_token_cookie(response: HTMLResponse | RedirectResponse, api_token: str) -> None:
    if DEMO_MODE:
        return
    response.set_cookie(
        key=API_TOKEN_COOKIE_NAME,
        value=api_token.strip(),
        max_age=API_TOKEN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_api_token_cookie(response: HTMLResponse | RedirectResponse) -> None:
    response.delete_cookie(key=API_TOKEN_COOKIE_NAME, samesite="lax")


def build_prefill_status_from_sources(account_source: str | None, api_token_source: str | None) -> dict[str, Any]:
    return {
        "account_id_source": account_source,
        "api_token_source": api_token_source,
        "has_prefill": bool(account_source or api_token_source),
        "uses_environment_prefill": "environment" in {account_source, api_token_source},
        "uses_database_prefill": "database" in {account_source, api_token_source},
        "uses_browser_prefill": api_token_source == "browser",
        "can_persist_api_token": bool(TOKEN_ENCRYPTION_KEY),
    }


def build_prefill_status(request: Request) -> dict[str, Any]:
    return build_prefill_status_from_sources(get_account_id_source(), get_api_token_source(request))


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


def get_last_backup_time_for_tunnel(tunnel_id: str) -> datetime | None:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last_at FROM backups WHERE tunnel_id = ?",
            (tunnel_id,),
        ).fetchone()
    if row and row["last_at"]:
        try:
            return datetime.fromisoformat(str(row["last_at"])).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


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
    account_id = get_saved_account_id()
    account_source = get_account_id_source()
    api_token = get_server_api_token()
    api_token_source = get_server_api_token_source()
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


@app.on_event("startup")
async def startup_event() -> None:
    if DEMO_MODE:
        logger.warning(
            "DEMO mode is active. Authentication data will not be persisted and the automatic backup scheduler is disabled."
        )
    logger.info(
        "Starting Tikka Masala (data_dir=%s, api_base=%s, log_level=%s, auto_backup_timezone=%s, retention_days=%s, notifications=%s, demo_mode=%s).",
        DATA_DIR,
        API_BASE,
        LOG_LEVEL,
        AUTO_BACKUP_TIMEZONE or "auto",
        get_backup_retention_days() or "disabled",
        get_notification_status()["summary_label"],
        DEMO_MODE,
    )
    notification_log_context = get_notification_log_context()
    logger.info(
        "Notification channels: summary=%s, events=%s, webhook_enabled=%s, telegram_enabled=%s, webhook_event_count=%s, telegram_event_count=%s.",
        notification_log_context["summary"],
        notification_log_context["events"],
        notification_log_context["webhook_enabled"],
        notification_log_context["telegram_enabled"],
        notification_log_context["webhook_event_count"],
        notification_log_context["telegram_event_count"],
    )
    if not DEMO_MODE:
        if not auto_backup_scheduler.running:
            auto_backup_scheduler.start()
        configure_auto_backup_job()
    logger.info("Application startup complete.")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    logger.info("Shutting down Tikka Masala.")
    if auto_backup_scheduler.running:
        auto_backup_scheduler.shutdown(wait=False)
    logger.info("Application shutdown complete.")


async def cloudflare_get(account_id: str, path: str, api_token: str) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers(api_token))
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=message)
    return data


async def cloudflare_put(account_id: str, path: str, api_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.put(url, headers=headers(api_token), json=payload)
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=message)
    return data


async def verify_token(api_token: str) -> dict[str, Any]:
    url = f"{API_BASE}/user/tokens/verify"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers(api_token))
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=f"Token verification failed: {message}")
    return data


async def fetch_tunnel_configuration(account_id: str, tunnel_id: str, api_token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    tunnel_data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}", api_token)
    config_data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token)
    return tunnel_data, config_data


async def list_tunnels(account_id: str, api_token: str) -> list[dict[str, Any]]:
    data = await cloudflare_get(account_id, "cfd_tunnel", api_token)
    result = data.get("result", [])
    if isinstance(result, list):
        return result
    return result if isinstance(result, list) else []


async def create_backup(account_id: str, tunnel_id: str, api_token: str, notes: str | None = None) -> BackupRecord:
    tunnel_data, config_data = await fetch_tunnel_configuration(account_id, tunnel_id, api_token)
    tunnel = tunnel_data.get("result", {})
    config = config_data.get("result", {})

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tunnel_name = tunnel.get("name") or "Unknown tunnel"
    ingress = config.get("config", {}).get("ingress", [])
    route_count = len(ingress)
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:12]}.json"

    payload = {
        "exported_at": created_at,
        "account_id": account_id,
        "tunnel_id": tunnel_id,
        "tunnel": tunnel,
        "configuration": config,
        "notes": notes or "",
    }

    file_path = BACKUP_DIR / filename
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    with closing(db()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes or ""),
        )
        conn.commit()
        backup_id = cursor.lastrowid

    purge_expired_backups(datetime.fromisoformat(created_at))

    return BackupRecord(
        id=backup_id,
        created_at=created_at,
        account_id=account_id,
        tunnel_id=tunnel_id,
        tunnel_name=tunnel_name,
        route_count=route_count,
        filename=filename,
        notes=notes,
    )


def purge_expired_backups(reference_time: datetime | None = None) -> int:
    retention_days = get_backup_retention_days()
    if retention_days is None:
        return 0

    cutoff_time = (reference_time or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    cutoff_iso = cutoff_time.replace(microsecond=0).isoformat()

    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, filename
            FROM backups
            WHERE created_at < ?
            ORDER BY id ASC
            """,
            (cutoff_iso,),
        ).fetchall()

        deleted_count = 0
        for row in rows:
            backup_id = int(row["id"])
            filename = str(row["filename"])
            file_path = BACKUP_DIR / filename

            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove expired backup file: %s", file_path)

            conn.execute("DELETE FROM restores WHERE backup_id = ?", (backup_id,))
            conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
            deleted_count += 1

        conn.commit()

    if deleted_count:
        logger.info(
            "Deleted %s expired backup(s) older than %s day(s) due to BACKUP_RETENTION_DAYS.",
            deleted_count,
            retention_days,
        )
        _rc_details = {
            "deleted_count": deleted_count,
            "retention_days": retention_days,
            "cutoff": cutoff_iso,
        }
        queue_notification(
            "retention_cleanup",
            render_notification_message("retention_cleanup", _rc_details),
            _rc_details,
            level="info",
        )

    return deleted_count


def extract_error_message(payload: dict[str, Any]) -> str:
    errors = payload.get("errors") or []
    if errors:
        first = errors[0]
        code = first.get("code")
        message = first.get("message", "Unknown Cloudflare API error")
        return f"{message} (code {code})" if code else message
    messages = payload.get("messages") or []
    if messages:
        first = messages[0]
        return first.get("message", "Unknown Cloudflare API response")
    return "Unknown Cloudflare API error"


def get_backups() -> list[BackupRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes FROM backups ORDER BY id DESC"
        ).fetchall()
    return [BackupRecord(**dict(row)) for row in rows]


def get_backups_page(page: int, page_size: int = BACKUPS_PAGE_SIZE) -> tuple[list[BackupRecord], dict[str, int | bool]]:
    page = max(page, 1)
    page_size = page_size if page_size in BACKUPS_PAGE_SIZE_OPTIONS else BACKUPS_PAGE_SIZE
    offset = (page - 1) * page_size

    with closing(db()) as conn:
        total_count = int(conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0])
        rows = conn.execute(
            """
            SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes
            FROM backups
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()

    backups = [BackupRecord(**dict(row)) for row in rows]
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    current_page = min(page, total_pages)
    start_index = offset + 1 if total_count and backups else 0
    end_index = offset + len(backups)

    return backups, {
        "current_page": current_page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
        "previous_page": current_page - 1,
        "next_page": current_page + 1,
        "start_index": start_index,
        "end_index": end_index,
    }


def get_database_stats() -> dict[str, Any]:
    with closing(db()) as conn:
        backup_stats = conn.execute(
            """
            SELECT
                COUNT(*) AS backup_count,
                COUNT(DISTINCT tunnel_id) AS tunnel_count,
                COALESCE(SUM(route_count), 0) AS route_total,
                MIN(created_at) AS oldest_backup_at,
                MAX(created_at) AS latest_backup_at
            FROM backups
            """
        ).fetchone()
        restore_stats = conn.execute(
            """
            SELECT COUNT(*) AS restore_count
            FROM restores
            """
        ).fetchone()
        run_stats = conn.execute(
            """
            SELECT COUNT(*) AS scheduled_run_count
            FROM scheduled_runs
            """
        ).fetchone()

    return {
        "backup_count": int(backup_stats["backup_count"] or 0),
        "tunnel_count": int(backup_stats["tunnel_count"] or 0),
        "route_total": int(backup_stats["route_total"] or 0),
        "restore_count": int(restore_stats["restore_count"] or 0),
        "scheduled_run_count": int(run_stats["scheduled_run_count"] or 0),
        "oldest_backup_at": backup_stats["oldest_backup_at"],
        "latest_backup_at": backup_stats["latest_backup_at"],
        "database_size": format_bytes(DB_PATH.stat().st_size) if DB_PATH.exists() else "0 B",
        "backup_storage_size": format_bytes(get_directory_size(BACKUP_DIR)),
    }


def get_backup_retention_status() -> dict[str, Any]:
    retention_days = get_backup_retention_days()
    if retention_days is None:
        return {
            "enabled": False,
            "days": None,
            "summary": "Full archive retention is active. Backups are kept until you remove them manually.",
            "detail": "BACKUP_RETENTION_DAYS is not currently set, so Tikka Masala will not delete old backups automatically.",
        }

    return {
        "enabled": True,
        "days": retention_days,
        "summary": f"Automatic retention is active. Backups older than {retention_days} day(s) are deleted when new backups are created.",
        "detail": "Cleanup removes both the JSON snapshot and its related restore history from the database.",
    }


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def get_directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def get_requested_backup_page(request: Request) -> int:
    raw_page = (request.query_params.get("page") or "1").strip()
    try:
        return max(int(raw_page), 1)
    except ValueError:
        return 1


def get_requested_backup_page_size(request: Request) -> int:
    raw_page_size = (request.query_params.get("page_size") or str(BACKUPS_PAGE_SIZE)).strip()
    try:
        page_size = int(raw_page_size)
    except ValueError:
        return BACKUPS_PAGE_SIZE
    return page_size if page_size in BACKUPS_PAGE_SIZE_OPTIONS else BACKUPS_PAGE_SIZE


def get_requested_scheduled_runs_page(request: Request) -> int:
    raw_page = (request.query_params.get("page") or "1").strip()
    try:
        return max(int(raw_page), 1)
    except ValueError:
        return 1


def get_requested_scheduled_runs_page_size(request: Request) -> int | None:
    raw_page_size = (request.query_params.get("page_size") or str(SCHEDULED_RUNS_PAGE_SIZE)).strip().lower()
    if raw_page_size == "all":
        return None
    try:
        page_size = int(raw_page_size)
    except ValueError:
        return SCHEDULED_RUNS_PAGE_SIZE
    return page_size if page_size in SCHEDULED_RUNS_PAGE_SIZE_OPTIONS else SCHEDULED_RUNS_PAGE_SIZE


def get_backup(backup_id: int) -> BackupRecord:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes FROM backups WHERE id = ?",
            (backup_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return BackupRecord(**dict(row))


def load_backup_json(backup_id: int) -> dict[str, Any]:
    backup = get_backup(backup_id)
    file_path = BACKUP_DIR / backup.filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    with file_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def record_restore(backup_id: int, account_id: str, tunnel_id: str) -> None:
    restored_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO restores (backup_id, restored_at, account_id, tunnel_id)
            VALUES (?, ?, ?, ?)
            """,
            (backup_id, restored_at, account_id, tunnel_id),
        )
        conn.commit()


def get_restore_history(backup_id: int) -> list[RestoreRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, backup_id, restored_at, account_id, tunnel_id
            FROM restores
            WHERE backup_id = ?
            ORDER BY id DESC
            """,
            (backup_id,),
        ).fetchall()
    return [RestoreRecord(**dict(row)) for row in rows]


async def run_auto_backup_job(trigger: str = "schedule") -> dict[str, Any]:
    if auto_backup_lock.locked():
        logger.info("Automatic backup run skipped because another run is already in progress.")
        details = json.dumps({"message": "Skipped because another automatic backup is already running."}, ensure_ascii=False)
        run_id = create_scheduled_run(get_saved_account_id() or "unknown", "skipped", details)
        complete_scheduled_run(run_id, "skipped", 0, 0, 0, details)
        set_setting("auto_backup_last_run_at", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        set_setting("auto_backup_last_status", "skipped")
        return {"status": "skipped", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

    async with auto_backup_lock:
        account_id = get_saved_account_id()
        api_token = get_server_api_token()
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


def render_index_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    success_target: str | None = "tunnels",
    tunnels: list[dict[str, Any]] | None = None,
    prefill_status: dict[str, Any] | None = None,
    prefill_account_id: str | None = None,
    prefill_api_token: str | None = None,
    backup_page: int | None = None,
    backup_page_size: int | None = None,
) -> HTMLResponse:
    resolved_page = backup_page if isinstance(backup_page, int) else get_requested_backup_page(request)
    resolved_page_size = backup_page_size if isinstance(backup_page_size, int) else get_requested_backup_page_size(request)
    backups, backup_pagination = get_backups_page(resolved_page, resolved_page_size)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "message": message,
            "error": error,
            "success_target": success_target,
            "backups": backups,
            "backup_pagination": backup_pagination,
            "backup_page_size_options": sorted(BACKUPS_PAGE_SIZE_OPTIONS),
            "database_stats": get_database_stats(),
            "backup_retention": get_backup_retention_status(),
            "tunnels": tunnels,
            "prefill_status": prefill_status or build_prefill_status(request),
            "prefill_account_id": prefill_account_id if prefill_account_id is not None else get_saved_account_id(),
            "prefill_api_token": prefill_api_token if prefill_api_token is not None else get_saved_api_token(request),
            "auto_backup": build_auto_backup_status(),
            "demo_mode": DEMO_MODE,
        },
    )


def render_backup_page(
    request: Request,
    backup_id: int,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    backup = get_backup(backup_id)
    content = load_backup_json(backup_id)
    restores = get_restore_history(backup_id)
    return templates.TemplateResponse(
        request,
        "backup.html",
        {
            "backup": backup,
            "restores": restores,
            "content": json.dumps(content, indent=2, ensure_ascii=False),
            "message": message,
            "error": error,
            "prefill_account_id": get_saved_account_id(),
            "prefill_api_token": get_saved_api_token(request),
            "demo_mode": DEMO_MODE,
        },
    )


def render_scheduled_runs_page(request: Request) -> HTMLResponse:
    runs, scheduled_runs_pagination = get_scheduled_runs_page(
        get_requested_scheduled_runs_page(request),
        get_requested_scheduled_runs_page_size(request),
    )
    scheduled_runs_stats = get_scheduled_runs_stats()
    run_items = [
        {
            "record": run,
            "details_summary": summarize_scheduled_run_details(run.details),
        }
        for run in runs
    ]
    return templates.TemplateResponse(
        request,
        "scheduled_runs.html",
        {
            "runs": run_items,
            "scheduled_runs_pagination": scheduled_runs_pagination,
            "scheduled_runs_page_size_options": SCHEDULED_RUNS_PAGE_SIZE_OPTIONS,
            "scheduled_runs_stats": scheduled_runs_stats,
            "demo_mode": DEMO_MODE,
        },
    )


@app.get("/", response_class=HTMLResponse)
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


@app.post("/verify-token", response_class=HTMLResponse)
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


@app.post("/list-tunnels", response_class=HTMLResponse)
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


@app.post("/backup", response_model=None)
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


@app.post("/clear-saved-auth", response_class=HTMLResponse)
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


@app.post("/auto-backup/settings", response_class=HTMLResponse)
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


@app.post("/auto-backup/run", response_class=HTMLResponse)
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


@app.get("/auto-backup/tunnel-filters", response_class=HTMLResponse)
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


@app.post("/auto-backup/tunnel-filters", response_class=HTMLResponse)
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


@app.get("/notifications/messages", response_class=HTMLResponse)
async def notification_messages_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/#notifications-section", status_code=303)
    templates_data = get_notification_message_templates()
    return templates.TemplateResponse(
        request,
        "notification_messages.html",
        {
            "events": list(DEFAULT_NOTIFICATION_MESSAGES.keys()),
            "defaults": DEFAULT_NOTIFICATION_MESSAGES,
            "placeholders": NOTIFICATION_MESSAGE_PLACEHOLDERS,
            "global_placeholders": NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS,
            "saved": templates_data,
            "demo_mode": DEMO_MODE,
        },
    )


@app.post("/notifications/messages", response_class=HTMLResponse)
async def notification_messages_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/#notifications-section", status_code=303)
    try:
        form = await request.form()
        new_templates: dict[str, str] = {}
        for event in DEFAULT_NOTIFICATION_MESSAGES:
            value = str(form.get(f"msg_{event}", "")).strip()
            if value and value != DEFAULT_NOTIFICATION_MESSAGES[event]:
                new_templates[event] = value
        set_notification_message_templates(new_templates)
        logger.info("Notification message templates updated (%s custom).", len(new_templates))
        return RedirectResponse(url="/notifications/messages?saved=1", status_code=303)
    except Exception:
        logger.exception("Unexpected error saving notification message templates.")
        return RedirectResponse(url="/notifications/messages?error=1", status_code=303)


@app.post("/notifications/messages/reset", response_class=HTMLResponse)
async def notification_messages_reset_action() -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/#notifications-section", status_code=303)
    set_notification_message_templates({})
    logger.info("Notification message templates reset to defaults.")
    return RedirectResponse(url="/notifications/messages?reset=1", status_code=303)


@app.post("/notifications/test", response_class=HTMLResponse)
async def notifications_test_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return render_index_page(request, error="Notifications are not available in Demo mode.", success_target="notifications")
    status = get_notification_status()
    if not status["webhook_enabled"] and not status["telegram_enabled"]:
        logger.warning("Notification test requested, but no notification channel is configured.")
        return render_index_page(
            request,
            error="Notification test failed: no configured channel found. Set a webhook URL and/or Telegram bot settings first.",
            success_target="notifications",
        )

    logger.info(
        "Notification test requested from UI (channels=%s, events=%s).",
        status["channel_label"],
        ", ".join(status["events"]) if status["events"] else "none",
    )
    _nt_details: dict[str, Any] = {
        "configured_channels": status["channel_label"],
        "enabled_events": status["events"],
    }
    result = await deliver_notification(
        "notification_test",
        render_notification_message("notification_test", _nt_details),
        _nt_details,
        level="info",
        force=True,
    )
    logger.info(
        "Notification test finished (sent=%s, successful_channels=%s, failed_channels=%s).",
        result["sent_count"],
        ", ".join(result["successful_channels"]) or "none",
        ", ".join(result["failed_channels"]) or "none",
    )
    if result["sent_count"]:
        successful_channels = [channel.title() for channel in result["successful_channels"]]
        failed_channels = [channel.title() for channel in result["failed_channels"]]
        message = f"Notification test sent successfully via {', '.join(successful_channels)}."
        if result["failed_channels"]:
            message += f" Failed channels: {', '.join(failed_channels)}."
        return render_index_page(request, message=message, success_target="notifications")

    return render_index_page(
        request,
        error="Notification test failed. Check the container logs for channel-specific errors.",
        success_target="notifications",
    )


@app.get("/backup/{backup_id}", response_class=HTMLResponse)
async def backup_details(request: Request, backup_id: int) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    return render_backup_page(request, backup_id)


@app.get("/auto-backup/runs", response_class=HTMLResponse)
async def auto_backup_runs(request: Request) -> HTMLResponse:
    return render_scheduled_runs_page(request)


@app.get("/backup/{backup_id}/download", response_model=None)
async def download_backup(backup_id: int) -> FileResponse | RedirectResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    backup = get_backup(backup_id)
    file_path = BACKUP_DIR / backup.filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    logger.info("Backup download requested (backup_id=%s, filename=%s).", backup_id, backup.filename)
    return FileResponse(path=file_path, filename=backup.filename, media_type="application/json")


@app.post("/backup/{backup_id}/restore", response_class=HTMLResponse)
async def restore_backup(
    request: Request,
    backup_id: int,
    account_id: str = Form(default=""),
    tunnel_id: str = Form(...),
    api_token: str = Form(default=""),
) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    try:
        account_id = resolve_account_id(account_id)
        api_token = resolve_api_token(request, api_token)
        logger.info(
            "Restore requested (backup_id=%s, account_id=%s, tunnel_id=%s).",
            backup_id,
            account_id,
            tunnel_id,
        )
        payload = load_backup_json(backup_id)
        configuration = payload.get("configuration", {})
        config_body = configuration.get("config")
        if not isinstance(config_body, dict):
            raise HTTPException(status_code=400, detail="Backup file does not contain a restorable tunnel configuration")
        await cloudflare_put(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token, {"config": config_body})
        remember_account_id(account_id)
        remember_api_token(api_token)
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
        response = render_backup_page(request, backup_id, message="Backup restored successfully.")
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        logger.warning("Restore failed (backup_id=%s): %s", backup_id, exc.detail)
        _rf_details = {
            "backup_id": backup_id,
            "account_id": account_id.strip() or None,
            "tunnel_id": tunnel_id.strip() or None,
            "error": str(exc.detail),
        }
        queue_notification(
            "restore_failed",
            render_notification_message("restore_failed", _rf_details),
            _rf_details,
            level="warning",
        )
        return render_backup_page(request, backup_id, error=exc.detail)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
