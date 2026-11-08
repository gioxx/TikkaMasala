"""Test bootstrap: point the app at a throwaway data dir before it is imported.

Configuration is read from the environment at import time, so this must run
before any ``app.*`` module is loaded.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

_DATA_DIR = Path(tempfile.mkdtemp(prefix="tikkamasala-tests-"))
os.environ["DATA_DIR"] = str(_DATA_DIR)
for _name in (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "TOKEN_ENCRYPTION_KEY",
    "AUTO_BACKUP_TIMEZONE",
    "BACKUP_RETENTION_DAYS",
    "NOTIFICATION_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DEMO",
    "CF_ACCESS_TEAM_DOMAIN",
    "CF_ACCESS_AUD",
):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _setup_gate(request, monkeypatch):
    """Keep the first-run setup redirect off unless the test is marked ``setup_gate``."""
    if request.node.get_closest_marker("setup_gate"):
        return
    import app.setup

    monkeypatch.setattr(app.setup, "setup_required", lambda: False)


@pytest.fixture(autouse=True)
def _fake_tunnel_list(monkeypatch):
    """Keep the Tunnels home offline: an empty tunnel list and a clean cache for every test.

    Tests that need tunnels patch ``app.tunnels.list_tunnels`` again.
    """
    import app.tunnels

    async def _no_tunnels(account_id: str, api_token: str) -> list[dict]:
        return []

    app.tunnels.clear_tunnel_list_cache()
    monkeypatch.setattr(app.tunnels, "list_tunnels", _no_tunnels)
    yield
    app.tunnels.clear_tunnel_list_cache()


@pytest.fixture(autouse=True)
def _fresh_run_locks():
    """Module-level asyncio locks and tasks belong to one event loop: never let them leak between tests."""
    import app.background
    import app.scheduler

    app.scheduler._profile_run_locks.clear()
    app.background._background_tasks.clear()
    yield
    app.scheduler._profile_run_locks.clear()
    app.background._background_tasks.clear()


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _DATA_DIR


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    # No context manager: lifespan (scheduler start) is not needed for these tests.
    return TestClient(app)


@pytest.fixture
def make_backup(data_dir: Path):
    """Insert a backup row plus its JSON file, mimicking ``create_backup``."""

    created: list[int] = []

    def _make(
        tunnel_id: str = "tunnel-1",
        tunnel_name: str = "Home",
        ingress: list[dict] | None = None,
        created_at: str = "2026-01-01T00:00:00+00:00",
        account_id: str = "acc-1",
        notes: str = "",
        origin: str = "manual",
    ) -> int:
        ingress = ingress if ingress is not None else [
            {"hostname": "app.example.com", "service": "http://10.0.0.5:8080"},
            {"service": "http_status:404"},
        ]
        filename = f"test_{tunnel_id}_{len(created)}_{created_at[:10]}.json"
        payload = {
            "exported_at": created_at,
            "account_id": account_id,
            "tunnel_id": tunnel_id,
            "tunnel": {"id": tunnel_id, "name": tunnel_name},
            "configuration": {"config": {"ingress": ingress}},
            "notes": notes,
        }
        (data_dir / "backups" / filename).write_text(json.dumps(payload), encoding="utf-8")
        with closing(sqlite3.connect(data_dir / "app.db")) as conn:
            cursor = conn.execute(
                """
                INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, account_id, tunnel_id, tunnel_name, len(ingress), filename, notes, origin),
            )
            conn.commit()
            backup_id = int(cursor.lastrowid)
        created.append(backup_id)
        return backup_id

    yield _make

    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        for backup_id in created:
            conn.execute("DELETE FROM restores WHERE backup_id = ?", (backup_id,))
            conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
        conn.commit()


@pytest.fixture
def configured_default():
    """Save working credentials on the Default profile; clear them afterwards."""
    from app.credentials import ApiCredentials, remember_api_token
    from app.profiles import DEFAULT_PROFILE_ID, update_profile

    creds = ApiCredentials(account_id="0123456789abcdef0123456789abcdef", api_token="test-token-0000-wxyz")
    update_profile(DEFAULT_PROFILE_ID, account_id=creds.account_id)
    remember_api_token(DEFAULT_PROFILE_ID, creds.api_token)
    yield creds
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)


SECOND_PROFILE_ACCOUNT_ID = "fedcba9876543210fedcba9876543210"
SECOND_PROFILE_API_TOKEN = "client-token-0000-abcd"


def use_profile(client, profile_id) -> None:
    """Make ``client`` work on ``profile_id`` from its next request (sets the ``tm_profile`` cookie)."""
    client.cookies.set("tm_profile", str(profile_id))


@pytest.fixture(autouse=True)
def _clear_client_cookies(request):
    """The session-scoped client keeps cookies: never let ``tm_profile`` leak into later tests."""
    yield
    if "client" in request.fixturenames:
        request.getfixturevalue("client").cookies.clear()


@pytest.fixture
def second_profile(data_dir: Path):
    """A second profile, "Client", with its own account and token; everything not Default is removed afterwards."""
    from app.credentials import remember_api_token
    from app.profiles import create_profile, get_profile, update_profile
    from app.scheduler import AUTO_BACKUP_JOB_ID_PREFIX, auto_backup_job_id, auto_backup_scheduler

    profile = create_profile("Client")
    update_profile(profile.id, account_id=SECOND_PROFILE_ACCOUNT_ID)
    remember_api_token(profile.id, SECOND_PROFILE_API_TOKEN)
    yield get_profile(profile.id)

    default_job_id = auto_backup_job_id(1)
    for job in auto_backup_scheduler.get_jobs():
        if job.id.startswith(AUTO_BACKUP_JOB_ID_PREFIX) and job.id != default_job_id:
            auto_backup_scheduler.remove_job(job.id)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM notification_destinations WHERE profile_id != 1")
        conn.execute("DELETE FROM scheduled_runs WHERE profile_id != 1")
        conn.execute("DELETE FROM profiles WHERE id != 1")
        conn.commit()
