"""Schema migrations: fresh installs, legacy upgrades, idempotency and safety."""

from __future__ import annotations

import logging
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from app.migrations import LATEST_SCHEMA_VERSION, apply_migrations

# Today's (pre-profiles) DDL, copied from app/db.py at main.
LEGACY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        account_id TEXT NOT NULL,
        tunnel_id TEXT NOT NULL,
        tunnel_name TEXT,
        route_count INTEGER NOT NULL,
        filename TEXT NOT NULL UNIQUE,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS restores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_id INTEGER NOT NULL,
        restored_at TEXT NOT NULL,
        account_id TEXT NOT NULL,
        tunnel_id TEXT NOT NULL,
        FOREIGN KEY (backup_id) REFERENCES backups(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        account_id TEXT NOT NULL,
        tunnel_count INTEGER NOT NULL DEFAULT 0,
        backup_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        details TEXT
    )
    """,
)


def legacy_db(
    tmp_path: Path,
    settings: dict[str, str] | None = None,
    backups: list[tuple] | None = None,
    runs: int = 0,
) -> Path:
    """Create a pre-migration database. ``backups`` items are ``(filename, notes)``."""
    path = tmp_path / "app.db"
    with closing(sqlite3.connect(path)) as conn:
        for ddl in LEGACY_DDL:
            conn.execute(ddl)
        for key, value in (settings or {}).items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
        for filename, notes in backups or []:
            conn.execute(
                """
                INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes)
                VALUES ('2026-01-01T00:00:00+00:00', 'acc-1', 'tun-1', 'Home', 2, ?, ?)
                """,
                (filename, notes),
            )
        for i in range(runs):
            conn.execute(
                """
                INSERT INTO scheduled_runs (started_at, finished_at, status, account_id)
                VALUES (?, ?, 'success', 'acc-1')
                """,
                (f"2026-01-0{i + 1}T04:00:00+00:00", f"2026-01-0{i + 1}T04:00:05+00:00"),
            )
        conn.commit()
    return path


def migrate(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return apply_migrations(conn, path)


def query(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def user_version(path: Path) -> int:
    return int(query(path, "PRAGMA user_version")[0][0])


def test_fresh_db_gets_empty_default_profile(tmp_path):
    path = tmp_path / "app.db"
    assert migrate(path) == LATEST_SCHEMA_VERSION == 1
    assert user_version(path) == 1
    rows = query(path, "SELECT * FROM profiles")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == 1
    assert row["name"] == "Default"
    assert row["account_id"] is None
    assert row["api_token"] is None
    assert row["auto_backup_enabled"] == 0
    assert row["created_at"]


def test_legacy_settings_move_into_default(tmp_path):
    settings = {
        "account_id": "acc-1",
        "api_token": "enc:abc",
        "auto_backup_enabled": "1",
        "auto_backup_cron": "0 4 * * *",
        "auto_backup_tunnel_schedule": '{"mode":"custom"}',
        "auto_backup_last_run_at": "2026-01-02T04:00:00+00:00",
        "auto_backup_last_status": "success",
        "auto_backup_timezone": "Europe/Rome",
        "notification_message_templates": "{}",
    }
    path = legacy_db(tmp_path, settings=settings)
    assert migrate(path) == 1

    row = query(path, "SELECT * FROM profiles WHERE id = 1")[0]
    assert row["name"] == "Default"
    assert row["account_id"] == "acc-1"
    assert row["api_token"] == "enc:abc"
    assert row["auto_backup_enabled"] == 1
    assert row["auto_backup_cron"] == "0 4 * * *"
    assert row["tunnel_schedule"] == '{"mode":"custom"}'
    assert row["last_run_at"] == "2026-01-02T04:00:00+00:00"
    assert row["last_status"] == "success"
    assert row["retention_days"] is None

    remaining = {r["key"]: r["value"] for r in query(path, "SELECT key, value FROM settings")}
    assert remaining == {
        "auto_backup_timezone": "Europe/Rome",
        "notification_message_templates": "{}",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("TRUE", 1), (" yes ", 1), ("On", 1), ("0", 0), ("false", 0), ("", 0), ("enabled", 0)],
)
def test_auto_backup_enabled_truthy_set(tmp_path, raw, expected):
    path = legacy_db(tmp_path, settings={"auto_backup_enabled": raw})
    migrate(path)
    assert query(path, "SELECT auto_backup_enabled FROM profiles WHERE id = 1")[0][0] == expected


def test_plaintext_token_copied_verbatim(tmp_path):
    path = legacy_db(tmp_path, settings={"api_token": "plain-123"})
    migrate(path)
    assert query(path, "SELECT api_token FROM profiles WHERE id = 1")[0][0] == "plain-123"


def test_scheduled_runs_backfilled_to_default(tmp_path):
    path = legacy_db(tmp_path, runs=3)
    migrate(path)
    profile_ids = [r[0] for r in query(path, "SELECT profile_id FROM scheduled_runs")]
    assert profile_ids == [1, 1, 1]


def test_backup_origin_backfilled(tmp_path):
    path = legacy_db(
        tmp_path,
        backups=[
            ("auto.json", "Automatic backup triggered by schedule."),
            ("manual.json", "my note"),
            ("none.json", None),
        ],
    )
    migrate(path)
    origins = {r["filename"]: r["origin"] for r in query(path, "SELECT filename, origin FROM backups")}
    assert origins == {"auto.json": "automatic", "manual.json": "manual", "none.json": "manual"}


def test_backup_origin_defaults_to_manual_for_new_rows(tmp_path):
    path = tmp_path / "app.db"
    migrate(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes)
            VALUES ('2026-01-01T00:00:00+00:00', 'acc-1', 'tun-1', 'Home', 2, 'x.json', '')
            """
        )
        conn.commit()
    assert query(path, "SELECT origin FROM backups")[0][0] == "manual"
    indexes = [r[0] for r in query(path, "SELECT name FROM sqlite_master WHERE type = 'index'")]
    assert "idx_backups_account_id" in indexes


