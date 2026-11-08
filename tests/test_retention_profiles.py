"""Backup retention is resolved per profile and scoped to the profile's account."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.backups as backups
import app.credentials as credentials
from app.backups import (
    get_backup_retention_days,
    get_backup_retention_status,
    purge_expired_backups,
)
from app.profiles import DEFAULT_PROFILE_ID, create_profile, update_profile

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)

_RESET_FIELDS = {"account_id": None, "retention_days": None}


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _exists(data_dir: Path, backup_id: int) -> bool:
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        return conn.execute("SELECT 1 FROM backups WHERE id = ?", (backup_id,)).fetchone() is not None


@pytest.fixture(autouse=True)
def profiles_env(data_dir: Path, monkeypatch):
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(backups, "BACKUP_RETENTION_DAYS_RAW", "30")
    update_profile(DEFAULT_PROFILE_ID, **_RESET_FIELDS)
    update_profile(DEFAULT_PROFILE_ID, account_id="acc-1")
    created: list[int] = []

    def _client(account_id: str, retention_days: int | None = None) -> int:
        profile = create_profile(f"Client {len(created)}")
        update_profile(profile.id, account_id=account_id, retention_days=retention_days)
        created.append(profile.id)
        return profile.id

    yield _client

    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM profiles WHERE id != ?", (DEFAULT_PROFILE_ID,))
        conn.commit()
    update_profile(DEFAULT_PROFILE_ID, **_RESET_FIELDS)


def test_default_uses_env_retention(make_backup, data_dir):
    old = make_backup(created_at=_ago(40), account_id="acc-1")
    assert purge_expired_backups(NOW) == 1
    assert not _exists(data_dir, old)


def test_profile_value_overrides_env(make_backup, data_dir):
    update_profile(DEFAULT_PROFILE_ID, retention_days=60)
    kept = make_backup(created_at=_ago(40), account_id="acc-1")
    assert purge_expired_backups(NOW) == 0
    assert _exists(data_dir, kept)
    assert get_backup_retention_days(DEFAULT_PROFILE_ID) == 60


def test_other_profile_null_keeps_forever(make_backup, data_dir, profiles_env):
    profiles_env("acc-B")
    kept = make_backup(created_at=_ago(400), account_id="acc-B")
    assert purge_expired_backups(NOW) == 0
    assert _exists(data_dir, kept)


def test_unmatched_account_uses_env(make_backup, data_dir):
    old = make_backup(created_at=_ago(40), account_id="acc-Z")
    assert purge_expired_backups(NOW) == 1
    assert not _exists(data_dir, old)
    assert get_backup_retention_days(None) == 30


def test_purge_scoped_to_account(make_backup, data_dir, profiles_env):
    update_profile(DEFAULT_PROFILE_ID, retention_days=100)
    profiles_env("acc-B", retention_days=1)
    default_backup = make_backup(created_at=_ago(2), account_id="acc-1")
    client_backup = make_backup(created_at=_ago(2), account_id="acc-B")
    assert purge_expired_backups(NOW) == 1
    assert _exists(data_dir, default_backup)
    assert not _exists(data_dir, client_backup)


def test_shared_account_purged_once_by_first_profile(make_backup, data_dir, profiles_env):
    update_profile(DEFAULT_PROFILE_ID, retention_days=100)
    profiles_env("acc-1", retention_days=1)
    kept = make_backup(created_at=_ago(2), account_id="acc-1")
    assert purge_expired_backups(NOW) == 0
    assert _exists(data_dir, kept)


def test_profile_without_account_owns_nothing(make_backup, data_dir, profiles_env):
    update_profile(DEFAULT_PROFILE_ID, account_id=None)
    profiles_env("acc-B", retention_days=1)
    unmatched = make_backup(created_at=_ago(40), account_id="")
    assert purge_expired_backups(NOW) == 1
    assert not _exists(data_dir, unmatched)


def test_status_for_profile_without_retention(profiles_env):
    other = profiles_env("acc-B")
    assert get_backup_retention_status(other)["enabled"] is False
    assert get_backup_retention_status(DEFAULT_PROFILE_ID)["days"] == 30


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_stored_value_is_not_set(make_backup, data_dir, profiles_env, bad):
    other = profiles_env("acc-B", retention_days=bad)
    kept = make_backup(created_at=_ago(40), account_id="acc-B")
    assert get_backup_retention_days(other) is None
    assert purge_expired_backups(NOW) == 0
    assert _exists(data_dir, kept)

    update_profile(DEFAULT_PROFILE_ID, retention_days=bad)
    assert get_backup_retention_days(DEFAULT_PROFILE_ID) == 30
    old = make_backup(created_at=_ago(40), account_id="acc-1")
    assert purge_expired_backups(NOW) == 1
    assert not _exists(data_dir, old)
