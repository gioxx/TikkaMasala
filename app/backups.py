"""Backup snapshots on disk, their SQLite metadata, retention and restore history."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.cloudflare import fetch_tunnel_configuration
from app.config import (
    BACKUP_DIR,
    BACKUP_RETENTION_DAYS_RAW,
    BACKUPS_PAGE_SIZE,
    BACKUPS_PAGE_SIZE_OPTIONS,
    DB_PATH,
)
from app.db import BackupRecord, RestoreRecord, db
from app.notifications import queue_notification, render_notification_message

logger = logging.getLogger(__name__)


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


async def create_backup(
    account_id: str,
    tunnel_id: str,
    api_token: str,
    notes: str | None = None,
    origin: str = "manual",
) -> BackupRecord:
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
            INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes or "", origin),
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
        origin=origin,
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


def get_backups() -> list[BackupRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin FROM backups ORDER BY id DESC"
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
            SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin
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


def get_backup(backup_id: int) -> BackupRecord:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin FROM backups WHERE id = ?",
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


def get_latest_backups_per_tunnel() -> list[BackupRecord]:
    """Return the most recent backup of every tunnel, ordered by tunnel name."""

    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.created_at, b.account_id, b.tunnel_id, b.tunnel_name, b.route_count, b.filename, b.notes, b.origin
            FROM backups b
            JOIN (SELECT tunnel_id, MAX(id) AS id FROM backups GROUP BY tunnel_id) latest ON latest.id = b.id
            ORDER BY b.tunnel_name COLLATE NOCASE, b.tunnel_id
            """
        ).fetchall()
    return [BackupRecord(**dict(row)) for row in rows]
