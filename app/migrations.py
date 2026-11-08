"""Versioned SQLite schema migrations, tracked with ``PRAGMA user_version``.

``MIGRATIONS[i]`` upgrades the schema from version ``i`` to ``i + 1``. Pending
migrations run together with the version bump in one ``BEGIN IMMEDIATE``
transaction, so a crash leaves the database at its previous version and the
next start simply retries.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LATEST_SCHEMA_VERSION = 1

DEFAULT_PROFILE_ID = 1

# The schema as it was before versioning (user_version 0).
_LEGACY_DDL = (
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
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS restores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_id INTEGER NOT NULL,
        restored_at TEXT NOT NULL,
        account_id TEXT NOT NULL,
        tunnel_id TEXT NOT NULL,
        FOREIGN KEY (backup_id) REFERENCES backups(id)
    )
    """,
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
    """,
)

# Settings keys that become columns of the Default profile (settings key -> column).
_PROFILE_SETTINGS = {
    "account_id": "account_id",
    "api_token": "api_token",
    "auto_backup_enabled": "auto_backup_enabled",
    "auto_backup_cron": "auto_backup_cron",
    "auto_backup_tunnel_schedule": "tunnel_schedule",
    "auto_backup_last_run_at": "last_run_at",
    "auto_backup_last_status": "last_status",
}

# Same truthy set as app.scheduler.get_auto_backup_enabled.
_TRUTHY = {"1", "true", "yes", "on"}


def _migration_1(conn: sqlite3.Connection) -> None:
    """Introduce profiles and move the single-account settings into "Default"."""
    for ddl in _LEGACY_DDL:
        conn.execute(ddl)

    conn.execute(
        """
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            account_id TEXT,
            api_token TEXT,
            auto_backup_enabled INTEGER NOT NULL DEFAULT 0,
            auto_backup_cron TEXT,
            tunnel_schedule TEXT,
            retention_days INTEGER,
            last_run_at TEXT,
            last_status TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE notification_destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL REFERENCES profiles(id),
            kind TEXT NOT NULL CHECK (kind IN ('webhook', 'telegram')),
            label TEXT,
            target TEXT NOT NULL,
            chat_id TEXT,
            events TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"ALTER TABLE scheduled_runs ADD COLUMN profile_id INTEGER NOT NULL DEFAULT {DEFAULT_PROFILE_ID}"
    )
    conn.execute("ALTER TABLE backups ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
    conn.execute(
        "UPDATE backups SET origin = 'automatic' WHERE notes LIKE 'Automatic backup triggered by%'"
    )
    conn.execute("CREATE INDEX idx_backups_account_id ON backups(account_id)")

    keys = list(_PROFILE_SETTINGS)
    placeholders = ", ".join("?" for _ in keys)
    stored = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})", keys
        )
    }
    enabled = (stored.get("auto_backup_enabled") or "").strip().lower() in _TRUTHY
    conn.execute(
        """
        INSERT INTO profiles (
            id, name, account_id, api_token, auto_backup_enabled, auto_backup_cron,
            tunnel_schedule, retention_days, last_run_at, last_status, created_at
        ) VALUES (?, 'Default', ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            DEFAULT_PROFILE_ID,
            stored.get("account_id"),
            stored.get("api_token"),
            1 if enabled else 0,
            stored.get("auto_backup_cron"),
            stored.get("auto_backup_tunnel_schedule"),
            stored.get("auto_backup_last_run_at"),
            stored.get("auto_backup_last_status"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", keys)


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migration_1]


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _has_data(conn: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table in ("backups", "settings", "scheduled_runs"):
        if table in tables and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return True
    return False


def _write_backup(conn: sqlite3.Connection, target: Path) -> None:
    """Copy the database to ``target`` (overwriting it), readable by the owner only."""
    target.unlink(missing_ok=True)
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(target, 0o600)


def apply_migrations(conn: sqlite3.Connection, db_path: Path | None = None) -> int:
    """Bring the schema up to ``LATEST_SCHEMA_VERSION`` and return the resulting version.

    Refuses (without writing anything) a database created by a newer app. When
    ``db_path`` is given and an unversioned database already holds data, it is
    first copied to ``<db_path>.pre-v1.bak``.
    """
    current = _user_version(conn)
    if current > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this app supports "
            f"({LATEST_SCHEMA_VERSION}). Upgrade TikkaMasala, or restore a backup "
            "taken before the newer version was started."
        )
    if current == LATEST_SCHEMA_VERSION:
        return current

    if current == 0 and db_path is not None and _has_data(conn):
        backup_path = Path(f"{db_path}.pre-v1.bak")
        _write_backup(conn, backup_path)
        logger.info("Saved a copy of the database before migrating: %s.", backup_path)

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # explicit transaction control below
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock: another process may have migrated meanwhile.
            current = _user_version(conn)
            logger.info("Migrating database schema from version %s to %s.", current, LATEST_SCHEMA_VERSION)
            for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
                MIGRATIONS[version - 1](conn)
                conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous_isolation
    logger.info("Database schema migrated to version %s.", LATEST_SCHEMA_VERSION)
    return _user_version(conn)
