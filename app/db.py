"""SQLite schema, connection helper, records and the key/value settings table."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from app.config import DB_PATH
from app.migrations import apply_migrations


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
class ProfileRecord:
    id: int
    name: str
    account_id: str | None
    api_token: str | None
    auto_backup_enabled: bool
    auto_backup_cron: str | None
    tunnel_schedule: str | None
    retention_days: int | None
    last_run_at: str | None
    last_status: str | None
    created_at: str


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
        apply_migrations(conn, DB_PATH)


init_db()


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
