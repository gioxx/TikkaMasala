"""Notification destinations on Automation: add, edit, delete and test them per profile."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import httpx
import pytest

import app.notifications as notifications
from app.config import DEFAULT_NOTIFICATION_EVENTS
from app.destinations import (
    DESTINATION_EVENTS,
    DestinationInput,
    create_destination,
    delete_destination,
    get_destination,
    list_destinations,
    update_destination,
)
from app.profiles import DEFAULT_PROFILE_ID
from tests.conftest import use_profile

HOOK = "https://hooks.example.com/T000/secret-path-xyz?token=abc"
BOT = "123456789:AAH-secret-bot-token-value-0000"
ENV_HOOK = "https://env-hooks.example.net/env-secret-path"
CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}
ADDED = "/automation?destination=added#notifications-section"


@pytest.fixture(autouse=True)
def _clean(data_dir, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", "")
    monkeypatch.setattr(notifications, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "")
    notifications.get_webhook_notification_events.cache_clear()
    notifications.get_telegram_notification_events.cache_clear()
    notifications.parse_notification_events.cache_clear()
    yield
    notifications.get_webhook_notification_events.cache_clear()
    notifications.get_telegram_notification_events.cache_clear()
    notifications.parse_notification_events.cache_clear()
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


def _hook(label="Ops", url=HOOK, enabled=True, events=("auto_backup_failed",)) -> DestinationInput:
    return DestinationInput(kind="webhook", label=label, secret=url, chat_id="", events=events, enabled=enabled)


def _env_both(monkeypatch):
    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", ENV_HOOK)
    monkeypatch.setattr(notifications, "TELEGRAM_BOT_TOKEN", BOT)
    monkeypatch.setattr(notifications, "TELEGRAM_CHAT_ID", "-100987654321")


def _section(text: str) -> str:
    start = text.index('id="notifications-section"')
    return text[start:text.index("</section>", start)]


def test_new_form_defaults(client):
    text = client.get("/automation/destinations/new?kind=telegram").text
    assert "Add Telegram" in text
    assert 'name="chat_id"' in text
    assert 'type="password"' in text and 'autocomplete="off"' in text
    assert "Bot token" in text
    # Unknown kinds fall back to webhook.
    text = client.get("/automation/destinations/new?kind=email").text
    assert "Add webhook" in text and "Webhook URL" in text
    assert 'name="chat_id"' not in text
    checked = re.findall(r'name="events" value="([a-z_]+)"\s+checked', text)
    assert checked and set(checked) == set(DEFAULT_NOTIFICATION_EVENTS) & set(DESTINATION_EVENTS)


def test_add_webhook_flow(client, posted):
    response = client.post(
        "/automation/destinations",
        data={"kind": "webhook", "label": "Ops", "secret": HOOK, "events": ["auto_backup_failed"], "enabled": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == ADDED
    text = client.get("/automation?destination=added").text
    section = _section(text)
    assert "Ops" in section and '<span class="destination-hint">hooks.example.com</span>' in section
    assert "Destination added." in section
    assert "Automatic backup failed" in section
    assert "secret-path-xyz" not in text
    (view,) = list_destinations(DEFAULT_PROFILE_ID)
    assert f'id="destination-{view.id}"' in section
    edit = client.get(f"/automation/destinations/{view.id}/edit").text
    assert "Edit Ops" in edit
    assert "secret-path-xyz" not in edit
    assert "Leave empty to keep the saved URL" in edit


def test_add_validation_400_no_secret_echo(client):
    response = client.post(
        "/automation/destinations", data={"kind": "webhook", "label": "Ops", "secret": HOOK, "enabled": "on"}
    )
    assert response.status_code == 400
    assert "Pick at least one event." in response.text
    assert "secret-path-xyz" not in response.text
    assert 'value="Ops"' in response.text
    assert list_destinations(DEFAULT_PROFILE_ID) == []


def test_add_telegram_validation(client):
    response = client.post(
        "/automation/destinations",
        data={"kind": "telegram", "label": "Chat", "secret": BOT, "chat_id": "abc", "events": ["restore_failed"]},
    )
    assert response.status_code == 400
    assert "Enter the Telegram chat ID" in response.text
    assert BOT not in response.text
    assert "secret-bot-token" not in response.text
    assert 'value="abc"' in response.text
    # The enabled box was left unticked: it stays unticked.
    assert not re.search(r'name="enabled"[^>]*checked', response.text)


def test_edit_keeps_secret_when_blank(client, posted):
    view = create_destination(DEFAULT_PROFILE_ID, _hook())
    response = client.post(
        f"/automation/destinations/{view.id}",
        data={"kind": "webhook", "label": "New", "secret": "", "events": ["auto_backup_failed"], "enabled": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/automation?destination=saved#notifications-section"
    assert get_destination(view.id).label == "New"
    assert "Destination saved." in client.get("/automation?destination=saved").text
    client.post(f"/automation/destinations/{view.id}/test")
    assert [url for url, _ in posted] == [HOOK]


def test_edit_validation_no_secret_echo(client):
    view = create_destination(DEFAULT_PROFILE_ID, _hook())
    response = client.post(
        f"/automation/destinations/{view.id}",
        data={"kind": "webhook", "label": "New", "secret": "https://x.example/leak-me", "events": []},
    )
    assert response.status_code == 400
    assert "leak-me" not in response.text
    assert 'value="New"' in response.text
    assert get_destination(view.id).label == "Ops"


def test_delete(client):
    view = create_destination(DEFAULT_PROFILE_ID, _hook())
    response = client.post(f"/automation/destinations/{view.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/automation?destination=deleted#notifications-section"
    text = client.get("/automation?destination=deleted").text
    assert f'id="destination-{view.id}"' not in text
    assert "Destination deleted." in text
    assert "No destinations yet." in text


def test_other_profile_destination_404(client, second_profile, posted):
    view = create_destination(second_profile.id, _hook(label="Theirs"))
    edit = f"/automation/destinations/{view.id}/edit"
    form = {"kind": "webhook", "label": "Hijacked", "secret": "", "events": ["auto_backup_failed"], "enabled": "on"}
    for response in (
        client.get(edit),
        client.post(f"/automation/destinations/{view.id}", data=form),
        client.post(f"/automation/destinations/{view.id}/test"),
        client.post(f"/automation/destinations/{view.id}/delete"),
    ):
        assert response.status_code == 404
        assert "Destination not found." in response.text
        assert "hooks.example.com" not in response.text
    assert get_destination(view.id).label == "Theirs"
    assert posted == []
    assert f'id="destination-{view.id}"' not in client.get("/automation").text

    use_profile(client, second_profile.id)
    assert client.get(edit).status_code == 200
    response = client.post(f"/automation/destinations/{view.id}", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert client.post(f"/automation/destinations/{view.id}/test").status_code == 200
    assert len(posted) == 1
    assert client.post(f"/automation/destinations/{view.id}/delete", follow_redirects=False).status_code == 303
    assert list_destinations(second_profile.id) == []


def test_unknown_destination_404(client):
    assert client.get("/automation/destinations/999999/edit").status_code == 404
    assert client.post("/automation/destinations/999999/delete").status_code == 404


def test_repository_profile_guard(second_profile):
    view = create_destination(second_profile.id, _hook())
    with pytest.raises(LookupError):
        update_destination(view.id, _hook(label="X", url=""), profile_id=DEFAULT_PROFILE_ID)
    with pytest.raises(LookupError):
        delete_destination(view.id, profile_id=DEFAULT_PROFILE_ID)
    assert get_destination(view.id).label == "Ops"
    assert update_destination(view.id, _hook(label="Y", url=""), profile_id=second_profile.id).label == "Y"
    delete_destination(view.id, profile_id=second_profile.id)
    with pytest.raises(LookupError):
        get_destination(view.id)


def test_per_destination_test_sends_only_that_one(client, posted):
    a = create_destination(DEFAULT_PROFILE_ID, _hook(label="A", url="https://a.example/x"))
    create_destination(DEFAULT_PROFILE_ID, _hook(label="B", url="https://b.example/y"))
    response = client.post(f"/automation/destinations/{a.id}/test")
    assert response.status_code == 200
    assert [url for url, _ in posted] == ["https://a.example/x"]
    assert posted[0][1]["event"] == "notification_test"
    assert "Test notification sent to A." in response.text
    assert 'id="notifications-notice"' in response.text


def test_test_failure_notice_no_secret(client, monkeypatch):
    async def _fail(url, payload):
        return httpx.Response(404, text=f"not found: {url}")

    monkeypatch.setattr(notifications, "_post_json", _fail)
    view = create_destination(DEFAULT_PROFILE_ID, _hook())
    response = client.post(f"/automation/destinations/{view.id}/test")
    assert "Test notification to Ops failed: HTTP 404." in response.text
    assert HOOK not in response.text
    assert "secret-path-xyz" not in response.text


def test_test_exception_notice_names_type_only(client, monkeypatch):
    async def _boom(url, payload):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(notifications, "_post_json", _boom)
    view = create_destination(DEFAULT_PROFILE_ID, _hook())
    text = client.post(f"/automation/destinations/{view.id}/test").text
    assert "Test notification to Ops failed: could not connect (ConnectError)." in text
    assert "secret-path-xyz" not in text


def test_disabled_destination_test_refused(client, posted):
    view = create_destination(DEFAULT_PROFILE_ID, _hook(enabled=False))
    response = client.post(f"/automation/destinations/{view.id}/test")
    assert "Ops is disabled." in response.text
    assert posted == []
    section = _section(client.get("/automation").text)
    row = section[section.index(f'id="destination-{view.id}"'):]
    test_form = row[row.index(f'action="/automation/destinations/{view.id}/test"'):]
    assert "disabled" in test_form[:test_form.index("</form>")]
    assert ">Disabled<" in row


def test_unreadable_destination_test_refused(client, posted, data_dir):
    view = create_destination(DEFAULT_PROFILE_ID, _hook())
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("UPDATE notification_destinations SET target = 'enc:garbage' WHERE id = ?", (view.id,))
        conn.commit()
    response = client.post(f"/automation/destinations/{view.id}/test")
    assert "Ops: the saved secret cannot be decrypted." in response.text
    assert posted == []
    assert "notice warn" in _section(client.get("/automation").text)


def test_test_all_with_only_disabled_destinations(client, posted):
    create_destination(DEFAULT_PROFILE_ID, _hook(enabled=False))
    response = client.post("/automation/notifications/test")
    assert "No enabled destinations to test. Enable one or add a new destination." in response.text
    assert posted == []


def test_env_rows_only_on_default(client, second_profile, monkeypatch, posted):
    monkeypatch.setattr(notifications, "NOTIFICATION_WEBHOOK_URL", ENV_HOOK)
    section = _section(client.get("/automation").text)
    assert 'id="destination-env-webhook"' in section
    assert 'id="destination-env-telegram"' not in section
    assert "From environment" in section
    assert 'action="/automation/destinations/env/webhook/test"' in section
    assert "env-secret-path" not in section

    use_profile(client, second_profile.id)
    section = _section(client.get("/automation").text)
    assert "destination-env-" not in section
    assert "From environment" not in section
    assert client.post("/automation/destinations/env/webhook/test").status_code == 404
    assert posted == []


def test_env_test_not_configured_or_unknown_404(client):
    assert client.post("/automation/destinations/env/webhook/test").status_code == 404
    assert client.post("/automation/destinations/env/email/test").status_code == 404


def test_env_test_per_destination(client, posted, monkeypatch):
    _env_both(monkeypatch)
    response = client.post("/automation/destinations/env/telegram/test")
    assert response.status_code == 200
    assert [url for url, _ in posted] == [f"https://api.telegram.org/bot{BOT}/sendMessage"]
    assert "Test notification sent to Telegram (environment)." in response.text
    assert BOT not in response.text


def test_test_all_covers_profile_destinations(client, posted, second_profile, monkeypatch):
    _env_both(monkeypatch)
    create_destination(second_profile.id, _hook(label="Client hook"))
    create_destination(DEFAULT_PROFILE_ID, _hook(label="Default hook", url="https://default.example/x"))
    use_profile(client, second_profile.id)
    response = client.post("/automation/notifications/test")
    assert [url for url, _ in posted] == [HOOK]
    assert "Client hook" in response.text


def test_card_markup(client):
    section = _section(client.get("/automation").text)
    assert "Where this profile's notifications go. Saved URLs and tokens are never shown again." in section
    assert 'href="/automation/destinations/new?kind=webhook"' in section
    assert 'href="/automation/destinations/new?kind=telegram"' in section
    assert 'href="/settings/notifications"' in section
    assert "Send test to all destinations" in section


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/automation/destinations/new"),
        ("post", "/automation/destinations"),
        ("get", "/automation/destinations/1/edit"),
        ("post", "/automation/destinations/1"),
        ("post", "/automation/destinations/1/delete"),
        ("post", "/automation/destinations/1/test"),
        ("post", "/automation/destinations/env/webhook/test"),
    ],
)
def test_demo_redirects(client, monkeypatch, method, path):
    import app.routers.destinations as destinations_router

    monkeypatch.setattr(destinations_router, "DEMO_MODE", True)
    response = getattr(client, method)(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_cross_site_rejected(client):
    response = client.post(
        "/automation/destinations",
        data={"kind": "webhook", "label": "Ops", "secret": HOOK, "events": ["auto_backup_failed"]},
        headers=CROSS_SITE,
    )
    assert response.status_code == 403
    assert list_destinations(DEFAULT_PROFILE_ID) == []


def test_secret_input_blocks_password_managers(client):
    for path in ("/automation/destinations/new", "/automation/destinations/new?kind=telegram"):
        text = client.get(path).text
        field = re.search(r'<input type="password" name="secret"[^>]*>', text, re.S).group(0)
        assert 'autocomplete="new-password"' in field
        assert "data-1p-ignore" in field and 'data-lpignore="true"' in field


def test_invalid_secret_rerender(client):
    response = client.post(
        "/automation/destinations",
        data={"kind": "webhook", "label": "Ops", "secret": "not a url secret-leak", "events": ["restore_failed"]},
    )
    assert response.status_code == 400
    assert "secret-leak" not in response.text
    field = re.search(r'<input type="password" name="secret"[^>]*>', response.text, re.S).group(0)
    assert 'aria-invalid="true"' in field
    assert 'value=""' in field
    assert list_destinations(DEFAULT_PROFILE_ID) == []


def test_telegram_edit_page_never_contains_token(client):
    view = create_destination(
        DEFAULT_PROFILE_ID,
        DestinationInput(
            kind="telegram", label="Chat", secret=BOT, chat_id="-100123", events=("restore_failed",), enabled=True
        ),
    )
    text = client.get(f"/automation/destinations/{view.id}/edit").text
    assert BOT not in text
    assert "AAH-secret" not in text
    assert "Leave empty to keep the saved token" in text


def test_ticked_events_survive_400_rerender(client):
    chosen = ["restore_failed", "auto_backup_failed"]
    response = client.post(
        "/automation/destinations",
        data={"kind": "webhook", "label": "", "secret": "x" * 3, "events": chosen, "enabled": "on"},
    )
    assert response.status_code == 400
    ticked = re.findall(r'<input type="checkbox" name="events" value="([^"]+)"\s+checked', response.text)
    assert sorted(ticked) == sorted(chosen)


def test_events_fieldset_not_aria_invalid(client):
    response = client.post(
        "/automation/destinations", data={"kind": "webhook", "label": "Ops", "secret": HOOK, "events": []}
    )
    assert response.status_code == 400
    assert "Pick at least one event." in response.text
    fieldset = re.search(r"<fieldset[^>]*>", response.text).group(0)
    assert "aria-invalid" not in fieldset
    assert 'aria-describedby="destination-error"' in fieldset


def test_destination_controls_have_accessible_names(client):
    create_destination(DEFAULT_PROFILE_ID, _hook(label="Ops room"))
    section = _section(client.get("/automation").text)
    assert 'Test<span class="sr-only"> Ops room</span>' in section
    assert 'Edit<span class="sr-only"> Ops room</span>' in section
    assert 'Delete…<span class="sr-only"> Ops room</span>' in section


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/automation/destinations/abc/edit"),
        ("post", "/automation/destinations/abc"),
        ("post", "/automation/destinations/abc/delete"),
        ("post", "/automation/destinations/abc/test"),
    ],
)
def test_non_integer_destination_id_is_404(client, method, path):
    assert getattr(client, method)(path).status_code == 404
