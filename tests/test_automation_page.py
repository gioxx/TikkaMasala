"""Automation overview: schedule, status, Run now and read-only notification destinations."""

from __future__ import annotations

import pytest

from app.config import DEFAULT_AUTO_BACKUP_CRON
from app.db import set_setting
from app.profiles import DEFAULT_PROFILE_ID
from app.scheduler import (
    get_auto_backup_cron,
    get_auto_backup_enabled,
    get_saved_auto_backup_timezone,
    set_auto_backup_cron,
    set_auto_backup_enabled,
)

GOOD = "0123456789abcdef0123456789abcdef"
CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}


@pytest.fixture(autouse=True)
def _restore_schedule():
    yield
    set_auto_backup_cron(DEFAULT_PROFILE_ID, DEFAULT_AUTO_BACKUP_CRON)
    set_auto_backup_enabled(DEFAULT_PROFILE_ID, False)
    set_setting("auto_backup_timezone", "")


@pytest.fixture
def recorders(monkeypatch):
    import app.routers.automation as automation

    calls = {"all": 0, "single": 0}

    def fake_all() -> None:
        calls["all"] += 1

    def fake_single(profile_id: int) -> None:
        calls["single"] += 1

    monkeypatch.setattr(automation, "configure_all_auto_backup_jobs", fake_all)
    monkeypatch.setattr(automation, "configure_auto_backup_job", fake_single)
    yield calls
    set_auto_backup_cron(DEFAULT_PROFILE_ID, DEFAULT_AUTO_BACKUP_CRON)
    set_auto_backup_enabled(DEFAULT_PROFILE_ID, False)
    set_setting("auto_backup_timezone", "")


def _section(text: str, section_id: str) -> str:
    start = text.index(f'id="{section_id}"')
    return text[start:text.index("</section>", start)]


def test_automation_sections(client):
    response = client.get("/automation")
    assert response.status_code == 200
    text = response.text
    for section_id in ("auto-backup-section", "runs-section", "notifications-section"):
        assert f'id="{section_id}"' in text
    assert 'action="/automation/schedule"' in text
    assert 'action="/automation/run"' in text
    assert 'action="/automation/notifications/test"' in text


def test_no_run_yet_copy(client):
    text = client.get("/automation").text
    assert "No run yet" in text
    assert "Never run" not in text


def test_schedule_saves_cron_for_active_profile(client, configured_default, recorders):
    response = client.post("/automation/schedule", data={"cron_expression": "0 4 * * *"})
    assert response.status_code == 200
    assert "Automatic backups disabled." in response.text
    assert get_auto_backup_cron(DEFAULT_PROFILE_ID) == "0 4 * * *"


def test_schedule_bad_cron_is_400_and_keeps_timezone(client):
    set_setting("auto_backup_timezone", "UTC")
    response = client.post(
        "/automation/schedule", data={"cron_expression": "not a cron", "browser_timezone": "Europe/Rome"}
    )
    assert response.status_code == 400
    assert "Invalid cron expression" in response.text
    assert 'id="schedule-notice"' in response.text
    assert get_saved_auto_backup_timezone() == "UTC"


def test_schedule_unknown_timezone_is_400(client):
    before = get_auto_backup_cron(DEFAULT_PROFILE_ID)
    response = client.post(
        "/automation/schedule", data={"cron_expression": "0 6 * * *", "browser_timezone": "Mars/Base"}
    )
    assert response.status_code == 400
    assert "Unknown timezone: Mars/Base" in response.text
    assert get_auto_backup_cron(DEFAULT_PROFILE_ID) == before


def test_schedule_enable_without_credentials_is_400(client):
    response = client.post("/automation/schedule", data={"enabled": "on", "cron_expression": "0 3 * * *"})
    assert response.status_code == 400
    assert "require a server-side Account ID" in response.text
    assert get_auto_backup_enabled(DEFAULT_PROFILE_ID) is False


def test_timezone_change_reconfigures_all_profiles(client, configured_default, recorders):
    data = {"cron_expression": "0 3 * * *", "browser_timezone": "Europe/Rome"}
    assert client.post("/automation/schedule", data=data).status_code == 200
    assert recorders == {"all": 1, "single": 0}
    assert get_saved_auto_backup_timezone() == "Europe/Rome"
    assert client.post("/automation/schedule", data=data).status_code == 200
    assert recorders == {"all": 1, "single": 1}


def test_run_now_reports_result(client, configured_default, monkeypatch):
    import app.routers.automation as automation

    async def fake_run(profile_id, trigger="scheduled"):
        return {"status": "success", "tunnel_count": 2, "backup_count": 2, "error_count": 0}

    monkeypatch.setattr(automation, "run_auto_backup_job", fake_run)
    response = client.post("/automation/run")
    assert response.status_code == 200
    assert "Automatic backup run completed: 2 backup(s) created from 2 tunnel(s)." in response.text
    assert 'id="run-notice"' in response.text


