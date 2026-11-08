"""Backup snapshots on disk, their SQLite metadata, retention and restore history."""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.cloudflare import fetch_tunnel_configuration
from app.config import (
    BACKUP_DIR,
    BACKUP_RETENTION_DAYS_RAW,
    DB_PATH,
)
from app.credentials import get_saved_account_id
from app.db import BackupRecord, RestoreRecord, db
from app.notifications import queue_notification, render_notification_message
from app.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles

logger = logging.getLogger(__name__)


RETENTION_MAX_DAYS = 36500
RETENTION_INPUT_ERROR = "Retention must be a whole number of days from 1 to 36500, or empty to use the default."


def parse_retention_days_input(raw: str) -> int | None:
    """Parse the retention form value: empty means "use the default", else a whole number 1..36500."""
    text = raw.strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(RETENTION_INPUT_ERROR)
    days = int(text)
    if not 1 <= days <= RETENTION_MAX_DAYS:
        raise ValueError(RETENTION_INPUT_ERROR)
    return days


def _env_retention_days() -> int | None:
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


def _profile_value(profile_id: int | None) -> int | None:
    """The retention set on the profile itself (valid values only)."""
    if profile_id is None:
        return None
    try:
        stored = get_profile(profile_id).retention_days
    except LookupError:
        return None
    return stored if stored is not None and stored > 0 else None


def get_backup_retention_days(profile_id: int | None) -> int | None:
    """Profile value if set; else the environment value for Default (or no profile); else keep forever."""
    if profile_id is not None:
        try:
            stored = get_profile(profile_id).retention_days
        except LookupError:
            stored = None
        if stored is not None and stored > 0:
            return stored
        if stored is not None:
            logger.warning("Ignoring retention_days=%s for profile %s because it must be greater than zero.", stored, profile_id)
    if profile_id is None or profile_id == DEFAULT_PROFILE_ID:
        return _env_retention_days()
    return None


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


NOTES_MAX_LENGTH = 500


def clean_notes(raw: str | None) -> str | None:
    """A backup note as stored: stripped, with ``\n`` line ends; ``None`` when empty.

    Raises ``ValueError`` with a user-facing message when longer than ``NOTES_MAX_LENGTH``.
    """
    if raw is None:
        return None
    notes = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not notes:
        return None
    if len(notes) > NOTES_MAX_LENGTH:
        raise ValueError(f"Notes can be at most {NOTES_MAX_LENGTH} characters.")
    return notes


@dataclass(frozen=True)
class Snapshot:
    """A tunnel's configuration fetched from Cloudflare, ready to save or download; nothing written yet."""

    payload: dict[str, Any]
    created_at: str
    tunnel_name: str
    route_count: int
    filename: str


async def take_snapshot(account_id: str, tunnel_id: str, api_token: str, notes: str | None = None) -> Snapshot:
    """Fetch one tunnel and its configuration and build the backup payload; writes nothing."""
    tunnel_data, config_data = await fetch_tunnel_configuration(account_id, tunnel_id, api_token)
    tunnel = tunnel_data.get("result", {})
    config = config_data.get("result", {})

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tunnel_name = tunnel.get("name") or "Unknown tunnel"
    ingress = config.get("config", {}).get("ingress", [])
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:12]}.json"

    payload = {
        "exported_at": created_at,
        "account_id": account_id,
        "tunnel_id": tunnel_id,
        "tunnel": tunnel,
        "configuration": config,
        "notes": notes or "",
    }
    return Snapshot(
        payload=payload,
        created_at=created_at,
        tunnel_name=tunnel_name,
        route_count=len(ingress),
        filename=filename,
    )


def save_snapshot(snapshot: Snapshot, origin: str = "manual") -> BackupRecord:
    """Write the snapshot's JSON file and its database row, then apply retention."""
    payload = snapshot.payload
    account_id = payload["account_id"]
    tunnel_id = payload["tunnel_id"]
    stored_notes = payload.get("notes") or ""

    file_path = BACKUP_DIR / snapshot.filename
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    with closing(db()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.created_at,
                account_id,
                tunnel_id,
                snapshot.tunnel_name,
                snapshot.route_count,
                snapshot.filename,
                stored_notes,
                origin,
            ),
        )
        conn.commit()
        backup_id = cursor.lastrowid

    purge_expired_backups(datetime.fromisoformat(snapshot.created_at))

    return BackupRecord(
        id=backup_id,
        created_at=snapshot.created_at,
        account_id=account_id,
        tunnel_id=tunnel_id,
        tunnel_name=snapshot.tunnel_name,
        route_count=snapshot.route_count,
        filename=snapshot.filename,
        notes=stored_notes or None,
        origin=origin,
    )


