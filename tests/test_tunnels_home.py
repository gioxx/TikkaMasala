"""Tunnels home data: tiles and rows from local data plus one cached tunnel list call."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

import app.credentials as credentials
import app.tunnels as tunnels
from app.backups import count_backups_for_account
from app.credentials import DEMO_NO_CREDENTIALS_MESSAGE, MISSING_CREDENTIALS_MESSAGE, ApiCredentials
from app.ingress_overview import IngressOverview, TunnelSource, snapshot_overview
from app.profiles import update_profile
from app.tunnels import (
    TUNNEL_ID_PATTERN,
    build_tunnels_home,
    clear_tunnel_list_cache,
    fetch_tunnel_list,
    format_when,
)

GOOD = "0123456789abcdef0123456789abcdef"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
T3 = "33333333-3333-4333-8333-333333333333"
T4 = "44444444-4444-4444-8444-444444444444"

TUNNEL_LIST = [
    {"id": T1, "name": "Home", "status": "healthy"},
    {"id": T2, "name": "Lab", "status": "degraded"},
    {"id": T3, "name": "Office", "status": "down"},
    {"id": T4, "name": "Old", "status": "inactive"},
]


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")


class _Recorder:
    def __init__(self, result=None, error: BaseException | None = None, delay: float = 0.0):
        self.calls: list[tuple] = []
        self.result = result if result is not None else []
        self.error = error
        self.delay = delay

    async def __call__(self, *args):
        self.calls.append(args)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_list(monkeypatch):
    def _install(result=None, error: BaseException | None = None, delay: float = 0.0) -> _Recorder:
        recorder = _Recorder(result, error, delay)
        monkeypatch.setattr(tunnels, "list_tunnels", recorder)
        return recorder

    return _install


@pytest.fixture
def seeded(make_backup):
    make_backup(
        T1,
        "Home",
        ingress=[
            {"hostname": "app.example.com", "service": "http://10.0.0.5:8080"},
            {"hostname": "local.example.com", "service": "http://localhost:3000"},
            {"service": "http_status:404"},
        ],
        created_at="2026-01-01T00:00:00+00:00",
        account_id=GOOD,
    )
    make_backup(
        T2,
        "Lab",
        ingress=[{"hostname": "lab.example.com", "service": "http://10.0.0.6"}, {"service": "http_status:404"}],
        account_id=GOOD.upper(),
    )
    make_backup("t-other", "Other", account_id="acc-other")


def _build(**kwargs):
    return asyncio.run(build_tunnels_home(1, **kwargs))


def test_tiles_from_local_data_and_tunnel_list(configured_default, seeded, fake_list):
    fake_list(TUNNEL_LIST)
    home = _build()
    assert home.account_id == GOOD
    assert home.list_error is None
    assert home.credentials_message is None

    tile = home.tile("tunnels")
    assert (tile.value, tile.detail, tile.tone, tile.href) == ("4", "1 degraded, 1 down", "warn", None)
    tile = home.tile("hostnames")
    assert (tile.value, tile.detail, tile.tone, tile.href) == ("3", "1 host-local origin", "warn", "/ingress")
    tile = home.tile("backups")
    assert (tile.value, tile.detail, tile.href) == ("2", "Kept until deleted", "/backups")
    tile = home.tile("auto_backup")
    assert (tile.value, tile.href) == ("Disabled", "/automation")
    assert [t.key for t in home.tiles] == ["tunnels", "hostnames", "auto_backup", "backups"]


def test_rows_merge_status_and_local_columns(configured_default, seeded, fake_list):
    fake_list(TUNNEL_LIST)
    home = _build()
    assert [row.name for row in home.rows] == ["Home", "Lab", "Office", "Old"]
    home_row = home.rows[0]
    assert (home_row.tunnel_id, home_row.status, home_row.hostname_count, home_row.last_backup_at) == (
        T1,
        "healthy",
        2,
        "2026-01-01T00:00:00+00:00",
    )
    assert home_row.last_backup_id is not None
    assert home.rows[1].hostname_count == 1
    office = home.rows[2]
    assert (office.status, office.hostname_count, office.last_backup_at, office.last_backup_id) == ("down", None, None, None)


def test_unknown_status_value(configured_default, fake_list):
    fake_list([{"id": T1, "name": "b", "status": "weird"}, {"id": T2, "name": "a"}, {"name": "no id"}])
    home = _build()
    assert [(row.name, row.status) for row in home.rows] == [("a", "unknown"), ("b", "unknown")]
    assert home.tile("tunnels").value == "2"


def test_unknown_status_is_not_healthy(configured_default, fake_list):
    fake_list([{"id": T1, "name": "a", "status": "healthy"}, {"id": T2, "name": "b"}])
    tile = _build().tile("tunnels")
    assert (tile.value, tile.detail, tile.tone) == ("2", "1 status unknown", "warn")


def test_unknown_status_listed_with_unhealthy(configured_default, fake_list):
    fake_list(
        [
            {"id": T1, "name": "a", "status": "down"},
            {"id": T2, "name": "b", "status": "brand-new"},
            {"id": T3, "name": "c", "status": "degraded"},
            {"id": T4, "name": "d", "status": None},
        ]
    )
    tile = _build().tile("tunnels")
    assert (tile.value, tile.detail, tile.tone) == ("4", "1 degraded, 1 down, 2 status unknown", "warn")


def test_hostnames_tile_warns_on_unreadable_snapshots():
    overview = IngressOverview(
        source="snapshot",
        tunnels=[
            TunnelSource(tunnel_id=T1, tunnel_name="a"),
            TunnelSource(tunnel_id=T2, tunnel_name="b", error="Snapshot file is missing."),
        ],
    )
    tile = tunnels._hostnames_tile(overview)
    assert (tile.value, tile.detail, tile.tone) == ("0", "1 snapshot unreadable", "warn")
    overview.tunnels.append(TunnelSource(tunnel_id=T3, tunnel_name="c", error="Snapshot file is corrupt."))
    tile = tunnels._hostnames_tile(overview)
    assert (tile.detail, tile.tone) == ("2 snapshots unreadable", "warn")


def test_hostnames_tile_unreadable_snapshot_end_to_end(configured_default, seeded, fake_list, monkeypatch):
    fake_list(TUNNEL_LIST)
    real = snapshot_overview

    def broken(account_id):
        overview = real(account_id)
        overview.tunnels.append(TunnelSource(tunnel_id=T3, tunnel_name="Office", error="Snapshot file is corrupt."))
        return overview

    monkeypatch.setattr(tunnels, "snapshot_overview", broken)
    tile = _build().tile("hostnames")
    assert (tile.value, tile.detail, tile.tone) == ("3", "1 host-local origin, 1 snapshot unreadable", "warn")


def test_all_healthy_with_inactive(configured_default, fake_list):
    fake_list([{"id": T1, "name": "a", "status": "HEALTHY"}, {"id": T4, "name": "b", "status": "inactive"}])
    tile = _build().tile("tunnels")
    assert (tile.value, tile.detail, tile.tone) == ("2", "All healthy, 1 inactive", "ok")


def test_zero_tunnels(configured_default, fake_list):
    fake_list([])
    tile = _build().tile("tunnels")
    assert (tile.value, tile.detail, tile.tone) == ("0", "No tunnels in this account", "muted")


def test_list_failure_keeps_local_data(configured_default, seeded, fake_list):
    fake_list(error=HTTPException(status_code=502, detail="boom"))
    home = _build()
    assert "boom" in home.list_error
    tile = home.tile("tunnels")
    assert (tile.value, tile.detail, tile.tone) == ("—", "Status unavailable", "error")
    assert home.tile("hostnames").value == "3"
    assert home.tile("backups").value == "2"
    assert [(row.name, row.status) for row in home.rows] == [("Home", "unknown"), ("Lab", "unknown")]
    assert home.rows[0].hostname_count == 2


def test_list_timeout_keeps_local_data(monkeypatch, configured_default, seeded, fake_list):
    monkeypatch.setattr(tunnels, "HOME_TUNNEL_LIST_TIMEOUT_SECONDS", 0.01)
    fake_list(TUNNEL_LIST, delay=1)
    home = _build()
    assert home.list_error == "Cloudflare did not answer within 0.01 seconds."
    assert [row.name for row in home.rows] == ["Home", "Lab"]


def test_unexpected_list_error_is_contained(caplog, configured_default, fake_list):
    fake_list(error=ValueError("x"))
    with caplog.at_level(logging.ERROR):
        home = _build()
    assert home.list_error == "Unexpected error while listing tunnels."
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert configured_default.api_token not in caplog.text


def test_cache_reuses_list_within_ttl(configured_default, fake_list):
    recorder = fake_list(TUNNEL_LIST)
    _build()
    _build()
    assert len(recorder.calls) == 1
    _build(fresh=True)
    assert len(recorder.calls) == 2
    clear_tunnel_list_cache()
    _build()
    assert len(recorder.calls) == 3


def test_cache_keyed_on_token(fake_list):
    recorder = fake_list(TUNNEL_LIST)
    asyncio.run(fetch_tunnel_list(ApiCredentials(account_id=GOOD, api_token="token-A")))
    asyncio.run(fetch_tunnel_list(ApiCredentials(account_id=GOOD, api_token="token-B")))
    assert len(recorder.calls) == 2


def test_cache_does_not_store_failures(monkeypatch, fake_list):
    creds = ApiCredentials(account_id=GOOD, api_token="token-A")
    recorder = fake_list(error=HTTPException(status_code=502, detail="boom"))
    with pytest.raises(HTTPException):
        asyncio.run(fetch_tunnel_list(creds))
    recorder.error = None
    recorder.result = TUNNEL_LIST
    assert asyncio.run(fetch_tunnel_list(creds)) == TUNNEL_LIST
    assert len(recorder.calls) == 2


def test_no_credentials(fake_list):
    recorder = fake_list(TUNNEL_LIST)
    home = _build()
    assert home.credentials_message == MISSING_CREDENTIALS_MESSAGE
    assert recorder.calls == []
    tile = home.tile("tunnels")
    assert (tile.value, tile.detail, tile.tone) == ("—", "No credentials", "muted")
    assert home.rows == []
    assert home.tile("backups").value == "0"


def test_demo_no_credentials(monkeypatch, fake_list):
    monkeypatch.setattr(tunnels, "DEMO_MODE", True)
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    recorder = fake_list(TUNNEL_LIST)
    home = _build()
    assert home.credentials_message == DEMO_NO_CREDENTIALS_MESSAGE
    assert recorder.calls == []
    tile = home.tile("auto_backup")
    assert (tile.value, tile.detail, tile.tone) == ("Disabled", "Not available in demo mode", "muted")


@pytest.mark.parametrize(
    "enabled,ready,last_status,value,tone",
    [
        (False, True, None, "Disabled", "muted"),
        (True, False, None, "Failing", "error"),
        (True, True, "failed", "Failing", "error"),
        (True, True, "partial", "Failing", "error"),
        (True, True, None, "Waiting", "muted"),
        (True, True, "success", "OK", "ok"),
        (True, True, "skipped", "OK", "ok"),
    ],
)
def test_auto_backup_tile(monkeypatch, configured_default, fake_list, enabled, ready, last_status, value, tone):
    fake_list([])
    status = {
        "enabled": enabled,
        "prereqs": {"ready": ready},
        "last_status": last_status,
        "last_run_at": "2020-01-01T03:00:00+00:00" if last_status else None,
        "next_run_at": None,
    }
    monkeypatch.setattr(tunnels, "build_auto_backup_status", lambda profile_id: status)
    tile = _build().tile("auto_backup")
    assert (tile.value, tile.tone) == (value, tone)
    if value == "Disabled":
        assert tile.detail == "Turn it on in Automation"
    elif value == "Failing" and not ready:
        assert tile.detail == "Missing credentials"
    elif value == "Failing":
        assert tile.detail == f"Last run {last_status} 2020-01-01"
    elif value == "Waiting":
        assert tile.detail == "Last never, next not scheduled"
    else:
        assert tile.detail == "Last 2020-01-01, next not scheduled"


def test_retention_tile(configured_default, fake_list):
    fake_list([])
    update_profile(1, retention_days=30)
    try:
        tile = _build().tile("backups")
    finally:
        update_profile(1, retention_days=None)
    assert tile.detail == "Kept for 30 days"


def test_snapshot_overview_filters_account(seeded):
    names = {tunnel.tunnel_name for tunnel in snapshot_overview(GOOD).tunnels}
    assert names == {"Home", "Lab"}
    assert "Other" in {tunnel.tunnel_name for tunnel in snapshot_overview().tunnels}


def test_count_backups_for_account(seeded):
    assert count_backups_for_account(GOOD) == 2
    assert count_backups_for_account("ACC-OTHER") == 1


def test_format_when():
    now = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    utc = ZoneInfo("UTC")
    assert format_when("2026-03-10T03:00:00+00:00", now=now, tz=utc) == "today 03:00"
    assert format_when("2026-03-09T03:00:00+00:00", now=now, tz=utc) == "yesterday 03:00"
    assert format_when("2026-03-11T03:00:00+00:00", now=now, tz=utc) == "tomorrow 03:00"
    assert format_when("2026-03-07T03:00:00+00:00", now=now, tz=utc) == "3 days ago"
    assert format_when("2026-01-01T00:00:00+00:00", now=now, tz=utc) == "2026-01-01"
    assert format_when(None, now=now, tz=utc) == "never"
    assert format_when("2026-03-09T23:30:00+00:00", now=now, tz=ZoneInfo("Europe/Rome")) == "today 00:30"


def test_tunnel_id_pattern():
    assert TUNNEL_ID_PATTERN.match(T1)
    for bad in ("t1", "../x", T1 + "x"):
        assert not TUNNEL_ID_PATTERN.match(bad)
