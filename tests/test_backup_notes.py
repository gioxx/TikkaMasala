"""Snapshots, "Back up with a note" (/backups/new) and note validation."""

from __future__ import annotations

import asyncio
import json
from contextlib import closing

import pytest
from fastapi import HTTPException

import app.backups as backups
import app.credentials as credentials
import app.routers.backups as backups_router
import app.tunnels as tunnels
from app.config import BACKUP_DIR
from app.db import db

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"

LISTING = [
    {"id": T2, "name": "Lab", "status": "healthy"},
    {"id": T1, "name": "Home", "status": "healthy"},
]


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")


class _ConfigFake:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[tuple] = []
        self.error = error

    async def __call__(self, account_id, tunnel_id, api_token):
        self.calls.append((account_id, tunnel_id, api_token))
        if self.error is not None:
            raise self.error
        return (
            {"result": {"id": tunnel_id, "name": "Home"}},
            {
                "result": {
                    "config": {
                        "ingress": [
                            {"hostname": "a.example.com", "service": "http://10.0.0.1"},
                            {"service": "http_status:404"},
                        ]
                    }
                }
            },
        )


@pytest.fixture
def fake_config(monkeypatch):
    def _install(error: BaseException | None = None) -> _ConfigFake:
        fake = _ConfigFake(error)
        monkeypatch.setattr(backups, "fetch_tunnel_configuration", fake)
        return fake

    return _install


@pytest.fixture
def fake_list(monkeypatch):
    def _install(result=None, error: BaseException | None = None):
        async def _list(account_id, api_token):
            if error is not None:
                raise error
            return result if result is not None else LISTING

        monkeypatch.setattr(tunnels, "list_tunnels", _list)

    return _install


@pytest.fixture
def events(monkeypatch):
    recorded: list[str] = []

    def _queue(event, message, details=None, level="info", *, profile_id):
        recorded.append(event)

    monkeypatch.setattr(tunnels, "queue_notification", _queue)
    return recorded


@pytest.fixture
def cleanup_backups():
    """Remove backups created by the test (rows and files)."""
    before = _backup_ids()
    yield
    with closing(db()) as conn:
        rows = conn.execute("SELECT id, filename FROM backups").fetchall()
        for row in rows:
            if int(row["id"]) not in before:
                (BACKUP_DIR / str(row["filename"])).unlink(missing_ok=True)
                conn.execute("DELETE FROM backups WHERE id = ?", (int(row["id"]),))
        conn.commit()


def _backup_ids() -> set[int]:
    with closing(db()) as conn:
        return {int(row["id"]) for row in conn.execute("SELECT id FROM backups").fetchall()}


def _file_count() -> int:
    return sum(1 for item in BACKUP_DIR.iterdir() if item.is_file())


# --- clean_notes and the snapshot split -------------------------------------


def test_clean_notes():
    assert backups.NOTES_MAX_LENGTH == 500
    assert backups.clean_notes(" hi \r\n") == "hi"
    assert backups.clean_notes("a\r\nb") == "a\nb"
    assert backups.clean_notes("") is None
    assert backups.clean_notes("   ") is None
    assert backups.clean_notes(None) is None
    assert backups.clean_notes("x" * 500) == "x" * 500
    with pytest.raises(ValueError, match="^Notes can be at most 500 characters.$"):
        backups.clean_notes("x" * 501)


def test_take_snapshot_writes_nothing(fake_config):
    fake_config()
    ids, files = _backup_ids(), _file_count()
    snapshot = asyncio.run(backups.take_snapshot(GOOD, T1, "tok", "n"))
    assert _backup_ids() == ids
    assert _file_count() == files
    assert snapshot.payload["notes"] == "n"
    assert snapshot.payload["tunnel_id"] == T1
    assert snapshot.route_count == 2
    assert snapshot.tunnel_name == "Home"
    assert snapshot.filename.endswith(".json")


def test_create_backup_still_saves(fake_config, cleanup_backups):
    fake_config()
    record = asyncio.run(backups.create_backup(GOOD, T1, "tok", "n"))
    assert backups.get_backup(record.id).notes == "n"
    assert backups.get_backup(record.id).origin == "manual"
    path = BACKUP_DIR / record.filename
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["notes"] == "n"


# --- GET /backups/new -------------------------------------------------------


def test_new_page_lists_tunnels(client, configured_default, fake_list):
    fake_list()
    response = client.get(f"/backups/new?tunnel={T2}")
    assert response.status_code == 200
    text = response.text
    assert 'action="/backups/new"' in text
    assert f'<option value="{T2}" selected' in text
    assert f'<option value="{T1}" selected' not in text
    assert text.index(">Home<") < text.index(">Lab<")
    assert 'maxlength="500"' in text
    assert "Optional. Up to 500 characters. Saved inside the backup file." in text
    assert "Back up now" in text
    assert "test-token-0000-wxyz" not in text


def test_new_page_escapes_tunnel_names(client, configured_default, fake_list):
    fake_list([{"id": T1, "name": "<script>x</script>", "status": "healthy"}])
    text = client.get("/backups/new").text
    assert "<script>x</script>" not in text
    assert "&lt;script&gt;" in text


def test_new_page_without_credentials(client):
    response = client.get("/backups/new")
    assert response.status_code == 200
    assert 'Add them in <a href="/settings">Settings</a>' in response.text
    assert 'action="/backups/new"' not in response.text


