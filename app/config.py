"""Environment-driven settings and app-wide constants."""

from __future__ import annotations

import logging
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "app.db"
ENCRYPTION_KEY_FILE = DATA_DIR / "encryption.key"
API_BASE = os.getenv("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
DEFAULT_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
DEFAULT_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip()
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "").strip()
AUTO_BACKUP_TIMEZONE = os.getenv("AUTO_BACKUP_TIMEZONE", "").strip()
BACKUP_RETENTION_DAYS_RAW = os.getenv("BACKUP_RETENTION_DAYS", "").strip()
NOTIFICATION_WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "").strip()
NOTIFICATION_WEBHOOK_EVENTS_RAW = os.getenv("NOTIFICATION_WEBHOOK_EVENTS", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_NOTIFICATION_EVENTS_RAW = os.getenv("TELEGRAM_NOTIFICATION_EVENTS", "").strip()
DEMO_MODE = os.getenv("DEMO", "false").strip().lower() in ("true", "1", "yes")
# Legacy: the browser cookie that held the API token. Read only by the one-time
# migration in app/setup.py, which moves it to the Default profile and deletes it.
API_TOKEN_COOKIE_NAME = "cf_api_token"
ENCRYPTED_VALUE_PREFIX = "enc:"
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
# httpx logs every request URL at INFO. A webhook URL and the Telegram bot URL (bot token in the
# path) are secrets, so its request log stays off whatever LOG_LEVEL says.
logging.getLogger("httpx").setLevel(logging.WARNING)
