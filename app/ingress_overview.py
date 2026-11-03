"""Every hostname → origin mapping across all tunnels, from snapshots or live.

``snapshot_overview`` reads the latest saved backup of each tunnel (no API
calls, works offline); ``live_overview`` asks Cloudflare for the current
configuration of every tunnel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException

from app.backups import get_latest_backups_per_tunnel
from app.cloudflare import cloudflare_get, list_tunnels
from app.config import BACKUP_DIR
from app.ingress import IngressRule, extract_ingress

logger = logging.getLogger(__name__)

# Parallel configuration requests in live mode; keeps large accounts well
# below Cloudflare's API rate limit.
LIVE_FETCH_CONCURRENCY = 5


@dataclass
class TunnelSource:
    """Where one tunnel's rules came from, and anything that went wrong."""

    tunnel_id: str
    tunnel_name: str
    rule_count: int = 0
    backup_id: int | None = None
    snapshot_at: str | None = None
    error: str | None = None
    note: str | None = None


@dataclass
class OverviewRow:
    tunnel_id: str
    tunnel_name: str
    position: int  # 1-based rule order inside the tunnel (first match wins)
    rule: IngressRule
    backup_id: int | None = None
    snapshot_at: str | None = None
    shared_with: list[str] = field(default_factory=list)

    @property
    def link(self) -> str | None:
        hostname = self.rule.hostname
        if not hostname or "*" in hostname:
            return None
        return f"https://{hostname}"

    @property
    def search_text(self) -> str:
        parts = [self.rule.hostname or "catch-all", self.rule.path or "", self.rule.service, self.tunnel_name]
        return " ".join(parts).lower()


@dataclass
class IngressOverview:
    source: str  # "snapshot" | "live"
    rows: list[OverviewRow] = field(default_factory=list)
    tunnels: list[TunnelSource] = field(default_factory=list)
    generated_at: str | None = None
    error: str | None = None

    @property
    def hostname_count(self) -> int:
        return len({row.rule.hostname.lower() for row in self.rows if row.rule.hostname})

    @property
    def fragile_count(self) -> int:
        return sum(1 for row in self.rows if row.rule.is_fragile)

    @property
    def catch_all_count(self) -> int:
        return sum(1 for row in self.rows if not row.rule.hostname)

    @property
    def shared_hostnames(self) -> list[str]:
        return sorted({row.rule.hostname.lower() for row in self.rows if row.shared_with and row.rule.hostname})

    @property
    def tunnels_with_errors(self) -> list[TunnelSource]:
        return [tunnel for tunnel in self.tunnels if tunnel.error]

    @property
    def tunnels_with_notes(self) -> list[TunnelSource]:
        return [tunnel for tunnel in self.tunnels if tunnel.note and not tunnel.error]


def build_overview(
    source: str,
    entries: list[tuple[TunnelSource, list[IngressRule]]],
    generated_at: str | None = None,
) -> IngressOverview:
    """Flatten per-tunnel rules into rows sorted by hostname (catch-all rules last).

    A hostname + path served by more than one tunnel is flagged: Cloudflare
    sends traffic to the tunnel its DNS record points at, so the other rules
    are likely stale.
    """

    rows: list[OverviewRow] = []
    tunnels: list[TunnelSource] = []
    for tunnel, rules in entries:
        tunnel.rule_count = len(rules)
        tunnels.append(tunnel)
        for position, rule in enumerate(rules, start=1):
            rows.append(
                OverviewRow(
                    tunnel_id=tunnel.tunnel_id,
                    tunnel_name=tunnel.tunnel_name,
                    position=position,
                    rule=rule,
                    backup_id=tunnel.backup_id,
                    snapshot_at=tunnel.snapshot_at,
                )
            )

    owners: dict[tuple[str, str | None], dict[str, str]] = {}
    for row in rows:
        if row.rule.hostname:
            key = (row.rule.hostname.lower(), row.rule.path)
            owners.setdefault(key, {})[row.tunnel_id] = row.tunnel_name
    for row in rows:
        if row.rule.hostname:
            tunnel_owners = owners[(row.rule.hostname.lower(), row.rule.path)]
            row.shared_with = sorted(name for tid, name in tunnel_owners.items() if tid != row.tunnel_id)

    rows.sort(
        key=lambda row: (
            row.rule.hostname is None,
            (row.rule.hostname or "").lower(),
            row.rule.path or "",
            row.tunnel_name.lower(),
            row.position,
        )
    )
    return IngressOverview(source=source, rows=rows, tunnels=tunnels, generated_at=generated_at)


def snapshot_overview() -> IngressOverview:
    entries: list[tuple[TunnelSource, list[IngressRule]]] = []
    for backup in get_latest_backups_per_tunnel():
        tunnel = TunnelSource(
            tunnel_id=backup.tunnel_id,
            tunnel_name=backup.tunnel_name or "Unknown tunnel",
            backup_id=backup.id,
            snapshot_at=backup.created_at,
        )
        rules: list[IngressRule] = []
        file_path = BACKUP_DIR / backup.filename
        try:
            with file_path.open("r", encoding="utf-8") as handle:
                rules = extract_ingress(json.load(handle))
        except FileNotFoundError:
            tunnel.error = "Snapshot file is missing from the backup directory."
        except (OSError, json.JSONDecodeError):
            logger.exception("Unable to read snapshot %s for the ingress overview.", file_path)
            tunnel.error = "Snapshot file could not be read."
        entries.append((tunnel, rules))
    return build_overview("snapshot", entries)


async def _live_entry(
    account_id: str, api_token: str, tunnel: dict, semaphore: asyncio.Semaphore
) -> tuple[TunnelSource, list[IngressRule]]:
    tunnel_id = str(tunnel.get("id") or "").strip()
    source = TunnelSource(tunnel_id=tunnel_id, tunnel_name=str(tunnel.get("name") or "Unknown tunnel"))
    async with semaphore:
        try:
            data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token)
        except HTTPException as exc:
            source.error = str(exc.detail)
            return source, []
        except Exception:
            logger.exception("Unexpected error loading the configuration of tunnel %s.", tunnel_id)
            source.error = "Unexpected error while loading the configuration."
            return source, []
    rules = extract_ingress(data.get("result") or {})
    if not rules:
        source.note = "No remotely managed ingress rules (the tunnel may use a local config file)."
    return source, rules


async def live_overview(account_id: str, api_token: str) -> IngressOverview:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        tunnels = await list_tunnels(account_id, api_token)
    except HTTPException as exc:
        return IngressOverview(source="live", generated_at=generated_at, error=f"Could not load tunnels: {exc.detail}")

    active = [t for t in tunnels if t.get("id") and not t.get("deleted_at")]
    active.sort(key=lambda t: str(t.get("name") or "").lower())
    semaphore = asyncio.Semaphore(LIVE_FETCH_CONCURRENCY)
    entries = await asyncio.gather(*(_live_entry(account_id, api_token, t, semaphore) for t in active))
    return build_overview("live", list(entries), generated_at)
