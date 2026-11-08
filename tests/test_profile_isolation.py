"""Two profiles side by side: each page and run works on the active profile only."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest

import app.scheduler as scheduler
import app.tunnels as tunnels
from app.profiles import DEFAULT_PROFILE_ID, get_profile, update_profile
from app.scheduler import (
    auto_backup_job_id,
    auto_backup_scheduler,
    configure_all_auto_backup_jobs,
    create_scheduled_run,
    get_all_scheduled_runs,
    run_auto_backup_job,
)
from tests.conftest import use_profile

GOOD = "0123456789abcdef0123456789abcdef"
CLIENT = "fedcba9876543210fedcba9876543210"
CLIENT_TOKEN = "client-token-0000-abcd"
OTHER = "acc-other"
TOKEN = "test-token-0000-wxyz"


class _ListRecorder:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, *args):
        self.calls.append(args)
        return []


@pytest.fixture
def list_recorder(monkeypatch):
    recorder = _ListRecorder()
    monkeypatch.setattr(tunnels, "list_tunnels", recorder)
    return recorder


def test_tunnels_home_lists_active_profile_only(client, configured_default, second_profile, list_recorder):
    use_profile(client, second_profile.id)
    assert client.get("/").status_code == 200
    assert list_recorder.calls == [(CLIENT, CLIENT_TOKEN)]

    tunnels.clear_tunnel_list_cache()
    list_recorder.calls.clear()
    use_profile(client, DEFAULT_PROFILE_ID)
    assert client.get("/").status_code == 200
    assert list_recorder.calls == [(GOOD, TOKEN)]


def test_hostnames_tile_counts_active_account_only(client, configured_default, second_profile, make_backup):
    make_backup("t-a", "Home", account_id=GOOD, ingress=[{"hostname": "a.example.com", "service": "http://localhost:1"}])
    make_backup("t-z", "Far", account_id=OTHER, ingress=[{"hostname": "z.example.com", "service": "http://localhost:2"}])
    home = asyncio.run(tunnels.build_tunnels_home(DEFAULT_PROFILE_ID))
    tile = home.tile("hostnames")
    assert (tile.value, tile.detail) == ("1", "1 host-local origin in this account")


def test_hostnames_tile_client_direction(client, configured_default, second_profile, make_backup):
    make_backup("t-a", "alphadefault", account_id=GOOD, ingress=[{"hostname": "a.example.com", "service": "http://localhost:1"}])
    make_backup(
        "t-c",
        "bravoclient",
        account_id=CLIENT,
        ingress=[
            {"hostname": "c1.example.com", "service": "http://localhost:3"},
            {"hostname": "c2.example.com", "service": "http://localhost:4"},
        ],
    )
    tunnels.clear_tunnel_list_cache()
    tile = asyncio.run(tunnels.build_tunnels_home(second_profile.id)).tile("hostnames")
    assert (tile.value, tile.detail) == ("2", "2 host-local origins in this account")


def test_ingress_snapshot_follows_active_profile(client, configured_default, second_profile, make_backup):
    make_backup("t-home", "Home", account_id=GOOD, ingress=[{"hostname": "home.example.com", "service": "http://10.0.0.1"}])
    make_backup(
        "t-client", "Site", account_id=CLIENT, ingress=[{"hostname": "client.example.com", "service": "http://10.0.0.2"}]
    )
    default_page = client.get("/ingress").text
    assert "home.example.com" in default_page and "client.example.com" not in default_page
    assert "Hostnames of the Default profile&#39;s account." in default_page or (
        "Hostnames of the Default profile's account." in default_page
    )

    use_profile(client, second_profile.id)
    client_page = client.get("/ingress").text
    assert "client.example.com" in client_page and "home.example.com" not in client_page
    assert "Hostnames of the Client profile" in client_page


def test_backups_archive_per_profile(client, configured_default, second_profile, make_backup):
    make_backup("t-home", "alphadefault", account_id=GOOD)
    make_backup("t-client", "bravoclient", account_id=CLIENT)
    make_backup("t-old", "charlieold", account_id=OTHER)
    default_page = client.get("/backups").text
    assert "Default (1)" in default_page
    assert "alphadefault" in default_page and "bravoclient" not in default_page
    use_profile(client, second_profile.id)
    page = client.get("/backups").text
    assert "Client (1)" in page
    assert "bravoclient" in page and "alphadefault" not in page
    other = client.get("/backups?scope=other").text
    assert "charlieold" in other and "alphadefault" not in other and "bravoclient" not in other


def test_schedule_and_retention_write_active_profile(client, configured_default, second_profile, monkeypatch):
    monkeypatch.setattr("app.routers.automation.configure_auto_backup_job", lambda profile_id: None)
    default_before = get_profile(DEFAULT_PROFILE_ID)
    use_profile(client, second_profile.id)
    response = client.post("/automation/schedule", data={"cron_expression": "0 5 * * *"})
    assert response.status_code == 200
    assert get_profile(second_profile.id).auto_backup_cron == "0 5 * * *"
    assert get_profile(DEFAULT_PROFILE_ID).auto_backup_cron == default_before.auto_backup_cron

    assert client.post("/automation/retention", data={"retention_days": "30"}).status_code == 200
    assert get_profile(second_profile.id).retention_days == 30
    assert get_profile(DEFAULT_PROFILE_ID).retention_days == default_before.retention_days


def test_schedule_and_retention_as_default_leave_client_unchanged(
    client, configured_default, second_profile, monkeypatch
):
    monkeypatch.setattr("app.routers.automation.configure_auto_backup_job", lambda profile_id: None)
    before = get_profile(second_profile.id)
    assert client.post("/automation/schedule", data={"cron_expression": "0 6 * * *"}).status_code == 200
    assert client.post("/automation/retention", data={"retention_days": "12"}).status_code == 200
    assert get_profile(DEFAULT_PROFILE_ID).auto_backup_cron == "0 6 * * *"
    assert get_profile(DEFAULT_PROFILE_ID).retention_days == 12
    after = get_profile(second_profile.id)
    assert (after.auto_backup_cron, after.retention_days) == (before.auto_backup_cron, before.retention_days)
    update_profile(DEFAULT_PROFILE_ID, retention_days=None)


def test_ingress_live_uses_active_profile_credentials(client, configured_default, second_profile, monkeypatch):
    import app.routers.ingress as ingress_router
    from app.ingress_overview import IngressOverview

    calls: list[tuple] = []

    async def fake_live(account_id, api_token):
        calls.append((account_id, api_token))
        return IngressOverview(source="live")

    monkeypatch.setattr(ingress_router, "live_overview", fake_live)
    use_profile(client, second_profile.id)
    assert client.get("/ingress?source=live").status_code == 200
    assert calls == [(CLIENT, CLIENT_TOKEN)]


def test_ingress_snapshot_without_account_shows_nothing(client, second_profile, make_backup):
    make_backup("t-home", "alphadefault", account_id=GOOD, ingress=[{"hostname": "home.example.com", "service": "http://10.0.0.1"}])
    make_backup("t-old", "charlieold", account_id=OTHER, ingress=[{"hostname": "old.example.com", "service": "http://10.0.0.3"}])
    update_profile(second_profile.id, account_id=None)
    use_profile(client, second_profile.id)
    page = client.get("/ingress").text
    assert "home.example.com" not in page and "old.example.com" not in page


def test_run_history_per_profile(client, second_profile, data_dir):
    default_run = create_scheduled_run(DEFAULT_PROFILE_ID, GOOD, "success")
    client_run = create_scheduled_run(second_profile.id, CLIENT, "success")
    try:
        use_profile(client, second_profile.id)
        page = client.get("/automation/runs").text
        assert f"#{client_run}<" in page
        assert f"#{default_run}<" not in page
    finally:
        with closing(sqlite3.connect(data_dir / "app.db")) as conn:
            conn.execute("DELETE FROM scheduled_runs WHERE id IN (?, ?)", (default_run, client_run))
            conn.commit()


def test_back_up_all_uses_active_profile(client, configured_default, second_profile, monkeypatch):
    account_ids: set[str] = set()

    async def fake_list(account_id, api_token):
        account_ids.add(account_id)
        return [{"id": "11111111-1111-4111-8111-111111111111", "name": "Home", "status": "healthy"}]

    created: list[str] = []

    async def fake_back_up(creds, tunnel_id, *, profile_id):
        created.append(creds.account_id)
        from types import SimpleNamespace

        return SimpleNamespace(id=1, tunnel_name="Home", route_count=1, filename="x.json")

    monkeypatch.setattr(tunnels, "list_tunnels", fake_list)
    monkeypatch.setattr(tunnels, "back_up_tunnel", fake_back_up)
    use_profile(client, second_profile.id)
    assert client.post("/tunnels/backup-all", headers={"HX-Request": "true"}).status_code == 200
    assert account_ids == {CLIENT}
    assert created == [CLIENT]


def test_every_enabled_profile_scheduled_and_isolated(second_profile, configured_default, data_dir, monkeypatch):
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        max_run_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM scheduled_runs").fetchone()[0]

    async def fake_list(account_id, api_token):
        if account_id == CLIENT:
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(scheduler, "list_tunnels", fake_list)
    monkeypatch.setattr(scheduler, "queue_notification", lambda *args, **kwargs: None)
    update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=True, auto_backup_cron="0 3 * * *")
    update_profile(second_profile.id, auto_backup_enabled=True, auto_backup_cron="0 4 * * *")
    try:
        configure_all_auto_backup_jobs()
        jobs = {
            job.kwargs["profile_id"]
            for job in auto_backup_scheduler.get_jobs()
            if job.id in (auto_backup_job_id(DEFAULT_PROFILE_ID), auto_backup_job_id(second_profile.id))
        }
        assert jobs == {DEFAULT_PROFILE_ID, second_profile.id}

        client_result = asyncio.run(run_auto_backup_job(second_profile.id))
        default_result = asyncio.run(run_auto_backup_job(DEFAULT_PROFILE_ID))
        assert client_result["status"] == "failed"
        assert default_result["status"] == "success"
        assert get_all_scheduled_runs(second_profile.id)[0].status == "failed"
        assert get_all_scheduled_runs(DEFAULT_PROFILE_ID)[0].status == "success"
    finally:
        update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=False, auto_backup_cron=None, last_run_at=None, last_status=None)
        if auto_backup_scheduler.get_job(auto_backup_job_id(DEFAULT_PROFILE_ID)) is not None:
            auto_backup_scheduler.remove_job(auto_backup_job_id(DEFAULT_PROFILE_ID))
        with closing(sqlite3.connect(data_dir / "app.db")) as conn:
            conn.execute("DELETE FROM scheduled_runs WHERE id > ?", (max_run_id,))
            conn.commit()
