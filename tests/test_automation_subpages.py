"""Automation sub-pages: per-tunnel schedule and run history."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.db import db
from app.profiles import DEFAULT_PROFILE_ID
from app.scheduler import complete_scheduled_run, create_scheduled_run, get_auto_backup_tunnel_schedule, set_auto_backup_tunnel_schedule

GOOD = "0123456789abcdef0123456789abcdef"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def fake_list(monkeypatch):
    import app.tunnels

    state = {"tunnels": [{"id": T1, "name": "Home"}, {"id": T2, "name": "Lab"}], "error": None}

    async def _list(account_id, api_token):
        if state["error"]:
            raise state["error"]
        return state["tunnels"]

    app.tunnels.clear_tunnel_list_cache()
    monkeypatch.setattr(app.tunnels, "list_tunnels", _list)
    return state


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_auto_backup_tunnel_schedule(DEFAULT_PROFILE_ID, {"mode": "all", "tunnels": {}})
    conn = db()
    try:
        conn.execute("DELETE FROM scheduled_runs WHERE profile_id = 1")
        conn.commit()
    finally:
        conn.close()


def test_tunnels_page_lists_and_saves(client, configured_default, fake_list):
    text = client.get("/automation/tunnels").text
    assert 'action="/automation/tunnels"' in text
    assert "Home" in text and "Lab" in text
    assert 'href="/automation/tunnels" aria-current="page"' in text


def test_tunnels_page_without_credentials(client):
    text = client.get("/automation/tunnels").text
    assert 'href="/settings"' in text
    assert 'action="/automation/tunnels"' not in text


def test_tunnels_page_list_failure(client, configured_default, fake_list):
    fake_list["error"] = HTTPException(502, "boom")
    response = client.get("/automation/tunnels")
    assert response.status_code == 200
    assert "Could not load tunnels: boom" in response.text


def test_tunnels_page_unexpected_failure(client, configured_default, fake_list):
    fake_list["error"] = RuntimeError("x")
    response = client.get("/automation/tunnels")
    assert response.status_code == 200
    assert "Unexpected error loading tunnels from Cloudflare API." in response.text


def test_tunnels_save_redirects_with_notice(client, configured_default, fake_list):
    response = client.post(
        "/automation/tunnels",
        data={"mode": "selected", "tunnel_ids": T1, f"include_{T1}": "1", f"freq_{T1}": "weekly", f"name_{T1}": "Home"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/automation/tunnels?saved=1"
    assert get_auto_backup_tunnel_schedule(DEFAULT_PROFILE_ID) == {
        "mode": "selected",
        "tunnels": {T1: {"name": "Home", "frequency": "weekly"}},
    }
    assert "Advanced schedule saved." in client.get("/automation/tunnels?saved=1").text
    assert "could not be saved" in client.get("/automation/tunnels?error=1").text


def test_tunnels_page_escapes_names(client, configured_default, fake_list):
    fake_list["tunnels"] = [{"id": T1, "name": "<b>x</b>"}]
    text = client.get("/automation/tunnels").text
    assert "&lt;b&gt;x" in text
    assert "<b>x</b>" not in text


def test_runs_page_pagination(client):
    for _ in range(16):
        run_id = create_scheduled_run(1, GOOD, "success")
        complete_scheduled_run(run_id, "success", 1, 1, 0)
    text = client.get("/automation/runs").text
    assert 'href="/automation/runs?page=2&amp;page_size=15' in text
    assert '<a href="/automation/runs" aria-current="page">Run history</a>' in text


def test_automation_overview_links_subpages(client):
    text = client.get("/automation").text
    assert 'href="/automation/tunnels"' in text
    assert 'href="/automation/runs"' in text
    assert "/auto-backup/" not in text


def test_subpages_redirect_in_demo(client, monkeypatch):
    import app.routers.automation as automation

    monkeypatch.setattr(automation, "DEMO_MODE", True)
    for path in ("/automation/tunnels", "/automation/runs"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_demo_post_tunnels_redirects(client, monkeypatch):
    import app.routers.automation as automation

    monkeypatch.setattr(automation, "DEMO_MODE", True)
    response = client.post("/automation/tunnels", data={"mode": "all"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
