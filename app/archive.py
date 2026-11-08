"""Backup archive queries: scope (active profile or other accounts), filters and pagination."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import urlencode

from app.config import BACKUPS_PAGE_SIZE, BACKUPS_PAGE_SIZE_OPTIONS
from app.credentials import get_saved_account_id
from app.db import BackupRecord, db
from app.profiles import list_profiles

ARCHIVE_SCOPES = ("profile", "other")
ARCHIVE_ORIGINS = ("manual", "automatic")
TUNNEL_PARAM_MAX = 128


@dataclass(frozen=True)
class ArchiveQuery:
    scope: str = "profile"
    tunnel_id: str | None = None
    origin: str | None = None
    page: int = 1
    page_size: int = BACKUPS_PAGE_SIZE


def parse_archive_query(params: Mapping[str, str]) -> ArchiveQuery:
    scope = params.get("scope") or "profile"
    if scope not in ARCHIVE_SCOPES:
        scope = "profile"
    origin = params.get("origin")
    if origin not in ARCHIVE_ORIGINS:
        origin = None
    tunnel = (params.get("tunnel") or "").strip()
    tunnel_id = tunnel if tunnel and len(tunnel) <= TUNNEL_PARAM_MAX else None
    try:
        page = int((params.get("page") or "1").strip())
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    try:
        page_size = int((params.get("page_size") or "").strip())
    except ValueError:
        page_size = BACKUPS_PAGE_SIZE
    if page_size not in BACKUPS_PAGE_SIZE_OPTIONS:
        page_size = BACKUPS_PAGE_SIZE
    return ArchiveQuery(scope=scope, tunnel_id=tunnel_id, origin=origin, page=page, page_size=page_size)


def archive_url(query: ArchiveQuery, **changes: Any) -> str:
    query = replace(query, **changes)
    pairs: list[tuple[str, Any]] = []
    if query.scope != "profile":
        pairs.append(("scope", query.scope))
    if query.tunnel_id:
        pairs.append(("tunnel", query.tunnel_id))
    if query.origin:
        pairs.append(("origin", query.origin))
    if query.page > 1:
        pairs.append(("page", query.page))
    if query.page_size != BACKUPS_PAGE_SIZE:
        pairs.append(("page_size", query.page_size))
    return "/backups" + (f"?{urlencode(pairs)}" if pairs else "")


def profile_account_ids() -> list[str]:
    seen: list[str] = []
    for profile in list_profiles():
        account_id = get_saved_account_id(profile.id).strip().casefold()
        if account_id and account_id not in seen:
            seen.append(account_id)
    return seen


@dataclass(frozen=True)
class ArchivePage:
    query: ArchiveQuery
    account_id: str | None
    backups: list[BackupRecord]
    pagination: dict[str, int | bool]
    tunnel_options: list[tuple[str, str]]
    profile_count: int
    other_count: int

    @property
    def filtered(self) -> bool:
        return bool(self.query.tunnel_id or self.query.origin)


def _scope_clause(scope: str, account_id: str | None, others: list[str]) -> tuple[str, list[str]]:
    if scope == "profile":
        if not account_id:
            return "0", []
        return "lower(account_id) = lower(?)", [account_id]
    if not others:
        return "1", []
    return f"lower(account_id) NOT IN ({', '.join('?' * len(others))})", list(others)


def build_archive_page(profile_id: int, query: ArchiveQuery) -> ArchivePage:
    account_id = get_saved_account_id(profile_id).strip() or None
    others = profile_account_ids()
    scope_sql, scope_args = _scope_clause(query.scope, account_id, others)
    profile_sql, profile_args = _scope_clause("profile", account_id, others)
    other_sql, other_args = _scope_clause("other", account_id, others)

    where = [scope_sql]
    args: list[Any] = list(scope_args)
    if query.tunnel_id:
        where.append("tunnel_id = ?")
        args.append(query.tunnel_id)
    if query.origin:
        where.append("origin = ?")
        args.append(query.origin)
    where_sql = " AND ".join(where)
    page_size = query.page_size

    with closing(db()) as conn:
        total_count = int(conn.execute(f"SELECT COUNT(*) FROM backups WHERE {where_sql}", args).fetchone()[0])
        total_pages = max((total_count + page_size - 1) // page_size, 1)
        current_page = min(max(query.page, 1), total_pages)
        offset = (current_page - 1) * page_size
        rows = conn.execute(
            f"""
            SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin
            FROM backups WHERE {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (*args, page_size, offset),
        ).fetchall()
        profile_count = int(conn.execute(f"SELECT COUNT(*) FROM backups WHERE {profile_sql}", profile_args).fetchone()[0])
        other_count = int(conn.execute(f"SELECT COUNT(*) FROM backups WHERE {other_sql}", other_args).fetchone()[0])
        option_rows = conn.execute(
            f"""
            SELECT b.tunnel_id, b.tunnel_name FROM backups b
            WHERE b.id IN (
                SELECT MAX(id) FROM backups WHERE {scope_sql} GROUP BY tunnel_id
            )
            """,
            scope_args,
        ).fetchall()

    backups = [BackupRecord(**dict(row)) for row in rows]
    options = sorted(
        ((row["tunnel_id"], row["tunnel_name"] or row["tunnel_id"]) for row in option_rows),
        key=lambda option: option[1].casefold(),
    )
    pagination: dict[str, int | bool] = {
        "current_page": current_page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
        "previous_page": current_page - 1,
        "next_page": current_page + 1,
        "start_index": offset + 1 if backups else 0,
        "end_index": offset + len(backups),
    }
    return ArchivePage(
        query=replace(query, page=current_page),
        account_id=account_id,
        backups=backups,
        pagination=pagination,
        tunnel_options=options,
        profile_count=profile_count,
        other_count=other_count,
    )
