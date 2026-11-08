"""Profile repository: one row of ``profiles`` per Cloudflare account."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response

from app.config import DEMO_MODE
from app.db import ProfileRecord, db
from app.migrations import DEFAULT_PROFILE_ID

__all__ = [
    "ACTIVE_PROFILE_COOKIE",
    "ACTIVE_PROFILE_MAX_AGE",
    "DEFAULT_NOT_REMOVABLE",
    "DEFAULT_PROFILE_ID",
    "PROFILE_NAME_MAX",
    "RESERVED_PROFILE_NAMES",
    "create_profile",
    "current_profile_id",
    "delete_profile_rows",
    "get_profile",
    "list_profiles",
    "normalize_profile_name",
    "parse_profile_id",
    "profile_exists",
    "rename_profile",
    "set_active_profile_cookie",
    "stale_profile_cookie_guard",
    "switch_destination",
    "update_profile",
]

ACTIVE_PROFILE_COOKIE = "tm_profile"
ACTIVE_PROFILE_MAX_AGE = 31_536_000  # one year, in seconds

# A profile id as it may appear in the cookie or a form: no sign, no leading zero, at most ten digits.
_PROFILE_ID_PATTERN = re.compile(r"[1-9][0-9]{0,9}")

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


def list_profiles(conn: sqlite3.Connection | None = None) -> list[ProfileRecord]:
    """Every profile in id order; reads through ``conn`` when given (e.g. inside a transaction)."""
    if conn is not None:
        return [_to_record(row) for row in conn.execute(f"{_SELECT} ORDER BY id").fetchall()]
    with closing(db()) as own:
        rows = own.execute(f"{_SELECT} ORDER BY id").fetchall()
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


def create_profile(
    name: str, *, account_id: str | None = None, api_token: str | None = None
) -> ProfileRecord:
    """Insert a profile. ``api_token`` is stored as given: the caller encrypts it first."""
    with closing(db()) as conn:
        cursor = conn.execute(
            "INSERT INTO profiles (name, account_id, api_token, created_at) VALUES (?, ?, ?, ?)",
            (name, account_id, api_token, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        profile_id = int(cursor.lastrowid)
    return get_profile(profile_id)


PROFILE_NAME_MAX = 40
# "Other accounts" names the Backups tab of backups no profile covers.
RESERVED_PROFILE_NAMES = frozenset({"other accounts"})
# Bidirectional formatting characters: they can make a name read differently from what it is.
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _is_control_character(ch: str) -> bool:
    # Cf covers zero-width and bidi formatting characters (U+200B, U+FEFF, U+202E, ...).
    return unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} or ch in _BIDI_CONTROLS


def normalize_profile_name(raw: str, *, exclude_id: int | None = None) -> str:
    """A profile name as stored: stripped and checked. Raises ``ValueError`` with a user-facing message."""
    name = (raw or "").strip()
    if not name:
        raise ValueError("Enter a profile name.")
    if len(name) > PROFILE_NAME_MAX:
        raise ValueError(f"Profile names can be at most {PROFILE_NAME_MAX} characters.")
    if any(_is_control_character(ch) for ch in name):
        raise ValueError("Profile names cannot contain control characters.")
    folded = name.casefold()
    if folded in RESERVED_PROFILE_NAMES:
        raise ValueError("That name is reserved.")
    for profile in list_profiles():
        if profile.id != exclude_id and profile.name.casefold() == folded:
            raise ValueError(f"A profile named '{profile.name}' already exists.")
    return name


def rename_profile(profile_id: int, name: str) -> ProfileRecord:
    """Give a profile a new name (checked like a new one). ``LookupError`` when the profile is missing."""
    get_profile(profile_id)
    update_profile(profile_id, name=normalize_profile_name(name, exclude_id=profile_id))
    return get_profile(profile_id)


DEFAULT_NOT_REMOVABLE = "The first profile cannot be removed: environment variables and setup belong to it."


def delete_profile_rows_in(conn: sqlite3.Connection, profile_id: int) -> None:
    """Delete a profile's destinations, run history and row through ``conn``; the caller commits.

    ``ValueError(DEFAULT_NOT_REMOVABLE)`` for Default, ``LookupError`` when the profile is missing.
    """
    if profile_id == DEFAULT_PROFILE_ID:
        raise ValueError(DEFAULT_NOT_REMOVABLE)
    conn.execute("DELETE FROM notification_destinations WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM scheduled_runs WHERE profile_id = ?", (profile_id,))
    cursor = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    if cursor.rowcount == 0:
        raise LookupError(f"Profile {profile_id} does not exist.")


def delete_profile_rows(profile_id: int) -> None:
    """Delete a profile with its destinations and run history, in one transaction."""
    if profile_id == DEFAULT_PROFILE_ID:
        raise ValueError(DEFAULT_NOT_REMOVABLE)
    with closing(db()) as conn:
        try:
            delete_profile_rows_in(conn, profile_id)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def profile_exists(profile_id: int) -> bool:
    with closing(db()) as conn:
        row = conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return row is not None


def parse_profile_id(raw: str | None) -> int | None:
    """The id of an existing profile written in ``raw``, or ``None`` when malformed or unknown."""
    if raw is None or not _PROFILE_ID_PATTERN.fullmatch(raw):
        return None
    profile_id = int(raw)
    return profile_id if profile_exists(profile_id) else None


def current_profile_id(request: Request) -> int:
    """The profile the request works on: the ``tm_profile`` cookie when it names an existing profile.

    Demo mode always works on Default. A missing, malformed or unknown cookie means Default; a
    rejected cookie is flagged on ``request.state.stale_profile_cookie`` so the response can drop
    it. The answer is cached on ``request.state.active_profile_id``.
    """
    cached = getattr(request.state, "active_profile_id", None)
    if cached is not None:
        return cached
    profile_id = DEFAULT_PROFILE_ID
    if not DEMO_MODE:
        raw = request.cookies.get(ACTIVE_PROFILE_COOKIE)
        if raw is not None:
            parsed = parse_profile_id(raw)
            if parsed is None:
                request.state.stale_profile_cookie = True
            else:
                profile_id = parsed
    request.state.active_profile_id = profile_id
    return profile_id


def set_active_profile_cookie(response: Response, profile_id: int) -> None:
    # Not Secure: the id is not a secret and the app is often reached over plain http on the LAN.
    response.set_cookie(
        ACTIVE_PROFILE_COOKIE,
        str(profile_id),
        max_age=ACTIVE_PROFILE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
    )


# First path segment -> where a switch lands. Pages tied to one profile's records go to their section root.
_SWITCH_SECTION_ROOTS = {
    "backup": "/backups",
    "setup": "/",
}
_SWITCH_SUBSECTION_ROOTS = {
    ("automation", "destinations"): "/automation",
    ("settings", "profiles"): "/settings",
}


def switch_destination(next_url: str) -> str:
    """The local path to land on after switching profile from ``next_url``. Path only, never a query."""
    path = next_url.split("#", 1)[0].split("?", 1)[0]
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in path)
    ):
        return "/"
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "/"
    if segments[0] in _SWITCH_SECTION_ROOTS:
        return _SWITCH_SECTION_ROOTS[segments[0]]
    if len(segments) >= 2 and (segments[0], segments[1]) in _SWITCH_SUBSECTION_ROOTS:
        return _SWITCH_SUBSECTION_ROOTS[(segments[0], segments[1])]
    return path


def _sets_active_profile_cookie(response: Response) -> bool:
    prefix = f"{ACTIVE_PROFILE_COOKIE}=".encode()
    return any(
        name.lower() == b"set-cookie" and value.startswith(prefix) for name, value in response.raw_headers
    )


async def stale_profile_cookie_guard(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Drop a ``tm_profile`` cookie that no longer names a profile (unless the handler set a new one)."""
    if ACTIVE_PROFILE_COOKIE not in request.cookies:
        return await call_next(request)
    current_profile_id(request)
    response = await call_next(request)
    if getattr(request.state, "stale_profile_cookie", False) and not _sets_active_profile_cookie(response):
        response.delete_cookie(ACTIVE_PROFILE_COOKIE, path="/", httponly=True, samesite="lax")
    return response
