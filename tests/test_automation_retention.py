"""Retention editing on the Automation page."""

from __future__ import annotations

import pytest

import app.backups as backups
from app.backups import (
    RETENTION_INPUT_ERROR,
    get_backup_retention_status,
    parse_retention_days_input,
)
from app.profiles import DEFAULT_PROFILE_ID, get_profile, update_profile


@pytest.fixture(autouse=True)
def _reset_retention(monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_RETENTION_DAYS_RAW", "")
    update_profile(DEFAULT_PROFILE_ID, retention_days=None)
    yield
    update_profile(DEFAULT_PROFILE_ID, retention_days=None)


@pytest.mark.parametrize("raw,expected", [("", None), ("  ", None), (" 30 ", 30), ("1", 1), ("36500", 36500)])
def test_parse_retention_days_input_ok(raw, expected):
    assert parse_retention_days_input(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-3", "1.5", "+5", "abc", "36501", "1e3"])
def test_parse_retention_days_input_rejects(raw):
    with pytest.raises(ValueError, match="whole number of days"):
        parse_retention_days_input(raw)


def test_status_sources(monkeypatch):
    update_profile(DEFAULT_PROFILE_ID, retention_days=30)
    status = get_backup_retention_status(DEFAULT_PROFILE_ID)
    assert (status["source"], status["label"]) == ("profile", "Kept for 30 days (set on this profile)")

    update_profile(DEFAULT_PROFILE_ID, retention_days=None)
    monkeypatch.setattr(backups, "BACKUP_RETENTION_DAYS_RAW", "45")
    status = get_backup_retention_status(DEFAULT_PROFILE_ID)
    assert (status["source"], status["label"], status["default_days"]) == (
        "environment", "Kept for 45 days (from BACKUP_RETENTION_DAYS)", 45,
    )

    monkeypatch.setattr(backups, "BACKUP_RETENTION_DAYS_RAW", "")
    status = get_backup_retention_status(DEFAULT_PROFILE_ID)
    assert (status["source"], status["label"], status["default_days"]) == (
        "none", "Kept until deleted (no retention set)", None,
    )


def test_retention_post_saves(client):
    response = client.post("/automation/retention", data={"retention_days": "30"})
    assert response.status_code == 200
    assert "now kept for 30 days" in response.text
    assert get_profile(DEFAULT_PROFILE_ID).retention_days == 30


def test_retention_post_empty_resets(client):
    client.post("/automation/retention", data={"retention_days": "30"})
    response = client.post("/automation/retention", data={"retention_days": ""})
    assert response.status_code == 200
    assert get_profile(DEFAULT_PROFILE_ID).retention_days is None
    assert "Retention reset to the default: Kept until deleted (no retention set)." in response.text


@pytest.mark.parametrize("raw", ["0", "abc"])
def test_retention_post_rejects_and_keeps_value(client, raw):
    client.post("/automation/retention", data={"retention_days": "30"})
    response = client.post("/automation/retention", data={"retention_days": raw})
    assert response.status_code == 400
    assert RETENTION_INPUT_ERROR in response.text
    assert f'value="{raw}"' in response.text
    assert get_profile(DEFAULT_PROFILE_ID).retention_days == 30
    assert 'id="retention-notice"' in response.text


def test_retention_section_shows_source(client, monkeypatch):
    monkeypatch.setattr(backups, "BACKUP_RETENTION_DAYS_RAW", "45")
    text = client.get("/automation").text
    assert "Kept for 45 days (from BACKUP_RETENTION_DAYS)" in text
    assert "Default: 45 days" in text
    assert 'id="retention-section"' in text


def test_retention_demo_redirects(client, monkeypatch):
    import app.routers.automation as automation

    monkeypatch.setattr(automation, "DEMO_MODE", True)
    response = client.post("/automation/retention", data={"retention_days": "5"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_legacy_retention_pills_gone(client):
    assert 'href="/backups#backup-retention-status"' not in client.get("/automation").text