def test_run_now_skipped_uses_shared_lock_copy(client, configured_default, monkeypatch):
    import app.routers.automation as automation

    async def fake_run(profile_id, trigger="scheduled"):
        return {"status": "skipped", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

    monkeypatch.setattr(automation, "run_auto_backup_job", fake_run)
    response = client.post("/automation/run")
    assert response.status_code == 200
    assert "Automatic backup run skipped because another backup run is already in progress." in response.text


def test_run_now_continues_in_background(client, configured_default, monkeypatch):
    import app.routers.automation as automation

    async def fake_run(profile_id, trigger="scheduled"):  # pragma: no cover - never awaited
        return {}

    async def fake_finish(coro, *, name, grace=None):
        coro.close()
        return False, None

    monkeypatch.setattr(automation, "run_auto_backup_job", fake_run)
    monkeypatch.setattr(automation, "finish_or_continue", fake_finish)
    response = client.post("/automation/run")
    assert response.status_code == 200
    assert "continues in the background" in response.text
    assert 'id="run-notice"' in response.text


def test_run_now_without_credentials_is_400(client):
    response = client.post("/automation/run")
    assert response.status_code == 400
    assert "require a server-side Account ID" in response.text


def test_notifications_test_without_channels(client):
    response = client.post("/automation/notifications/test")
    assert response.status_code == 200
    assert "no configured channel found" in response.text
    assert 'id="notifications-notice"' in response.text


def test_env_destinations_hide_secrets(client, monkeypatch):
    import app.notifications as notifications

    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", "https://hooks.example.com/T0/secret-path-abc")
    monkeypatch.setattr(notifications, "TELEGRAM_BOT_TOKEN", "123:bot-secret")
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "-100987654321")
    text = client.get("/automation").text
    assert "hooks.example.com" in text
    assert "chat ending in 4321" in text
    assert "secret-path-abc" not in text
    assert "bot-secret" not in text
    assert "-100987654321" not in text
    section = _section(text, "notifications-section")
    assert section.count(">Configured<") == 2
    assert "Set with NOTIFICATION_WEBHOOK_URL, NOTIFICATION_WEBHOOK_EVENTS" in section
    assert "Set with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_NOTIFICATION_EVENTS" in section


def test_env_notification_destinations_shape(monkeypatch):
    import app.notifications as notifications

    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", "https://hooks.example.com/x")
    monkeypatch.setattr(notifications, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "")
    webhook, telegram = notifications.env_notification_destinations()
    assert (webhook.kind, webhook.label, webhook.configured, webhook.target_hint) == (
        "webhook", "Webhook", True, "hooks.example.com",
    )
    assert webhook.events == sorted(webhook.events) and webhook.events
    assert (telegram.kind, telegram.label, telegram.configured, telegram.target_hint, telegram.events) == (
        "telegram", "Telegram", False, None, [],
    )


def test_destinations_not_configured(client):
    text = client.get("/automation").text
    section = _section(text, "notifications-section")
    assert section.count(">Not configured<") == 2
    assert "Destinations come from environment variables and are read-only here." in section
    form_start = text.index('action="/automation/notifications/test"')
    form = text[form_start:text.index("</form>", form_start)]
    assert "disabled" in form


@pytest.mark.parametrize("path", ["/automation/schedule", "/automation/run", "/automation/notifications/test"])
def test_demo_posts_redirect_home(client, monkeypatch, path):
    import app.routers.automation as automation

    monkeypatch.setattr(automation, "DEMO_MODE", True)
    response = client.post(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    response = client.get("/automation", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_cross_site_run_rejected(client, monkeypatch):
    import app.routers.automation as automation

    calls: list[int] = []

    async def fake_run(profile_id, trigger="scheduled"):
        calls.append(profile_id)
        return {"status": "success", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

    monkeypatch.setattr(automation, "run_auto_backup_job", fake_run)
    response = client.post("/automation/run", headers=CROSS_SITE)
    assert response.status_code == 403
    assert calls == []


def test_tunnel_filters_save_redirects_to_tunnels(client):
    response = client.post("/automation/tunnels", data={"mode": "all"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/automation/tunnels?saved=1"


def test_index_no_longer_has_moved_panels(client):
    text = client.get("/").text
    for panel_id in ("auto-backup-section", "saved-backups-section", "notifications-section"):
        assert f'id="{panel_id}"' not in text


@pytest.mark.parametrize("url", ["https://[hooks/x", "https://hooks.example.com:notaport/x"])
def test_malformed_webhook_url_does_not_break_page(client, monkeypatch, url):
    import app.notifications as notifications

    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", url)
    response = client.get("/automation")
    assert response.status_code == 200
    webhook = notifications.env_notification_destinations()[0]
    assert webhook.configured is True


def test_env_destinations_hide_userinfo_and_query(client, monkeypatch):
    import app.notifications as notifications

    monkeypatch.setattr(
        notifications, "NOTIFICATION_WEBHOOK_URL", "https://user:pw@hooks.example.com/T0/secret-path-abc?token=q"
    )
    text = client.get("/automation").text
    assert "hooks.example.com" in text
    for secret in ("user:pw", "pw@", "secret-path-abc", "token=q"):
        assert secret not in text
