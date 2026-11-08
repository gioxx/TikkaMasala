"""Interim Backups and Automation pages (rebuilt in step 5)."""

from __future__ import annotations


def test_backups_page_lists_archive(client, make_backup):
    backup_id = make_backup(tunnel_name="Archive Tunnel")
    response = client.get("/backups")
    assert response.status_code == 200
    assert 'id="saved-backups-section"' in response.text
    assert f"/backup/{backup_id}" in response.text
    assert "Archive Tunnel" in response.text


def test_backups_page_pagination_links(client, make_backup):
    for index in range(6):
        make_backup(tunnel_id=f"t-{index}", tunnel_name=f"Tunnel {index}")
    text = client.get("/backups?page_size=5").text
    assert 'href="/backups?page=2&page_size=5' in text
    assert "/?page=" not in text


def test_automation_page_has_panels(client):
    text = client.get("/automation").text
    assert 'id="auto-backup-section"' in text
    assert 'id="notifications-section"' in text
    assert 'action="/auto-backup/settings"' in text
    assert 'action="/auto-backup/run"' in text
    assert 'action="/notifications/test"' in text


def test_auto_backup_settings_renders_automation(client):
    response = client.post("/auto-backup/settings", data={"cron_expression": "not a cron"})
    assert response.status_code == 200
    assert "Invalid cron expression" in response.text
    assert 'id="auto-backup-section"' in response.text
    assert 'id="tunnels-section"' not in response.text


def test_notifications_test_renders_automation(client):
    response = client.post("/notifications/test")
    assert response.status_code == 200
    assert "no configured channel found" in response.text
    assert 'id="notifications-section"' in response.text


def test_tunnel_filters_save_redirects_to_automation(client):
    response = client.post("/auto-backup/tunnel-filters", data={"mode": "all"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/automation#auto-backup-section"


def test_interim_pages_redirect_in_demo(client, monkeypatch):
    import app.routers.auto_backup as auto_backup_router
    import app.routers.backups as backups_router

    monkeypatch.setattr(backups_router, "DEMO_MODE", True)
    monkeypatch.setattr(auto_backup_router, "DEMO_MODE", True)
    for path in ("/backups", "/automation"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/", path


def test_index_no_longer_has_moved_panels(client):
    text = client.get("/").text
    for panel_id in ("auto-backup-section", "saved-backups-section", "notifications-section"):
        assert f'id="{panel_id}"' not in text
