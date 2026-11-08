"""Per-profile notification destinations: validation, encryption at rest, masking, delivery targets."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from app import encryption
from app.config import DEFAULT_NOTIFICATION_EVENTS
from app.destinations import (
    DestinationInput,
    DestinationInputError,
    create_destination,
    decode_events,
    delete_destination,
    delivery_targets,
    encode_events,
    get_destination,
    list_destinations,
    update_destination,
    validate_destination,
    webhook_host,
)
from app.profiles import DEFAULT_PROFILE_ID

HOOK = "https://hooks.example.com/T000/secret-path-xyz?token=abc"
BOT = "123456789:AAH-secret-bot-token-value-0000"
UNREADABLE_HINT = "Saved secret cannot be decrypted. Edit the destination and enter it again."


@pytest.fixture(autouse=True)
def _clean_destinations(data_dir):
    yield
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM notification_destinations")
        conn.commit()


def _raw_row(data_dir, destination_id: int) -> sqlite3.Row:
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM notification_destinations WHERE id = ?", (destination_id,)).fetchone()


def _webhook(secret: str = HOOK, **overrides) -> DestinationInput:
    values = dict(
        kind="webhook",
        label="Ops hook",
        secret=secret,
        chat_id="",
        events=("auto_backup_failed", "restore_failed"),
        enabled=True,
    )
    values.update(overrides)
    return DestinationInput(**values)


def _telegram(chat_id: str = "-1001234567890", secret: str = BOT, **overrides) -> DestinationInput:
    values = dict(
        kind="telegram",
        label="Team chat",
        secret=secret,
        chat_id=chat_id,
        events=("auto_backup_failed",),
        enabled=True,
    )
    values.update(overrides)
    return DestinationInput(**values)


def _error(data: DestinationInput, *, creating: bool = True) -> DestinationInputError:
    with pytest.raises(DestinationInputError) as info:
        validate_destination(data, creating=creating)
    return info.value


def test_encode_decode_events():
    assert encode_events(["restore_failed", "manual_backup_success", "restore_failed"]) == (
        '["manual_backup_success", "restore_failed"]'
    )
    for raw in (None, "", "x", '{"a":1}'):
        assert decode_events(raw) == DEFAULT_NOTIFICATION_EVENTS
    assert decode_events("[]") == frozenset()
    assert decode_events('["restore_failed","nope"]') == {"restore_failed"}
    assert decode_events('["restore_failed", 3, null, "notification_test"]') == {"restore_failed"}


def test_webhook_host_tolerates_malformed_urls():
    assert webhook_host(HOOK) == "hooks.example.com"
    assert webhook_host("https://[::1") is None
    assert webhook_host("not a url") is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x.example/a",
        "hooks.example.com/a",
        "https://",
        "https://" + "a" * 2050,
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/plain,hello",
        "https://user:pass@/path",
        "https://evil@",
        "https://[::1/x",
        "https://host.example:99999/x",
        "https://host .example/x",
        "https://host.example/a\nb",
    ],
)
def test_validate_webhook_rejects(url):
    error = _error(_webhook(url))
    assert error.field == "secret"
    assert str(error) == "The webhook URL must start with http:// or https:// and include a host."
    if len(url) > 10:
        assert url not in str(error)


def test_validate_webhook_requires_url_on_create_only():
    error = _error(_webhook(""))
    assert (error.field, str(error)) == ("secret", "Enter the webhook URL.")
    assert validate_destination(_webhook("   "), creating=False).secret == ""


def test_validate_webhook_strips_and_defaults_label():
    data = validate_destination(_webhook(" https://h.example/x ", label="  ", chat_id=" 123 "), creating=True)
    assert data.secret == "https://h.example/x"
    assert data.label == "Webhook"
    assert data.chat_id == ""
    assert data.kind == "webhook"


def test_validate_telegram():
    assert validate_destination(_telegram(), creating=True).chat_id == "-1001234567890"
    data = validate_destination(_telegram("@mychannel", label=""), creating=True)
    assert (data.chat_id, data.label) == ("@mychannel", "Telegram")
    error = _error(_telegram("abc"))
    assert (error.field, str(error)) == ("chat_id", "Enter the Telegram chat ID: a number, or @channelname.")
    error = _error(_telegram(secret="nope"))
    assert (error.field, str(error)) == ("secret", "The bot token should look like 123456789:AA…")
    assert "nope" not in str(error)
    error = _error(_telegram(secret=""))
    assert (error.field, str(error)) == ("secret", "Enter the Telegram bot token.")
    # On edit an empty token keeps the saved one, but the chat ID is still required.
    assert validate_destination(_telegram(secret=""), creating=False).secret == ""
    assert _error(_telegram(""), creating=False).field == "chat_id"


def test_validate_kind():
    error = _error(_webhook(kind="email"))
    assert (error.field, str(error)) == ("kind", "Choose webhook or Telegram.")


def test_validate_events_and_label():
    error = _error(_webhook(events=()))
    assert (error.field, str(error)) == ("events", "Pick at least one event.")
    error = _error(_webhook(events=("notification_test",)))
    assert (error.field, str(error)) == ("events", "Unknown event: notification_test.")
    error = _error(_webhook(label="x" * 61))
    assert (error.field, str(error)) == ("label", "Labels can be at most 60 characters.")
    assert validate_destination(_webhook(label="x" * 60), creating=True).label == "x" * 60
    data = validate_destination(
        _webhook(events=(" restore_failed", "auto_backup_failed", "restore_failed")), creating=True
    )
    assert data.events == ("auto_backup_failed", "restore_failed")


def test_create_encrypts_secret(data_dir):
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    raw = dict(_raw_row(data_dir, view.id))
    assert raw["target"].startswith("enc:")
    assert "secret-path-xyz" not in repr(raw)
    assert raw["events"] == '["auto_backup_failed", "restore_failed"]'
    assert raw["enabled"] == 1
    tg = create_destination(DEFAULT_PROFILE_ID, _telegram())
    raw = dict(_raw_row(data_dir, tg.id))
    assert raw["target"].startswith("enc:")
    assert "secret-bot-token" not in repr(raw)
    assert raw["chat_id"] == "-1001234567890"


@pytest.mark.parametrize("key_source", ["environment", "file"])
def test_create_encrypts_with_any_key_source(data_dir, monkeypatch, tmp_path, key_source):
    if key_source == "environment":
        monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    else:
        monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
        monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", tmp_path / "encryption.key")
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    raw = _raw_row(data_dir, view.id)
    assert raw["target"].startswith("enc:")
    assert "secret-path-xyz" not in raw["target"]
    assert delivery_targets(DEFAULT_PROFILE_ID)[0].secret == HOOK


def test_create_with_broken_key_reports_secret_error(data_dir, monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(DestinationInputError) as info:
        create_destination(DEFAULT_PROFILE_ID, _webhook())
    assert info.value.field == "secret"
    assert str(info.value).startswith("The secret cannot be saved: ")
    assert "secret-path-xyz" not in str(info.value)
    assert list_destinations(DEFAULT_PROFILE_ID) == []


def test_view_masks_and_repr():
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    assert view.target_hint == "hooks.example.com"
    assert (view.kind, view.label, view.enabled, view.unreadable, view.chat_id) == (
        "webhook",
        "Ops hook",
        True,
        False,
        "",
    )
    assert view.events == {"auto_backup_failed", "restore_failed"}
    assert "secret-path" not in repr(view)
    assert not hasattr(view, "secret")
    tg = create_destination(DEFAULT_PROFILE_ID, _telegram())
    assert tg.target_hint == "chat ending in 7890"
    assert "secret-bot-token" not in repr(tg)
    targets = delivery_targets(DEFAULT_PROFILE_ID)
    assert [t.secret for t in targets] == [HOOK, BOT]
    for target in targets:
        assert "secret-path" not in repr(target)
        assert "secret-bot-token" not in repr(target)
    assert "secret-path" not in repr(_webhook())
    assert "secret-bot-token" not in repr(_telegram())


def test_view_of_unparseable_saved_url(data_dir):
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute(
            "UPDATE notification_destinations SET target = ? WHERE id = ?",
            (encryption.encrypt_secret("https://[::1/secret-path"), view.id),
        )
        conn.commit()
    assert get_destination(view.id).target_hint == "URL saved"


def test_update_keeps_secret_when_empty():
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    updated = update_destination(view.id, _webhook("", label="Renamed", enabled=False))
    assert (updated.label, updated.enabled) == ("Renamed", False)
    update_destination(view.id, _webhook(""))
    assert delivery_targets(DEFAULT_PROFILE_ID)[0].secret == HOOK
    update_destination(view.id, _webhook("https://new.example/other"))
    assert delivery_targets(DEFAULT_PROFILE_ID)[0].secret == "https://new.example/other"
    assert get_destination(view.id).target_hint == "new.example"
    with pytest.raises(DestinationInputError) as info:
        update_destination(view.id, _telegram())
    assert (info.value.field, str(info.value)) == ("kind", "The kind of a destination cannot change.")
    with pytest.raises(LookupError):
        update_destination(view.id + 1000, _webhook(""))


def test_unreadable_secret_flagged_and_skipped(data_dir, caplog):
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("UPDATE notification_destinations SET target = 'enc:garbage' WHERE id = ?", (view.id,))
        conn.commit()
    caplog.set_level(logging.WARNING)
    flagged = get_destination(view.id)
    assert flagged.unreadable
    assert flagged.target_hint == UNREADABLE_HINT
    assert list_destinations(DEFAULT_PROFILE_ID)[0].unreadable
    assert delivery_targets(DEFAULT_PROFILE_ID) == []
    assert "garbage" not in caplog.text
    assert f"Notification destination {view.id}" in caplog.text
    # Entering the secret again repairs it.
    update_destination(view.id, _webhook())
    assert not get_destination(view.id).unreadable


def test_delivery_targets_per_profile_and_enabled(second_profile):
    a = create_destination(DEFAULT_PROFILE_ID, _webhook())
    b = create_destination(second_profile.id, _telegram())
    create_destination(second_profile.id, _webhook(enabled=False))
    targets = delivery_targets(second_profile.id)
    assert [t.key for t in targets] == [f"dest-{b.id}"]
    target = targets[0]
    assert (target.label, target.kind, target.events, target.secret, target.chat_id) == (
        "Team chat",
        "telegram",
        frozenset({"auto_backup_failed"}),
        BOT,
        "-1001234567890",
    )
    assert [t.key for t in delivery_targets(None)] == [f"dest-{a.id}"]
    assert [v.id for v in list_destinations(DEFAULT_PROFILE_ID)] == [a.id]
    assert len(list_destinations(second_profile.id)) == 2


def test_delete():
    view = create_destination(DEFAULT_PROFILE_ID, _webhook())
    delete_destination(view.id)
    assert list_destinations(DEFAULT_PROFILE_ID) == []
    with pytest.raises(LookupError):
        get_destination(view.id)


def test_input_is_frozen():
    data = _webhook()
    with pytest.raises(AttributeError):
        data.secret = "x"  # type: ignore[misc]
    assert replace(data, label="y").label == "y"


def test_destination_events_are_supported_and_exclude_tests():
    from app.config import SUPPORTED_NOTIFICATION_EVENTS
    from app.destinations import DESTINATION_EVENTS

    assert set(DESTINATION_EVENTS) == SUPPORTED_NOTIFICATION_EVENTS - {"notification_test"}
    assert DEFAULT_NOTIFICATION_EVENTS <= set(DESTINATION_EVENTS)
