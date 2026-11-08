"""Notification message templates live under Settings."""

from __future__ import annotations

import re

import app.routers.notifications as notifications_router
from app.notifications import get_notification_message_templates, set_notification_message_templates

CUSTOM = "Custom restore {backup_id}"


def test_messages_page_under_settings(client):
    response = client.get("/settings/notifications")
    assert response.status_code == 200
    text = response.text
    assert 'action="/settings/notifications"' in text
    assert 'action="/settings/notifications/reset"' in text
    assert 'href="/settings"' in text
    assert re.search(r'<a[^>]*href="/settings"[^>]*aria-current="page"|aria-current="page"[^>]*href="/settings"', text)


def test_messages_save_and_reset(client):
    response = client.post(
        "/settings/notifications", data={"msg_restore_success": CUSTOM}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/notifications?saved=1"
    assert CUSTOM in client.get("/settings/notifications").text

    response = client.post("/settings/notifications/reset", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/notifications?reset=1"
    assert CUSTOM not in client.get("/settings/notifications").text


def test_settings_links_messages(client):
    assert 'href="/settings/notifications"' in client.get("/settings").text


def test_automation_links_messages(client):
    assert 'href="/settings/notifications"' in client.get("/automation").text


def test_messages_demo_redirects(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "DEMO_MODE", True)
    for method, path in (
        ("GET", "/settings/notifications"),
        ("POST", "/settings/notifications"),
        ("POST", "/settings/notifications/reset"),
    ):
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/", path


def test_legacy_messages_post_saves(client):
    try:
        response = client.post("/notifications/messages", data={"msg_restore_success": CUSTOM})
        assert response.status_code == 200
        assert get_notification_message_templates().get("restore_success") == CUSTOM
    finally:
        set_notification_message_templates({})