def test_new_page_list_failure(client, configured_default, fake_list):
    fake_list(error=HTTPException(status_code=502, detail="boom"))
    response = client.get("/backups/new")
    assert "Could not load tunnels from Cloudflare: boom" in response.text
    assert 'action="/backups/new"' not in response.text


# --- POST /backups/new ------------------------------------------------------


def test_post_creates_backup_with_note(client, configured_default, fake_list, fake_config, events, cleanup_backups):
    fake_list()
    config = fake_config()
    response = client.post(
        "/backups/new", data={"tunnel_id": T1, "notes": "before upgrade"}, follow_redirects=False
    )
    assert response.status_code == 303
    new_ids = _backup_ids()
    backup_id = max(new_ids)
    assert response.headers["location"] == f"/backup/{backup_id}?created=1"
    assert backups.get_backup(backup_id).notes == "before upgrade"
    assert config.calls == [(GOOD, T1, "test-token-0000-wxyz")]
    assert events == ["manual_backup_success"]


def test_post_rejects_long_note(client, configured_default, fake_list, fake_config, events):
    fake_list()
    config = fake_config()
    ids = _backup_ids()
    note = "y" * 501
    response = client.post("/backups/new", data={"tunnel_id": T1, "notes": note})
    assert response.status_code == 400
    assert "Notes can be at most 500 characters." in response.text
    assert f">{note}</textarea>" in response.text
    assert _backup_ids() == ids
    assert config.calls == []
    assert events == []


def test_post_rejects_bad_tunnel_id(client, configured_default, fake_list, fake_config, events):
    fake_list()
    config = fake_config()
    response = client.post("/backups/new", data={"tunnel_id": "nope", "notes": "keep me"})
    assert response.status_code == 400
    assert "Unknown tunnel ID." in response.text
    assert ">keep me</textarea>" in response.text
    assert config.calls == []
    assert events == []


def test_post_without_credentials(client, fake_config, events):
    config = fake_config()
    response = client.post("/backups/new", data={"tunnel_id": T1, "notes": "keep me"})
    assert response.status_code == 400
    assert 'Add them in <a href="/settings">Settings</a>' in response.text
    assert config.calls == []


def test_post_cloudflare_failure_keeps_note(client, configured_default, fake_list, fake_config, events):
    fake_list()
    fake_config(error=HTTPException(status_code=502, detail="down"))
    ids = _backup_ids()
    response = client.post("/backups/new", data={"tunnel_id": T1, "notes": "my <note>"})
    assert response.status_code == 502
    assert "Backup failed: down" in response.text
    assert ">my &lt;note&gt;</textarea>" in response.text
    assert f'<option value="{T1}" selected' in response.text
    assert _backup_ids() == ids
    assert events == ["manual_backup_failed"]


def test_new_routes_redirect_in_demo(client, monkeypatch, fake_config):
    monkeypatch.setattr(backups_router, "DEMO_MODE", True)
    config = fake_config()
    get = client.get("/backups/new", follow_redirects=False)
    post = client.post("/backups/new", data={"tunnel_id": T1, "notes": ""}, follow_redirects=False)
    assert (get.status_code, get.headers["location"]) == (303, "/")
    assert (post.status_code, post.headers["location"]) == (303, "/")
    assert config.calls == []


def test_post_rejects_cross_site(client, configured_default, fake_config):
    config = fake_config()
    response = client.post(
        "/backups/new", data={"tunnel_id": T1, "notes": ""}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    assert config.calls == []


def test_tunnels_panel_links_note_page(client, configured_default):
    assert 'href="/backups/new"' in client.get("/").text


def test_tunnels_panel_hides_note_link_without_credentials(client):
    assert 'href="/backups/new"' not in client.get("/").text


def test_clean_notes_strips_controls_and_bidi():
    assert backups.clean_notes("a\x00b\u202ec\td\ne\x7f") == "abc\td\ne"
    assert backups.clean_notes("x\u2066y\u2069z\x1b") == "xyz"
    assert backups.clean_notes("\x00\u202e") is None


def test_clean_notes_length_checked_after_removal():
    assert backups.clean_notes("x" * 500 + "\x00\u202e") == "x" * 500
    with pytest.raises(ValueError):
        backups.clean_notes("x" * 501 + "\x00")


def test_note_kept_when_list_fails_on_post(client, configured_default, fake_config, fake_list, cleanup_backups):
    fake_config(error=HTTPException(status_code=502, detail="Cloudflare down"))
    fake_list(error=HTTPException(status_code=502, detail="list down"))
    response = client.post("/backups/new", data={"tunnel_id": T1, "notes": "keep this note"})
    assert response.status_code == 502
    assert "keep this note" in response.text
    assert f'<option value="{T1}" selected>' in response.text
    assert 'name="tunnel_id"' in response.text
    assert "list down" in response.text


def test_note_kept_without_credentials(client):
    response = client.post("/backups/new", data={"tunnel_id": T1, "notes": "keep me"})
    assert response.status_code == 400
    assert "<textarea" in response.text and "readonly" in response.text and "keep me" in response.text
    assert "Your note was not saved. Copy it before leaving this page." in response.text
    assert 'action="/backups/new"' not in response.text