async def create_backup(
    account_id: str,
    tunnel_id: str,
    api_token: str,
    notes: str | None = None,
    origin: str = "manual",
) -> BackupRecord:
    """Fetch a tunnel's configuration and save it as a backup (``take_snapshot`` + ``save_snapshot``)."""
    snapshot = await take_snapshot(account_id, tunnel_id, api_token, notes)
    return save_snapshot(snapshot, origin)


def _purge_group(
    conn,
    retention_days: int,
    reference_time: datetime,
    where: str,
    params: tuple,
) -> int:
    cutoff_iso = (reference_time - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
    rows = conn.execute(
        f"SELECT id, filename FROM backups WHERE created_at < ? AND {where} ORDER BY id ASC",
        (cutoff_iso, *params),
    ).fetchall()

    for row in rows:
        backup_id = int(row["id"])
        file_path = BACKUP_DIR / str(row["filename"])
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove expired backup file: %s", file_path)
        conn.execute("DELETE FROM restores WHERE backup_id = ?", (backup_id,))
        conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
    conn.commit()

    deleted_count = len(rows)
    if deleted_count:
        logger.info("Deleted %s expired backup(s) older than %s day(s) due to the retention policy.", deleted_count, retention_days)
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


def purge_expired_backups(reference_time: datetime | None = None) -> int:
    reference_time = reference_time or datetime.now(timezone.utc)
    deleted_total = 0
    claimed: list[str] = []

    with closing(db()) as conn:
        for profile in list_profiles():
            account_id = get_saved_account_id(profile.id)
            # No account: owns nothing. Shared account: the first profile in id order owns it.
            if not account_id or account_id in claimed:
                continue
            claimed.append(account_id)
            retention_days = get_backup_retention_days(profile.id)
            if retention_days is None:
                continue
            deleted_total += _purge_group(conn, retention_days, reference_time, "account_id = ?", (account_id,))

        # Backups of accounts that match no profile follow the environment rule.
        retention_days = get_backup_retention_days(None)
        if retention_days is not None:
            placeholders = ",".join("?" for _ in claimed)
            where = f"account_id NOT IN ({placeholders})" if claimed else "1 = 1"
            deleted_total += _purge_group(conn, retention_days, reference_time, where, tuple(claimed))

    return deleted_total


def get_backups() -> list[BackupRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin FROM backups ORDER BY id DESC"
        ).fetchall()
    return [BackupRecord(**dict(row)) for row in rows]


def count_backups_for_account(account_id: str) -> int:
    """Number of saved backups of one Cloudflare account (Account ID compared case-insensitively)."""
    with closing(db()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM backups WHERE lower(account_id) = lower(?)", (account_id,)).fetchone()
    return int(row[0])


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


def get_backup_retention_status(profile_id: int | None) -> dict[str, Any]:
    retention_days = get_backup_retention_days(profile_id)
    uses_env = profile_id is None or profile_id == DEFAULT_PROFILE_ID
    default_days = _env_retention_days() if uses_env else None
    if retention_days is None:
        source, label = "none", "Kept until deleted (no retention set)"
    elif retention_days == default_days and not _profile_value(profile_id):
        source, label = "environment", f"Kept for {retention_days} days (from BACKUP_RETENTION_DAYS)"
    else:
        source, label = "profile", f"Kept for {retention_days} days (set on this profile)"
    extra = {"source": source, "label": label, "default_days": default_days}
    if retention_days is None:
        if profile_id is None or profile_id == DEFAULT_PROFILE_ID:
            detail = "BACKUP_RETENTION_DAYS is not currently set, so Tikka Masala will not delete old backups automatically."
        else:
            detail = "No retention period is set for this profile, so Tikka Masala will not delete its old backups automatically."
        return {
            "enabled": False,
            "days": None,
            "summary": "Full archive retention is active. Backups are kept until you remove them manually.",
            "detail": detail,
            **extra,
        }

    return {
        "enabled": True,
        "days": retention_days,
        "summary": f"Automatic retention is active. Backups older than {retention_days} day(s) are deleted when new backups are created.",
        "detail": "Cleanup removes both the JSON snapshot and its related restore history from the database.",
        **extra,
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
