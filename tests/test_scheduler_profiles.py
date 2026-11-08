"""The automatic backup schedule runs per profile, isolated from the others."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import app.credentials as credentials
import app.scheduler as scheduler
from app.db import get_setting
from app.profiles import DEFAULT_PROFILE_ID, create_profile, get_profile, update_profile
from app.scheduler import (
    auto_backup_job_id,
    auto_backup_scheduler,
    configure_all_auto_backup_jobs,
    get_all_scheduled_runs,
    get_auto_backup_tunnel_schedule,
    run_auto_backup_job,
    set_auto_backup_tunnel_schedule,
)

_RESET_FIELDS = {
    "account_id": None,
    "api_token": None,
    "auto_backup_enabled": False,
    "auto_backup_cron": None,
    "tunnel_schedule": None,
    "last_run_at": None,
    "last_status": None,
}


def _remove_profile_jobs() -> None:
    for job in auto_backup_scheduler.get_jobs():
        if job.id.startswith("auto-backup-profile-"):
            auto_backup_scheduler.remove_job(job.id)


@pytest.fixture
def recorder(data_dir: Path, monkeypatch):
    """Default (acc-A) and Client (acc-B) profiles, with Cloudflare calls recorded."""
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    monkeypatch.setattr(credentials, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(scheduler, "queue_notification", lambda *args, **kwargs: None)

    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        max_run_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM scheduled_runs").fetchone()[0]

    update_profile(DEFAULT_PROFILE_ID, **_RESET_FIELDS)
    update_profile(DEFAULT_PROFILE_ID, account_id="acc-A", api_token="tok-A")
    client = create_profile("Client")
    update_profile(client.id, account_id="acc-B", api_token="tok-B")

    calls: dict[str, list] = {"list_tunnels": [], "create_backup": []}

    async def fake_list_tunnels(account_id, api_token):
        calls["list_tunnels"].append(account_id)
        return [{"id": f"{account_id}-t1", "name": f"{account_id} one"}]

    async def fake_create_backup(account_id, tunnel_id, api_token, notes=None, origin="manual"):
        calls["create_backup"].append({"account_id": account_id, "tunnel_id": tunnel_id, "origin": origin})

    monkeypatch.setattr(scheduler, "list_tunnels", fake_list_tunnels)
    monkeypatch.setattr(scheduler, "create_backup", fake_create_backup)

    yield {"client_id": client.id, "calls": calls}

    _remove_profile_jobs()
    update_profile(DEFAULT_PROFILE_ID, **_RESET_FIELDS)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM scheduled_runs WHERE id > ?", (max_run_id,))
        conn.execute("DELETE FROM profiles WHERE id != ?", (DEFAULT_PROFILE_ID,))
        conn.commit()


def test_job_id_format():
    assert auto_backup_job_id(7) == "auto-backup-profile-7"


def test_job_ids_are_per_profile(recorder):
    client_id = recorder["client_id"]
    update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=True, auto_backup_cron="0 3 * * *")
    update_profile(client_id, auto_backup_enabled=True, auto_backup_cron="30 4 * * *")
    configure_all_auto_backup_jobs()
    job_ids = {job.id for job in auto_backup_scheduler.get_jobs()}
    assert "auto-backup-profile-1" in job_ids
    assert f"auto-backup-profile-{client_id}" in job_ids


def test_bad_cron_in_one_profile_does_not_block_other(recorder):
    client_id = recorder["client_id"]
    update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=True, auto_backup_cron="0 3 * * *")
    update_profile(client_id, auto_backup_enabled=True, auto_backup_cron="nope")
    configure_all_auto_backup_jobs()
    assert auto_backup_scheduler.get_job(auto_backup_job_id(DEFAULT_PROFILE_ID)) is not None
    assert auto_backup_scheduler.get_job(auto_backup_job_id(client_id)) is None


def test_configure_all_removes_jobs_of_disabled_or_deleted_profiles(recorder, data_dir: Path):
    client_id = recorder["client_id"]
    update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=True, auto_backup_cron="0 3 * * *")
    update_profile(client_id, auto_backup_enabled=True, auto_backup_cron="0 3 * * *")
    configure_all_auto_backup_jobs()
    assert auto_backup_scheduler.get_job(auto_backup_job_id(client_id)) is not None

    update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=False)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (client_id,))
        conn.commit()
    configure_all_auto_backup_jobs()
    assert auto_backup_scheduler.get_job(auto_backup_job_id(DEFAULT_PROFILE_ID)) is None
    assert auto_backup_scheduler.get_job(auto_backup_job_id(client_id)) is None


def test_run_only_touches_own_account(recorder):
    client_id = recorder["client_id"]
    calls = recorder["calls"]
    result = asyncio.run(run_auto_backup_job(client_id, trigger="manual"))
    assert result["status"] == "success"
    assert calls["list_tunnels"] == ["acc-B"]
    assert calls["create_backup"] == [{"account_id": "acc-B", "tunnel_id": "acc-B-t1", "origin": "automatic"}]


def test_profiles_do_not_block_each_other(recorder):
    client_id = recorder["client_id"]

    async def scenario():
        async with scheduler._profile_lock(DEFAULT_PROFILE_ID):
            client_result = await run_auto_backup_job(client_id, trigger="manual")
            default_result = await run_auto_backup_job(DEFAULT_PROFILE_ID, trigger="manual")
        return client_result, default_result

    client_result, default_result = asyncio.run(scenario())
    assert client_result["status"] != "skipped"
    assert default_result["status"] == "skipped"


def test_runs_are_recorded_per_profile(recorder):
    client_id = recorder["client_id"]
    asyncio.run(run_auto_backup_job(DEFAULT_PROFILE_ID, trigger="manual"))
    asyncio.run(run_auto_backup_job(client_id, trigger="manual"))
    default_runs = get_all_scheduled_runs(DEFAULT_PROFILE_ID)
    client_runs = get_all_scheduled_runs(client_id)
    assert [run.account_id for run in default_runs if run.account_id in {"acc-A", "acc-B"}] == ["acc-A"]
    assert [run.account_id for run in client_runs] == ["acc-B"]
    assert all(run.profile_id == client_id for run in client_runs)


def test_last_status_stored_on_profile(recorder):
    client_id = recorder["client_id"]
    asyncio.run(run_auto_backup_job(client_id, trigger="manual"))
    profile = get_profile(client_id)
    assert profile.last_status == "success"
    assert profile.last_run_at
    assert get_profile(DEFAULT_PROFILE_ID).last_status is None
    assert get_setting("auto_backup_last_status") is None
    assert get_setting("auto_backup_last_run_at") is None


def test_tunnel_schedule_isolated(recorder):
    client_id = recorder["client_id"]
    before = get_auto_backup_tunnel_schedule(client_id)
    set_auto_backup_tunnel_schedule(DEFAULT_PROFILE_ID, {"mode": "selected", "tunnels": {"x": {"frequency": "weekly"}}})
    assert get_auto_backup_tunnel_schedule(DEFAULT_PROFILE_ID)["mode"] == "selected"
    assert get_auto_backup_tunnel_schedule(client_id) == before
    assert get_setting("auto_backup_tunnel_schedule") is None
