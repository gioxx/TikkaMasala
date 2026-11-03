"""SQLite schema, connection helper, records and the key/value settings table."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from app.config import DB_PATH


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
