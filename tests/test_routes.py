"""HTTP-level smoke tests for every page.

They go through the public routes only, so they stay valid however the app is
split into modules internally.
"""

from __future__ import annotations


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "<title>Tikka Masala</title>" in body
    assert 'id="tunnels-section"' in body
    assert 'id="saved-backups-section"' in body
    assert "/static/app.js" in body or "smoothScrollToY" in body


def test_index_lists_saved_backup(client, make_backup):
    backup_id = make_backup(tunnel_name="Index Tunnel")
    response = client.get("/")
    assert response.status_code == 200
    assert f"/backup/{backup_id}" in response.text
    assert "Index Tunnel" in response.text


def test_index_pagination_params(client):
    assert client.get("/?page=2&page_size=10").status_code == 200
    assert client.get("/?page_size=7").status_code == 200


def test_scheduled_runs_page(client):
    response = client.get("/auto-backup/runs")
    assert response.status_code == 200
    assert "Automatic Backup Archive" in response.text
    assert client.get("/auto-backup/runs?page_size=all").status_code == 200


def test_notification_messages_page(client):
    response = client.get("/notifications/messages")
    assert response.status_code == 200
    assert "Notification Messages" in response.text
    assert "manual_backup_success" in response.text


def test_notification_messages_save_and_reset(client):
    response = client.post(
        "/notifications/messages",
        data={"msg_restore_success": "Custom restore {backup_id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Custom restore {backup_id}" in client.get("/notifications/messages").text

    response = client.post("/notifications/messages/reset", follow_redirects=False)
    assert response.status_code == 303
    assert "Custom restore {backup_id}" not in client.get("/notifications/messages").text


def test_tunnel_filters_page_without_credentials(client):
    # No server-side credentials: the page must render without calling Cloudflare.
    response = client.get("/auto-backup/tunnel-filters")
    assert response.status_code == 200
    assert "Advanced Backup Schedule" in response.text


def test_backup_detail_shows_ingress(client, make_backup):
    backup_id = make_backup(
        ingress=[
            {"hostname": "app.example.com", "service": "http://10.0.0.5:8080"},
            {"hostname": "local.example.com", "service": "http://localhost:3000"},
            {"service": "http_status:404"},
        ]
    )
    response = client.get(f"/backup/{backup_id}")
    assert response.status_code == 200
    body = response.text
    assert f"Backup #{backup_id}" in body
    assert '<td data-label="Hostname">app.example.com</td>' in body
    assert "http://localhost:3000" in body


def test_backup_download(client, make_backup):
    backup_id = make_backup()
    response = client.get(f"/backup/{backup_id}/download")
    assert response.status_code == 200
    assert response.json()["tunnel_id"] == "tunnel-1"


def test_backup_missing_returns_404(client):
    assert client.get("/backup/999999").status_code == 404
    assert client.get("/backup/999999/download").status_code == 404


def test_auto_backup_settings_rejects_enable_without_credentials(client):
    response = client.post(
        "/auto-backup/settings",
        data={"enabled": "on", "cron_expression": "0 3 * * *"},
    )
    assert response.status_code == 200
    assert "require a server-side Account ID" in response.text


def test_auto_backup_settings_rejects_bad_cron(client):
    response = client.post("/auto-backup/settings", data={"cron_expression": "not a cron"})
    assert response.status_code == 200
    assert "Invalid cron expression" in response.text


def test_notifications_test_without_channels(client):
    response = client.post("/notifications/test")
    assert response.status_code == 200
    assert "no configured channel found" in response.text


def test_list_tunnels_requires_account_id(client):
    response = client.post("/list-tunnels", data={"api_token": "x"})
    assert response.status_code == 200
    assert "Account ID is required" in response.text


def test_static_assets_served(client):
    assert client.get("/static/styles.css").status_code == 200
