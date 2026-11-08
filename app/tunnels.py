"""Tunnels home: summary tiles and tunnel rows from local data plus one cached tunnel list call.

Hostname counts, host-local origins and last backups come from the latest
saved snapshot of each tunnel of the active profile's account; the tunnel set
and each tunnel's status come from a single ``list_tunnels`` call, cached for a
short time per (account, token).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.backups import count_backups_for_account, get_backup_retention_status
from app.cloudflare import list_tunnels
from app.config import DEMO_MODE
from app.credentials import (
    DEMO_NO_CREDENTIALS_MESSAGE,
    MISSING_CREDENTIALS_MESSAGE,
    ApiCredentials,
    get_api_credentials,
    get_saved_account_id,
)
from app.ingress_overview import IngressOverview, build_overview, snapshot_overview
from app.scheduler import build_auto_backup_status, get_auto_backup_timezone

logger = logging.getLogger(__name__)

TUNNEL_LIST_TTL_SECONDS = 30.0
HOME_TUNNEL_LIST_TIMEOUT_SECONDS = 8.0
KNOWN_STATUSES = ("healthy", "degraded", "down", "inactive")
UNHEALTHY_STATUSES = ("degraded", "down")
TUNNEL_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# (account_id.lower(), sha256(token)) -> (stored at, monotonic seconds; tunnel list)
_tunnel_list_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def _cache_key(creds: ApiCredentials) -> tuple[str, str]:
    return creds.account_id.lower(), hashlib.sha256(creds.api_token.encode("utf-8")).hexdigest()


async def fetch_tunnel_list(creds: ApiCredentials, *, fresh: bool = False) -> list[dict[str, Any]]:
    """The account's tunnel list, from the cache when younger than the TTL (unless ``fresh``).

    Only successful answers are cached. A Cloudflare call slower than
    ``HOME_TUNNEL_LIST_TIMEOUT_SECONDS`` becomes a 504 ``HTTPException``.
    """
    key = _cache_key(creds)
    if not fresh:
        cached = _tunnel_list_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < TUNNEL_LIST_TTL_SECONDS:
            return cached[1]
    try:
        result = await asyncio.wait_for(
            list_tunnels(creds.account_id, creds.api_token), HOME_TUNNEL_LIST_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Cloudflare did not answer within {HOME_TUNNEL_LIST_TIMEOUT_SECONDS:g} seconds.",
        ) from exc
    _tunnel_list_cache[key] = (time.monotonic(), result)
    return result


def clear_tunnel_list_cache() -> None:
    _tunnel_list_cache.clear()


@dataclass(frozen=True)
class Tile:
    key: str  # "tunnels" | "hostnames" | "auto_backup" | "backups"
    label: str
    value: str
    detail: str
    tone: str  # "ok" | "warn" | "error" | "muted"
    href: str | None


@dataclass(frozen=True)
class TunnelRow:
    tunnel_id: str
    name: str
    status: str
    hostname_count: int | None
    last_backup_at: str | None
    last_backup_id: int | None


@dataclass(frozen=True)
class RowFeedback:
    ok: bool
    message: str
    backup_id: int | None = None


@dataclass(frozen=True)
class TunnelsHome:
    account_id: str | None
    tiles: list[Tile]
    rows: list[TunnelRow]
    list_error: str | None
    credentials_message: str | None

    def tile(self, key: str) -> Tile:
        for tile in self.tiles:
            if tile.key == key:
                return tile
        raise KeyError(key)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_when(value: str | None, *, now: datetime | None = None, tz: ZoneInfo | None = None) -> str:
    """A short, human time in ``tz`` (default: the scheduler timezone): today/yesterday/tomorrow HH:MM,
    "N days ago" up to six days, else the date."""
    if not value:
        return "never"
    moment = _parse_iso(value)
    if moment is None:
        return value
    tz = tz or get_auto_backup_timezone()
    now = now or datetime.now(timezone.utc)
    local = moment.astimezone(tz)
    days = (now.astimezone(tz).date() - local.date()).days
    if days == 0:
        return f"today {local:%H:%M}"
    if days == 1:
        return f"yesterday {local:%H:%M}"
    if days == -1:
        return f"tomorrow {local:%H:%M}"
    if 2 <= days <= 6:
        return f"{days} days ago"
    return f"{local:%Y-%m-%d}"


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in KNOWN_STATUSES else "unknown"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _tunnels_tile(
    tunnel_list: list[dict[str, Any]] | None, list_error: str | None, credentials_message: str | None
) -> Tile:
    def tile(value: str, detail: str, tone: str) -> Tile:
        return Tile(key="tunnels", label="Tunnels", value=value, detail=detail, tone=tone, href=None)

    if credentials_message is not None:
        return tile("—", "No credentials", "muted")
    if list_error is not None or tunnel_list is None:
        return tile("—", "Status unavailable", "error")
    if not tunnel_list:
        return tile("0", "No tunnels in this account", "muted")
    statuses = [_normalize_status(tunnel.get("status")) for tunnel in tunnel_list]
    unhealthy = [f"{statuses.count(status)} {status}" for status in UNHEALTHY_STATUSES if statuses.count(status)]
    if unhealthy:
        return tile(str(len(tunnel_list)), ", ".join(unhealthy), "warn")
    detail = "All healthy"
    inactive = statuses.count("inactive")
    if inactive:
        detail += f", {inactive} inactive"
    return tile(str(len(tunnel_list)), detail, "ok")


def _hostnames_tile(overview: IngressOverview) -> Tile:
    def tile(value: str, detail: str, tone: str) -> Tile:
        return Tile(key="hostnames", label="Hostnames", value=value, detail=detail, tone=tone, href="/ingress")

    if not overview.tunnels:
        return tile("0", "No snapshots yet", "muted")
    fragile = overview.fragile_count
    if fragile:
        return tile(str(overview.hostname_count), _plural(fragile, "host-local origin", "host-local origins"), "warn")
    return tile(str(overview.hostname_count), "No host-local origins", "ok")


def _auto_backup_tile(profile_id: int) -> Tile:
    def tile(value: str, detail: str, tone: str) -> Tile:
        return Tile(key="auto_backup", label="Auto backup", value=value, detail=detail, tone=tone, href="/automation")

    if DEMO_MODE:
        return tile("Disabled", "Not available in demo mode", "muted")
    status = build_auto_backup_status(profile_id)
    if not status.get("enabled"):
        return tile("Disabled", "Turn it on in Automation", "muted")
    if not (status.get("prereqs") or {}).get("ready"):
        return tile("Failing", "Missing credentials", "error")
    last_status = status.get("last_status")
    last_run_at = status.get("last_run_at")
    if last_status in ("partial", "failed"):
        return tile("Failing", f"Last run {last_status} {format_when(last_run_at)}", "error")
    next_run_at = status.get("next_run_at")
    next_text = f"next {format_when(next_run_at)}" if next_run_at else "next not scheduled"
    detail = f"Last {format_when(last_run_at)}, {next_text}"
    if last_status is None:
        return tile("Waiting", detail, "muted")
    return tile("OK", detail, "ok")


def _backups_tile(profile_id: int, account_id: str | None) -> Tile:
    count = count_backups_for_account(account_id) if account_id else 0
    retention = get_backup_retention_status(profile_id)
    detail = f"Kept for {retention['days']} days" if retention["enabled"] else "Kept until deleted"
    return Tile(key="backups", label="Saved backups", value=str(count), detail=detail, tone="muted", href="/backups")


def _rows(tunnel_list: list[dict[str, Any]] | None, overview: IngressOverview) -> list[TunnelRow]:
    hostnames: dict[str, set[str]] = {}
    for row in overview.rows:
        if row.rule.hostname:
            hostnames.setdefault(row.tunnel_id.lower(), set()).add(row.rule.hostname.lower())
    snapshots = {source.tunnel_id.lower(): source for source in overview.tunnels}

    def build_row(tunnel_id: str, name: str, status: str) -> TunnelRow:
        source = snapshots.get(tunnel_id.lower())
        readable = source is not None and source.error is None
        return TunnelRow(
            tunnel_id=tunnel_id,
            name=name,
            status=status,
            hostname_count=len(hostnames.get(tunnel_id.lower(), ())) if readable else None,
            last_backup_at=source.snapshot_at if source is not None else None,
            last_backup_id=source.backup_id if source is not None else None,
        )

    if tunnel_list is not None:
        rows = [
            build_row(str(tunnel["id"]), str(tunnel.get("name") or "Unknown tunnel"), _normalize_status(tunnel.get("status")))
            for tunnel in tunnel_list
        ]
    else:
        rows = [build_row(source.tunnel_id, source.tunnel_name, "unknown") for source in overview.tunnels]
    rows.sort(key=lambda row: (row.name.casefold(), row.tunnel_id))
    return rows


async def build_tunnels_home(profile_id: int, *, fresh: bool = False) -> TunnelsHome:
    """Tiles and rows of the Tunnels home for one profile; never raises on a Cloudflare failure."""
    account_id = get_saved_account_id(profile_id) or None
    overview = snapshot_overview(account_id) if account_id else build_overview("snapshot", [])

    creds = get_api_credentials(profile_id)
    credentials_message: str | None = None
    list_error: str | None = None
    tunnel_list: list[dict[str, Any]] | None = None
    if creds is None:
        credentials_message = DEMO_NO_CREDENTIALS_MESSAGE if DEMO_MODE else MISSING_CREDENTIALS_MESSAGE
    else:
        try:
            raw_list = await fetch_tunnel_list(creds, fresh=fresh)
            tunnel_list = [tunnel for tunnel in raw_list if isinstance(tunnel, dict) and tunnel.get("id")]
        except HTTPException as exc:
            list_error = str(exc.detail)
        except Exception:
            logger.exception("Unexpected error while listing tunnels (account_id=%s).", creds.account_id)
            list_error = "Unexpected error while listing tunnels."

    tiles = [
        _tunnels_tile(tunnel_list, list_error, credentials_message),
        _hostnames_tile(overview),
        _auto_backup_tile(profile_id),
        _backups_tile(profile_id, account_id),
    ]
    rows = _rows(tunnel_list, overview) if credentials_message is None else []
    return TunnelsHome(
        account_id=account_id,
        tiles=tiles,
        rows=rows,
        list_error=list_error,
        credentials_message=credentials_message,
    )
