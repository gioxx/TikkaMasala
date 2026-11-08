"""Old URLs answer 308 (method and body kept) to their new home, with the query string."""

from __future__ import annotations

import pytest

from app.config import DEFAULT_AUTO_BACKUP_CRON
from app.profiles import DEFAULT_PROFILE_ID
from app.scheduler import get_auto_backup_cron, set_auto_backup_cron, set_auto_backup_enabled

LEGACY = [
    ("/auto-backup/settings", "/automation/schedule"),
    ("/auto-backup/run", "/automation/run"),
    ("/notifications/test", "/automation/notifications/test"),
    ("/auto-backup/tunnel-filters", "/automation/tunnels"),
    ("/auto-backup/runs", "/automation/runs"),
    ("/notifications/messages", "/settings/notifications"),
    ("/notifications/messages/reset", "/settings/notifications/reset"),
]


@pytest.mark.parametrize("old,new", LEGACY)
def test_legacy_get_and_post_redirect(client, old, new):
    for method in ("GET", "POST"):
        response = client.request(method, old, follow_redirects=False)
        assert response.status_code == 308, method
        assert response.headers["location"] == new, method
        response = client.request(method, old + "?x=1", follow_redirects=False)
        assert response.status_code == 308, method
        assert response.headers["location"] == new + "?x=1", method


def test_legacy_runs_keeps_query(client):
    response = client.get("/auto-backup/runs?page=2&page_size=all", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/automation/runs?page=2&page_size=all"


def test_legacy_post_follows_to_schedule(client, configured_default, monkeypatch):
    import app.routers.automation as automation

    monkeypatch.setattr(automation, "configure_auto_backup_job", lambda profile_id: None)
    monkeypatch.setattr(automation, "configure_all_auto_backup_jobs", lambda: None)
    try:
        response = client.post("/auto-backup/settings", data={"cron_expression": "0 5 * * *"})
        assert response.status_code == 200
        assert get_auto_backup_cron(DEFAULT_PROFILE_ID) == "0 5 * * *"
    finally:
        set_auto_backup_cron(DEFAULT_PROFILE_ID, DEFAULT_AUTO_BACKUP_CRON)
        set_auto_backup_enabled(DEFAULT_PROFILE_ID, False)


def test_old_routes_not_registered():
    from app.main import app
    from app.routers import legacy

    legacy_endpoints = {route.endpoint for route in legacy.router.routes}
    old_paths = {old for old, _new in LEGACY}
    offenders = [
        route.path
        for route in app.routes
        if getattr(route, "path", None) in old_paths and getattr(route, "endpoint", None) not in legacy_endpoints
    ]
    assert offenders == []
