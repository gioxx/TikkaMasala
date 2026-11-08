"""Notifications go to the destinations of the profile they belong to, and name that profile."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import app.backups as backups
import app.credentials as credentials
import app.notifications as notifications
import app.scheduler as scheduler
import app.tunnels as tunnels
from app.destinations import DestinationInput, DestinationInputError, create_destination, validate_destination
from app.notifications import (
    NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS,
    OTHER_ACCOUNTS_LABEL,
    deliver_notification,
    env_delivery_targets,
    get_notification_message_templates,
    notification_profile_name,
    render_notification_message,
    set_notification_message_templates,
)
from app.profiles import DEFAULT_PROFILE_ID, update_profile
from tests.conftest import use_profile

GOOD = "0123456789abcdef0123456789abcdef"
CLIENT = "fedcba9876543210fedcba9876543210"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"
HOOK = "https://hooks.example.com/T000/secret-path-xyz?token=abc"
BOT = "123456789:AAH-secret-bot-token-value-0000"


@pytest.fixture(autouse=True)
def _clean(data_dir, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", "")
    monkeypatch.setattr(notifications, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "")
    saved_templates = get_notification_message_templates()
    yield
    set_notification_message_templates(saved_templates)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM notification_destinations")
        conn.commit()


@pytest.fixture
def posted(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def _fake_post(url, payload):
        calls.append((url, payload))
        return httpx.Response(200)

    monkeypatch.setattr(notifications, "_post_json", _fake_post)
    return calls


def _webhook(url: str, events=("restore_success", "restore_failed"), enabled=True, label="Hook") -> DestinationInput:
    return DestinationInput(kind="webhook", label=label, secret=url, chat_id="", events=tuple(events), enabled=enabled)


def _telegram(events=("restore_success",)) -> DestinationInput:
    return DestinationInput(
        kind="telegram", label="Chat", secret=BOT, chat_id="-1001234567890", events=tuple(events), enabled=True
    )


def _deliver(event="restore_success", *, profile_id, **kwargs):
    return asyncio.run(deliver_notification(event, "message", {"backup_id": 5}, profile_id=profile_id, **kwargs))


# --- Rendering --------------------------------------------------------------


def test_profile_is_a_global_placeholder():
    assert NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS == ["version", "timestamp", "profile"]


def test_profile_placeholder_in_custom_template(second_profile):
    set_notification_message_templates({"restore_success": "{profile}: #{backup_id}"})
    assert render_notification_message("restore_success", {"backup_id": 5}, profile_id=second_profile.id) == "Client: #5"


def test_custom_template_with_unknown_or_broken_placeholders(second_profile):
    set_notification_message_templates({"restore_success": "{profile} {nope} {backup_id}", "restore_failed": "{profile"})
    text = render_notification_message("restore_success", {"backup_id": 5}, profile_id=second_profile.id)
    assert text == "Client {nope} 5"
    assert render_notification_message("restore_failed", {}, profile_id=second_profile.id) == "{profile"


def test_profile_name_in_details_cannot_be_overridden(second_profile):
    set_notification_message_templates({"restore_success": "{profile}"})
    text = render_notification_message("restore_success", {"profile": "spoof"}, profile_id=second_profile.id)
    assert text == "Client"


def test_default_template_names_profile_only_with_two_profiles(second_profile):
    text = render_notification_message("restore_success", {"backup_id": 5, "tunnel_id": "t"}, profile_id=second_profile.id)
    assert text.startswith("[Client] ")
    other = render_notification_message("retention_cleanup", {"deleted_count": 1}, profile_id=None)
    assert other.startswith("[Other accounts] ")


def test_default_template_plain_with_one_profile():
    text = render_notification_message("restore_success", {"backup_id": 5, "tunnel_id": "t"}, profile_id=DEFAULT_PROFILE_ID)
    assert text == "Backup #5 was restored to tunnel t."
    assert "[Default]" not in text


def test_profile_name_with_braces_is_not_reformatted(second_profile):
    update_profile(second_profile.id, name="{version}")
    set_notification_message_templates({"restore_success": "{profile}"})
    assert render_notification_message("restore_success", {}, profile_id=second_profile.id) == "{version}"
    set_notification_message_templates({})
    default = render_notification_message("restore_success", {"backup_id": 1, "tunnel_id": "t"}, profile_id=second_profile.id)
    assert default.startswith("[{version}] ")


def test_notification_profile_name():
    assert notification_profile_name(None) == OTHER_ACCOUNTS_LABEL == "Other accounts"
    assert notification_profile_name(DEFAULT_PROFILE_ID) == "Default"
    assert notification_profile_name(987654) == "Profile 987654"


def test_render_requires_profile_keyword():
    with pytest.raises(TypeError):
        render_notification_message("restore_success", {})  # type: ignore[call-arg]


# --- Delivery ---------------------------------------------------------------


def test_fan_out_to_profile_destinations_only(posted, second_profile):
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://a.example/x"))
    create_destination(second_profile.id, _webhook("https://b.example/y"))
    result = _deliver(profile_id=second_profile.id)
    assert [url for url, _ in posted] == ["https://b.example/y"]
    assert result["sent_count"] == 1
    assert result["successful_channels"] == ["Hook"]


def test_none_profile_goes_to_default_destinations(posted, second_profile):
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://a.example/x", events=("retention_cleanup",)))
    create_destination(second_profile.id, _webhook("https://b.example/y", events=("retention_cleanup",)))
    _deliver("retention_cleanup", profile_id=None)
    assert [url for url, _ in posted] == ["https://a.example/x"]
    assert posted[0][1]["profile"] == {"id": None, "name": "Other accounts"}


def test_env_destinations_only_for_default(posted, second_profile, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", "https://env.example/h")
    notifications.get_webhook_notification_events.cache_clear()
    notifications.parse_notification_events.cache_clear()
    try:
        _deliver("restore_failed", profile_id=DEFAULT_PROFILE_ID)
        assert [url for url, _ in posted] == ["https://env.example/h"]
        posted.clear()
        _deliver("restore_failed", profile_id=second_profile.id)
        assert posted == []
    finally:
        notifications.get_webhook_notification_events.cache_clear()
        notifications.parse_notification_events.cache_clear()


def test_env_delivery_targets_shape(monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", HOOK)
    monkeypatch.setattr(notifications, "TELEGRAM_BOT_TOKEN", BOT)
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "-100987654321")
    targets = env_delivery_targets()
    assert [(t.key, t.label, t.kind) for t in targets] == [
        ("env-webhook", "Webhook (environment)", "webhook"),
        ("env-telegram", "Telegram (environment)", "telegram"),
    ]
    assert targets[1].chat_id == "-100987654321"
    assert HOOK not in repr(targets) and BOT not in repr(targets)
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "")
    assert [t.key for t in env_delivery_targets()] == ["env-webhook"]


def test_event_filter_disabled_and_force(posted):
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://a.example/x", events=("restore_failed",)))
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://off.example/z", events=("restore_success",), enabled=False))
    result = _deliver("restore_success", profile_id=DEFAULT_PROFILE_ID)
    assert posted == []
    assert result["attempted"] is False
    _deliver("restore_success", profile_id=DEFAULT_PROFILE_ID, force=True)
    assert [url for url, _ in posted] == ["https://a.example/x"]


def test_only_targets_one_key(posted):
    a = create_destination(DEFAULT_PROFILE_ID, _webhook("https://a.example/x"))
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://b.example/y"))
    _deliver(profile_id=DEFAULT_PROFILE_ID, only=f"dest-{a.id}", force=True)
    assert [url for url, _ in posted] == ["https://a.example/x"]


def test_payload_has_profile(posted, second_profile):
    create_destination(second_profile.id, _webhook("https://b.example/y"))
    _deliver(profile_id=second_profile.id)
    payload = posted[0][1]
    assert payload["profile"] == {"id": second_profile.id, "name": "Client"}
    assert payload["event"] == "restore_success"
    assert payload["message"] == "message"
    assert payload["details"] == {"backup_id": 5}


def test_failure_logs_no_secret(monkeypatch, caplog):
    async def _boom(url, payload):
        raise httpx.ConnectError(f"boom {HOOK}")

    monkeypatch.setattr(notifications, "_post_json", _boom)
    create_destination(DEFAULT_PROFILE_ID, _webhook(HOOK))
    with caplog.at_level(logging.DEBUG):
        result = _deliver(profile_id=DEFAULT_PROFILE_ID)
    assert result["results"][0]["reason"] == "could not connect (ConnectError)"
    assert result["failed_channels"] == ["Hook"]
    assert "secret-path-xyz" not in caplog.text
    assert "Notification to Hook failed: could not connect (ConnectError)" in caplog.text


def test_failure_of_one_target_does_not_stop_the_others(monkeypatch, caplog):
    calls: list[str] = []

    async def _post(url, payload):
        calls.append(url)
        if "a.example" in url:
            raise RuntimeError(f"bad {url}")
        if "b.example" in url:
            return httpx.Response(500, text=f"echo {url}")
        return httpx.Response(204)

    monkeypatch.setattr(notifications, "_post_json", _post)
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://a.example/secret-a", label="A"))
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://b.example/secret-b", label="B"))
    create_destination(DEFAULT_PROFILE_ID, _webhook("https://c.example/secret-c", label="C"))
    with caplog.at_level(logging.DEBUG):
        result = _deliver(profile_id=DEFAULT_PROFILE_ID)
    assert len(calls) == 3
    assert result["successful_channels"] == ["C"]
    assert result["failed_channels"] == ["A", "B"]
    reasons = {r["label"]: r["reason"] for r in result["results"]}
    assert reasons == {"A": "could not connect (RuntimeError)", "B": "HTTP 500", "C": None}
    assert "secret-" not in caplog.text


def test_telegram_target_url_and_chat(posted):
    create_destination(DEFAULT_PROFILE_ID, _telegram())
    _deliver(profile_id=DEFAULT_PROFILE_ID)
    url, payload = posted[0]
    assert url == f"https://api.telegram.org/bot{BOT}/sendMessage"
    assert payload["chat_id"] == "-1001234567890"
    assert "message" in payload["text"]


def test_send_notification_never_raises(monkeypatch):
    def _broken(profile_id):
        raise RuntimeError("database gone")

    monkeypatch.setattr(notifications, "delivery_targets", _broken)
    asyncio.run(notifications.send_notification("restore_success", "m", {}, profile_id=DEFAULT_PROFILE_ID))


# --- Destination validation carried from Task 6 -----------------------------


@pytest.mark.parametrize("label", ["Ops‮hook", "Ops​hook", "Ops\x07hook", "Ops⁦hook"])
def test_destination_label_rejects_control_characters(label):
    with pytest.raises(DestinationInputError) as info:
        validate_destination(_webhook(HOOK, label=label), creating=True)
    assert info.value.field == "label"
    assert str(info.value) == "Labels cannot contain control characters."


@pytest.mark.parametrize(
    "secret, chat_id, field",
    [
        ("١٢٣456789:AAH-secret-bot-token-value-0000", "-1001234567890", "secret"),
        (BOT, "-١٢٣", "chat_id"),
    ],
)
def test_telegram_patterns_reject_non_ascii_digits(secret, chat_id, field):
    data = DestinationInput(kind="telegram", label="", secret=secret, chat_id=chat_id, events=("restore_failed",), enabled=True)
    with pytest.raises(DestinationInputError) as info:
        validate_destination(data, creating=True)
    assert info.value.field == field


# --- Call sites carry the profile -------------------------------------------


@pytest.fixture
def queued(monkeypatch):
    def _install(module) -> list[dict]:
        recorded: list[dict] = []

        def _queue(event, message, details=None, level="info", *, profile_id):
            recorded.append({"event": event, "message": message, "profile_id": profile_id})

        monkeypatch.setattr(module, "queue_notification", _queue)
        return recorded

    return _install


def test_scheduler_notification_carries_profile(second_profile, monkeypatch, queued):
    recorded = queued(scheduler)

    async def fake_list(account_id, api_token):
        return [{"id": T1, "name": "Home"}]

    async def fake_create(account_id, tunnel_id, api_token, notes=None, origin="manual"):
        return None

    monkeypatch.setattr(scheduler, "list_tunnels", fake_list)
    monkeypatch.setattr(scheduler, "create_backup", fake_create)
    result = asyncio.run(scheduler.run_auto_backup_job(second_profile.id, trigger="manual"))
    assert result["status"] == "success"
    assert [r["profile_id"] for r in recorded] == [second_profile.id]
    assert recorded[0]["message"].startswith("[Client] ")


def test_manual_backup_notification_carries_profile(client, second_profile, monkeypatch, queued):
    recorded = queued(tunnels)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")

    async def fake_create(account_id, tunnel_id, api_token, notes=None, *rest):
        return SimpleNamespace(id=42, tunnel_name="Home", route_count=1, filename="x.json")

    monkeypatch.setattr(tunnels, "create_backup", fake_create)
    use_profile(client, second_profile.id)
    client.post(f"/tunnels/{T1}/backup")
    assert [r["profile_id"] for r in recorded] == [second_profile.id]


def test_new_backup_page_notification_carries_profile(client, second_profile, monkeypatch, queued):
    recorded = queued(tunnels)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")

    async def fake_create(account_id, tunnel_id, api_token, notes=None, *rest):
        return SimpleNamespace(id=42, tunnel_name="Home", route_count=1, filename="x.json")

    monkeypatch.setattr(tunnels, "create_backup", fake_create)
    use_profile(client, second_profile.id)
    client.post("/backups/new", data={"tunnel_id": T1, "notes": ""})
    assert [r["profile_id"] for r in recorded] == [second_profile.id]


def test_restore_notification_carries_profile(client, second_profile, make_backup, monkeypatch, queued):
    import app.routers.backups as backups_router

    recorded = queued(backups_router)

    async def fake_put(*args):
        return {}

    monkeypatch.setattr(backups_router, "cloudflare_put", fake_put)
    backup_id = make_backup(account_id=CLIENT, tunnel_id=T1)
    client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": T1, "mode": "apply"})
    assert [(r["event"], r["profile_id"]) for r in recorded] == [("restore_success", second_profile.id)]


def test_retention_cleanup_carries_profile(second_profile, make_backup, monkeypatch, queued):
    recorded = queued(backups)
    monkeypatch.setattr(backups, "BACKUP_RETENTION_DAYS_RAW", "1")
    update_profile(second_profile.id, retention_days=1)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    make_backup(account_id=CLIENT, created_at=old)
    make_backup(account_id=OTHER, created_at=old)
    backups.purge_expired_backups(now)
    assert {r["profile_id"] for r in recorded} == {second_profile.id, None}


def test_httpx_request_log_is_off():
    # httpx logs "HTTP Request: POST <url>" at INFO; the URL can hold a webhook path or a bot token.
    import app.config  # noqa: F401 - sets the level

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
