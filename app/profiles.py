"""Profile repository: one row of ``profiles`` per Cloudflare account."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from app.db import ProfileRecord, db
from app.migrations import DEFAULT_PROFILE_ID

__all__ = [
    "DEFAULT_PROFILE_ID",
    "create_profile",
    "current_profile_id",
    "get_profile",
    "list_profiles",
    "update_profile",
]

_COLUMNS = (
    "id",
    "name",
    "account_id",
    "api_token",
    "auto_backup_enabled",
    "auto_backup_cron",
    "tunnel_schedule",
    "retention_days",
    "last_run_at",
    "last_status",
    "created_at",
)

# Columns callers may change. SQL for updates is built only from these names.
_UPDATABLE_COLUMNS = frozenset(_COLUMNS) - {"id", "created_at"}

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM profiles"


def _to_record(row: sqlite3.Row) -> ProfileRecord:
    return ProfileRecord(
        id=int(row["id"]),
        name=str(row["name"]),
        account_id=row["account_id"],
        api_token=row["api_token"],
        auto_backup_enabled=bool(row["auto_backup_enabled"]),
        auto_backup_cron=row["auto_backup_cron"],
        tunnel_schedule=row["tunnel_schedule"],
        retention_days=None if row["retention_days"] is None else int(row["retention_days"]),
        last_run_at=row["last_run_at"],
        last_status=row["last_status"],
        created_at=str(row["created_at"]),
    )


def get_profile(profile_id: int) -> ProfileRecord:
    with closing(db()) as conn:
        row = conn.execute(f"{_SELECT} WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise LookupError(f"Profile {profile_id} does not exist.")
    return _to_record(row)


def list_profiles() -> list[ProfileRecord]:
    with closing(db()) as conn:
        rows = conn.execute(f"{_SELECT} ORDER BY id").fetchall()
    return [_to_record(row) for row in rows]


def update_profile(profile_id: int, **fields: Any) -> None:
    """Set the given columns of a profile. Pass ``None`` to clear a column."""
    unknown = sorted(name for name in fields if name not in _UPDATABLE_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown or read-only profile column(s): {', '.join(unknown)}")
    if not fields:
        return
    names = sorted(fields)
    values = [
        (1 if fields[name] else 0) if name == "auto_backup_enabled" else fields[name]
        for name in names
    ]
    assignments = ", ".join(f"{name} = ?" for name in names)
    with closing(db()) as conn:
        cursor = conn.execute(
            f"UPDATE profiles SET {assignments} WHERE id = ?", (*values, profile_id)
        )
        conn.commit()
    if cursor.rowcount == 0:
        raise LookupError(f"Profile {profile_id} does not exist.")


def create_profile(name: str) -> ProfileRecord:
    with closing(db()) as conn:
        cursor = conn.execute(
            "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
            (name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        profile_id = int(cursor.lastrowid)
    return get_profile(profile_id)


def current_profile_id(request: Request) -> int:
    """The profile the request works on. Always Default until profile switching lands."""
    return DEFAULT_PROFILE_ID