def test_second_run_is_noop(tmp_path):
    path = legacy_db(tmp_path, settings={"account_id": "acc-1"}, runs=1)
    assert migrate(path) == 1
    assert migrate(path) == 1
    assert len(query(path, "SELECT id FROM profiles")) == 1
    assert user_version(path) == 1


def test_migration_is_logged_once(tmp_path, caplog):
    path = legacy_db(tmp_path, settings={"account_id": "acc-1"}, runs=1)
    with caplog.at_level(logging.INFO, logger="app.migrations"):
        migrate(path)
    assert "Database schema migrated to version 1." in caplog.text
    assert "Saved a copy of the database before migrating" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.migrations"):
        migrate(path)
    assert "migrated to version" not in caplog.text


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import app.migrations as migrations

    path = legacy_db(tmp_path, settings={"account_id": "acc-1"}, runs=1)

    def boom(conn: sqlite3.Connection) -> None:
        migrations._migration_1(conn)
        raise RuntimeError("crash halfway")

    monkeypatch.setattr(migrations, "MIGRATIONS", [boom])
    with pytest.raises(RuntimeError, match="crash halfway"):
        migrate(path)

    assert user_version(path) == 0
    tables = [r[0] for r in query(path, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert "profiles" not in tables
    assert query(path, "SELECT value FROM settings WHERE key = 'account_id'")[0][0] == "acc-1"

    monkeypatch.undo()
    assert migrate(path) == 1
    assert len(query(path, "SELECT id FROM profiles")) == 1


def test_newer_schema_refused(tmp_path):
    path = legacy_db(tmp_path, settings={"account_id": "acc-1"})
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="newer than this app supports"):
        migrate(path)

    assert path.read_bytes() == before
    assert not (tmp_path / "app.db.pre-v1.bak").exists()


def test_pre_migration_backup_written(tmp_path):
    path = legacy_db(tmp_path, backups=[("a.json", "note")])
    migrate(path)
    bak = tmp_path / "app.db.pre-v1.bak"
    assert bak.exists()
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    tables = [r[0] for r in query(bak, "SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert "profiles" not in tables
    assert len(query(bak, "SELECT id FROM backups")) == 1
    assert int(query(bak, "PRAGMA user_version")[0][0]) == 0


def test_pre_migration_backup_overwrites_existing_file(tmp_path):
    path = legacy_db(tmp_path, settings={"account_id": "acc-1"})
    bak = tmp_path / "app.db.pre-v1.bak"
    bak.write_bytes(b"stale garbage")
    migrate(path)
    assert query(bak, "SELECT value FROM settings WHERE key = 'account_id'")[0][0] == "acc-1"


def test_no_backup_file_for_fresh_db(tmp_path):
    migrate(tmp_path / "app.db")
    assert not (tmp_path / "app.db.pre-v1.bak").exists()


def test_no_backup_file_for_empty_legacy_db(tmp_path):
    migrate(legacy_db(tmp_path))
    assert not (tmp_path / "app.db.pre-v1.bak").exists()


def test_destinations_table_exists_and_rejects_unknown_kind(tmp_path):
    path = tmp_path / "app.db"
    migrate(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO notification_destinations (profile_id, kind, target, created_at)
            VALUES (1, 'webhook', 'enc:x', '2026-01-01T00:00:00+00:00')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO notification_destinations (profile_id, kind, target, created_at)
                VALUES (1, 'email', 'enc:x', '2026-01-01T00:00:00+00:00')
                """
            )


def test_returns_early_when_another_process_migrated_first(tmp_path, caplog):
    path = tmp_path / "race.db"

    class RacingConn(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                with closing(sqlite3.connect(path)) as other:
                    apply_migrations(other)
            return super().execute(sql, *args)

    conn = sqlite3.connect(path, factory=RacingConn)
    try:
        with caplog.at_level(logging.INFO, logger="app.migrations"):
            assert apply_migrations(conn) == LATEST_SCHEMA_VERSION
        assert not conn.in_transaction
    finally:
        conn.close()
    # only the competing process logs a migration, not the one that lost the race
    assert caplog.text.count("Migrating database schema") == 1
