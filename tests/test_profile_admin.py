"""Profile lifecycle: names, create with credentials, rename, removal, unscheduling."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest
from fastapi import HTTPException

from app import cloudflare, credentials
from app.archive import ArchiveQuery, build_archive_page
from app.backups import delete_backups_for_account, get_backup, purge_expired_backups
from app.cloudflare import CredentialsInvalid
from app.credentials import create_profile_with_credentials, get_api_credentials
from app.profile_admin import (
    REMOVE_BUSY_MESSAGE,
    SHARED_BACKUPS_MESSAGE,
    BackupCountChanged,
    ProfileBusy,
    remove_profile,
    removal_preview,
)
from app.profiles import (
    DEFAULT_NOT_REMOVABLE,
    delete_profile_rows,
    get_profile,
    list_profiles,
    normalize_profile_name,
    profile_exists,
    rename_profile,
    update_profile,
)
from app.scheduler import (
    _profile_run_locks,
    auto_backup_job_id,
    auto_backup_scheduler,
    configure_all_auto_backup_jobs,
    profile_run_lock,
    run_auto_backup_job,
)

GOOD = "0123456789abcdef0123456789abcdef"
CLIENT = "fedcba9876543210fedcba9876543210"
CLIENT_TOKEN = "client-token-0000-abcd"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
NEW_TOKEN = "tok-new-0000-zzzz"


class _Recorder:
    def __init__(self, exc: BaseException | None = None):
        self.calls: list[tuple] = []
        self.exc = exc

    async def __call__(self, *args):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return 0


@pytest.fixture
def fake_check(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(cloudflare, "check_credentials", recorder)
    return recorder


@pytest.fixture
def cleanup_new_profiles(data_dir):
    yield
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM profiles WHERE id != 1")
        conn.commit()


def _raw_profile_token(data_dir, profile_id: int) -> str:
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        return conn.execute("SELECT api_token FROM profiles WHERE id = ?", (profile_id,)).fetchone()[0]


def _count(data_dir, sql: str, *args) -> int:
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        return int(conn.execute(sql, args).fetchone()[0])


@pytest.mark.parametrize("raw,expected", [(" Client ", "Client"), ("x" * 40, "x" * 40)])
def test_normalize_profile_name_ok(raw, expected):
    assert normalize_profile_name(raw) == expected


@pytest.mark.parametrize(
    "raw,message",
    [
        ("", "Enter a profile name."),
        ("   ", "Enter a profile name."),
        ("x" * 41, "at most 40"),
        ("a\x07b", "control characters"),
        ("a‮b", "control characters"),
        ("OTHER accounts", "reserved"),
        ("default", "already exists"),
    ],
)
def test_normalize_profile_name_rejects(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_profile_name(raw)


def test_normalize_profile_name_excludes_itself():
    assert normalize_profile_name("DEFAULT", exclude_id=1) == "DEFAULT"


def test_create_validates_before_insert(monkeypatch, cleanup_new_profiles):
    monkeypatch.setattr(cloudflare, "check_credentials", _Recorder(CredentialsInvalid("Cloudflare said no.")))
    with pytest.raises(CredentialsInvalid, match="Cloudflare said no."):
        asyncio.run(create_profile_with_credentials("New", CLIENT, NEW_TOKEN))
    assert "New" not in [p.name for p in list_profiles()]


def test_create_rejects_bad_name_and_missing_values(fake_check, cleanup_new_profiles):
    with pytest.raises(CredentialsInvalid, match="reserved"):
        asyncio.run(create_profile_with_credentials("Other accounts", CLIENT, NEW_TOKEN))
    with pytest.raises(CredentialsInvalid, match="Both an Account ID and an API token are required."):
        asyncio.run(create_profile_with_credentials("New", CLIENT, "  "))
    assert fake_check.calls == []
    assert [p.id for p in list_profiles()] == [1]


def test_create_rejects_duplicate_account(configured_default, fake_check, cleanup_new_profiles):
    with pytest.raises(CredentialsInvalid, match="already used by profile 'Default'"):
        asyncio.run(create_profile_with_credentials("New", GOOD.upper(), NEW_TOKEN))
    assert fake_check.calls == []
    assert "New" not in [p.name for p in list_profiles()]


def test_create_encrypts_token(fake_check, data_dir, cleanup_new_profiles, caplog):
    caplog.set_level("INFO")
    new = asyncio.run(create_profile_with_credentials(" New ", CLIENT, NEW_TOKEN))
    assert new.name == "New"
    assert new.account_id == CLIENT
    assert new.auto_backup_enabled is False
    raw = _raw_profile_token(data_dir, new.id)
    assert raw.startswith("enc:")
    assert NEW_TOKEN not in raw
    assert get_api_credentials(new.id).api_token == NEW_TOKEN
    assert fake_check.calls == [(CLIENT, NEW_TOKEN)]
    assert NEW_TOKEN not in caplog.text
    assert f"Profile {new.id} created (account_id={CLIENT})." in caplog.text


def test_create_refused_in_demo(monkeypatch, fake_check):
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    with pytest.raises(CredentialsInvalid, match="Profiles cannot be added in demo mode."):
        asyncio.run(create_profile_with_credentials("New", CLIENT, NEW_TOKEN))
    assert fake_check.calls == []


def test_save_credentials_still_rejects_duplicate(second_profile, configured_default, fake_check):
    with pytest.raises(CredentialsInvalid, match="already used by profile 'Default'"):
        asyncio.run(credentials.save_profile_credentials(second_profile.id, account_id=GOOD, api_token=None))


def test_rename_rules(second_profile):
    assert rename_profile(1, " Home ").name == "Home"
    assert get_profile(1).name == "Home"
    assert rename_profile(1, "Default").name == "Default"
    with pytest.raises(ValueError, match="already exists"):
        rename_profile(second_profile.id, "default")
    assert rename_profile(second_profile.id, "CLIENT").name == "CLIENT"
    with pytest.raises(LookupError):
        rename_profile(9999, "Ghost")


def test_delete_profile_rows_removes_destinations_and_runs(second_profile, data_dir):
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        for profile_id in (1, second_profile.id):
            conn.execute(
                "INSERT INTO notification_destinations (profile_id, kind, label, target, enabled, created_at)"
                " VALUES (?, 'webhook', 'Hook', 'enc:x', 1, '2026-01-01T00:00:00+00:00')",
                (profile_id,),
            )
            conn.execute(
                "INSERT INTO scheduled_runs (profile_id, started_at, status, account_id) VALUES (?, ?, 'success', ?)",
                (profile_id, "2026-01-01T00:00:00+00:00", "acc"),
            )
        conn.commit()
    try:
        delete_profile_rows(second_profile.id)
        assert not profile_exists(second_profile.id)
        for table in ("notification_destinations", "scheduled_runs"):
            assert _count(data_dir, f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?", second_profile.id) == 0
            assert _count(data_dir, f"SELECT COUNT(*) FROM {table} WHERE profile_id = 1") == 1
        with pytest.raises(LookupError):
            delete_profile_rows(second_profile.id)
        with pytest.raises(ValueError, match="first profile cannot be removed"):
            delete_profile_rows(1)
    finally:
        with closing(sqlite3.connect(data_dir / "app.db")) as conn:
            conn.execute("DELETE FROM notification_destinations WHERE profile_id = 1")
            conn.execute("DELETE FROM scheduled_runs WHERE profile_id = 1")
            conn.commit()


def test_default_cannot_be_removed():
    with pytest.raises(ValueError) as excinfo:
        remove_profile(1, delete_backups=False)
    assert str(excinfo.value) == DEFAULT_NOT_REMOVABLE
    with pytest.raises(ValueError):
        removal_preview(1)
    assert profile_exists(1)


def test_missing_profile_lookup_error():
    with pytest.raises(LookupError):
        removal_preview(9999)
    with pytest.raises(LookupError):
        remove_profile(9999, delete_backups=False)


def test_removal_preview_counts(second_profile, make_backup, configured_default):
    make_backup(T1, account_id=CLIENT)
    make_backup(T2, account_id=CLIENT.upper())
    make_backup(T1, account_id=GOOD)
    preview = removal_preview(second_profile.id)
    assert preview.profile_id == second_profile.id
    assert preview.name == "Client"
    assert preview.account_id == CLIENT
    assert preview.backup_count == 2
    assert preview.backups_shared is False


def test_remove_keeps_backups_by_default(second_profile, make_backup):
    backup_id = make_backup(T1, account_id=CLIENT)
    assert remove_profile(second_profile.id, delete_backups=False) == 0
    assert not profile_exists(second_profile.id)
    assert get_backup(backup_id).id == backup_id
    page = build_archive_page(1, ArchiveQuery(scope="other"))
    assert backup_id in [b.id for b in page.backups]


def test_removed_profile_backups_follow_other_retention(second_profile, make_backup, monkeypatch):
    import app.backups

    update_profile(second_profile.id, retention_days=3650)
    monkeypatch.setattr(app.backups, "BACKUP_RETENTION_DAYS_RAW", "1")
    backup_id = make_backup(T1, account_id=CLIENT, created_at="2020-01-01T00:00:00+00:00")
    purge_expired_backups()
    assert get_backup(backup_id).id == backup_id

    remove_profile(second_profile.id, delete_backups=False)
    purge_expired_backups()
    with pytest.raises(HTTPException):
        get_backup(backup_id)


def test_remove_deletes_backups_when_asked(second_profile, make_backup, data_dir):
    first = make_backup(T1, account_id=CLIENT)
    second = make_backup(T2, account_id=CLIENT)
    kept = make_backup(T1, account_id=GOOD)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute(
            "INSERT INTO restores (backup_id, restored_at, account_id, tunnel_id)"
            " VALUES (?, '2026-01-02T00:00:00+00:00', ?, ?)",
            (first, CLIENT, T1),
        )
        conn.commit()
    filenames = {bid: get_backup(bid).filename for bid in (first, second, kept)}

    assert remove_profile(second_profile.id, delete_backups=True, expected_backup_count=2) == 2

    assert not profile_exists(second_profile.id)
    for bid in (first, second):
        assert not (data_dir / "backups" / filenames[bid]).exists()
        assert _count(data_dir, "SELECT COUNT(*) FROM backups WHERE id = ?", bid) == 0
    assert _count(data_dir, "SELECT COUNT(*) FROM restores WHERE backup_id = ?", first) == 0
    assert get_backup(kept).id == kept
    assert (data_dir / "backups" / filenames[kept]).exists()


def test_remove_count_mismatch_refused(second_profile, make_backup):
    first = make_backup(T1, account_id=CLIENT)
    second = make_backup(T2, account_id=CLIENT)
    with pytest.raises(BackupCountChanged) as excinfo:
        remove_profile(second_profile.id, delete_backups=True, expected_backup_count=1)
    assert str(excinfo.value) == "The number of backups changed since this page was shown. Check again."
    assert isinstance(excinfo.value, ValueError)
    assert profile_exists(second_profile.id)
    assert get_backup(first) and get_backup(second)


def test_remove_refuses_shared_backups(second_profile, make_backup, data_dir):
    third = None
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        cursor = conn.execute(
            "INSERT INTO profiles (name, account_id, created_at) VALUES ('Twin', ?, '2026-01-01T00:00:00+00:00')",
            (CLIENT.upper(),),
        )
        conn.commit()
        third = int(cursor.lastrowid)
    backup_id = make_backup(T1, account_id=CLIENT)
    preview = removal_preview(second_profile.id)
    assert preview.backups_shared is True
    assert preview.backup_count == 1
    with pytest.raises(ValueError, match=SHARED_BACKUPS_MESSAGE):
        remove_profile(second_profile.id, delete_backups=True, expected_backup_count=1)
    assert profile_exists(second_profile.id)
    # Keeping them is fine: the twin profile still covers the account.
    assert remove_profile(second_profile.id, delete_backups=False) == 0
    assert get_backup(backup_id).id == backup_id
    assert profile_exists(third)


def test_remove_profile_busy(second_profile):
    async def scenario():
        async with profile_run_lock(second_profile.id):
            with pytest.raises(ProfileBusy, match=REMOVE_BUSY_MESSAGE):
                remove_profile(second_profile.id, delete_backups=False)

    asyncio.run(scenario())
    assert profile_exists(second_profile.id)


def test_remove_unschedules_job(second_profile):
    update_profile(second_profile.id, auto_backup_enabled=True)
    configure_all_auto_backup_jobs()
    job_id = auto_backup_job_id(second_profile.id)
    assert auto_backup_scheduler.get_job(job_id) is not None
    profile_run_lock(second_profile.id)

    remove_profile(second_profile.id, delete_backups=False)

    assert auto_backup_scheduler.get_job(job_id) is None
    assert second_profile.id not in _profile_run_locks


def test_run_for_removed_profile_returns_early(second_profile, data_dir, caplog):
    caplog.set_level("INFO")
    profile_id = second_profile.id
    remove_profile(profile_id, delete_backups=False)
    result = asyncio.run(run_auto_backup_job(profile_id))
    assert result == {"status": "skipped", "tunnel_count": 0, "backup_count": 0, "error_count": 0}
    assert _count(data_dir, "SELECT COUNT(*) FROM scheduled_runs WHERE profile_id = ?", profile_id) == 0
    assert f"Automatic backup run of profile {profile_id} skipped: the profile no longer exists." in caplog.text


def test_run_rechecks_profile_after_lock(second_profile, data_dir, monkeypatch):
    import app.scheduler

    profile_id = second_profile.id
    calls = {"n": 0}
    real = app.scheduler.profile_exists

    def exists_then_gone(pid):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(pid)
        return False

    monkeypatch.setattr(app.scheduler, "profile_exists", exists_then_gone)
    result = asyncio.run(run_auto_backup_job(profile_id))
    assert result["status"] == "skipped"
    assert calls["n"] == 2
    assert _count(data_dir, "SELECT COUNT(*) FROM scheduled_runs WHERE profile_id = ?", profile_id) == 0


def test_delete_backups_for_account_case_insensitive(make_backup, data_dir):
    first = make_backup(T1, account_id="AbC")
    second = make_backup(T2, account_id="abc")
    kept = make_backup(T1, account_id=OTHER)
    filename = get_backup(first).filename
    (data_dir / "backups" / get_backup(second).filename).unlink()  # a missing file is fine
    assert delete_backups_for_account("abc") == 2
    assert _count(data_dir, "SELECT COUNT(*) FROM backups WHERE id IN (?, ?)", first, second) == 0
    assert not (data_dir / "backups" / filename).exists()
    assert get_backup(kept).id == kept


@pytest.mark.parametrize("raw", ["a​b", "﻿Name", "a­b"])
def test_normalize_profile_name_rejects_format_characters(raw):
    with pytest.raises(ValueError, match="control characters"):
        normalize_profile_name(raw)


def test_remove_backup_files_skips_paths_outside_backup_dir(data_dir, caplog):
    from app.backups import remove_backup_files

    outside = data_dir / "outside-victim.json"
    outside.write_text("{}", encoding="utf-8")
    inside = data_dir / "backups" / "inside-victim.json"
    inside.write_text("{}", encoding="utf-8")
    try:
        caplog.set_level("WARNING")
        remove_backup_files(["../outside-victim.json", "inside-victim.json", "bad\x00name.json"])
        assert outside.exists()
        assert not inside.exists()
        assert "outside the backup directory" in caplog.text
    finally:
        outside.unlink(missing_ok=True)
        inside.unlink(missing_ok=True)


def test_remove_profile_with_traversal_filename_row(second_profile, data_dir):
    outside = data_dir / "outside-row.json"
    outside.write_text("{}", encoding="utf-8")
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute(
            "INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes, origin)"
            " VALUES ('2026-01-01T00:00:00+00:00', ?, ?, 'Home', 1, '../outside-row.json', '', 'manual')",
            (CLIENT, T1),
        )
        conn.commit()
    try:
        assert remove_profile(second_profile.id, delete_backups=True, expected_backup_count=1) == 1
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_remove_survives_scheduler_cleanup_failure(second_profile, make_backup, data_dir, monkeypatch, caplog):
    import app.profile_admin

    backup_id = make_backup(T1, account_id=CLIENT)
    filename = get_backup(backup_id).filename

    def boom(*args, **kwargs):
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(app.profile_admin, "forget_profile_runtime", boom)
    monkeypatch.setattr(app.profile_admin, "configure_all_auto_backup_jobs", boom)
    caplog.set_level("ERROR")

    assert remove_profile(second_profile.id, delete_backups=True, expected_backup_count=1) == 1

    assert not profile_exists(second_profile.id)
    assert not (data_dir / "backups" / filename).exists()
    assert "scheduler" in caplog.text.lower()


def test_remove_rolls_back_when_profile_delete_fails(second_profile, make_backup, data_dir, monkeypatch):
    import app.profile_admin

    first = make_backup(T1, account_id=CLIENT)
    second = make_backup(T2, account_id=CLIENT)
    filenames = [get_backup(bid).filename for bid in (first, second)]

    def failing_delete(conn, profile_id):
        # Backup rows are already deleted inside the open transaction at this point.
        assert conn.execute("SELECT COUNT(*) FROM backups WHERE id IN (?, ?)", (first, second)).fetchone()[0] == 0
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(app.profile_admin, "delete_profile_rows_in", failing_delete)

    with pytest.raises(RuntimeError, match="disk on fire"):
        remove_profile(second_profile.id, delete_backups=True, expected_backup_count=2)

    assert profile_exists(second_profile.id)
    assert get_backup(first).id == first and get_backup(second).id == second
    for filename in filenames:
        assert (data_dir / "backups" / filename).exists()
