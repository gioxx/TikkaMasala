"""Restore uses the saved credentials of the profile that owns the backup's account."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import app.credentials as credentials
import app.encryption as encryption
from app.credentials import remember_api_token
from app.profiles import DEFAULT_PROFILE_ID, update_profile

TOKEN = "saved-token-1234567890"


@pytest.fixture(autouse=True)
def default_profile(data_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", tmp_path / "encryption.key")
    update_profile(DEFAULT_PROFILE_ID, account_id="acc-1", api_token=None)
    remember_api_token(DEFAULT_PROFILE_ID, TOKEN)
    yield
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)


@pytest.fixture
def calls(monkeypatch, live_snapshots):
    recorded = {"put": [], "fetch": [], "snapshot": live_snapshots["calls"]}

    async def fake_put(*args):
        recorded["put"].append(args)
        return {}

    async def fake_fetch(*args):
        recorded["fetch"].append(args)
        return 200, {"result": {"config": {"ingress": [{"service": "http_status:404"}]}}}

    monkeypatch.setattr("app.routers.backups.cloudflare_put", fake_put)
    monkeypatch.setattr("app.routers.backups.fetch_tunnel_configuration", fake_fetch)
    return recorded


def test_restore_uses_saved_credentials(client, make_backup, calls):
    backup_id = make_backup()
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"})
    assert len(calls["put"]) == 1
    account, path, token, body = calls["put"][0]
    assert (account, path, token) == ("acc-1", "cfd_tunnel/tunnel-1/configurations", TOKEN)
    assert "config" in body
    assert "Backup restored successfully." in response.text


def test_restore_preview_uses_saved_credentials(client, make_backup, calls):
    backup_id = make_backup()
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "preview"})
    assert calls["fetch"] == [("acc-1", "tunnel-1", TOKEN)]
    assert calls["put"] == []
    assert "Preview only" in response.text


def test_restore_ignores_posted_credentials(client, make_backup, calls):
    backup_id = make_backup()
    client.post(
        f"/backup/{backup_id}/restore",
        data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace", "account_id": "evil-acc", "api_token": "evil"},
    )
    assert calls["put"][0][0] == "acc-1"
    assert calls["put"][0][2] == TOKEN


def test_restore_without_matching_profile(client, make_backup, calls):
    backup_id = make_backup(account_id="acc-other")
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"})
    assert "No profile has credentials for account acc-other" in response.text
    assert calls["put"] == []
    assert calls["fetch"] == []


def test_backup_page_has_no_credential_inputs(client, make_backup):
    backup_id = make_backup()
    text = client.get(f"/backup/{backup_id}").text
    assert 'name="api_token"' not in text
    assert 'name="account_id"' not in text
    assert 'name="tunnel_id"' in text


def test_restore_does_not_set_cookie(client, make_backup, calls):
    backup_id = make_backup()
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"})
    assert "cf_api_token" not in response.headers.get("set-cookie", "")


# --- cross-site guard on every unsafe method ------------------------------------------

CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}


@pytest.fixture
def run_calls(monkeypatch):
    import app.routers.automation as automation

    recorded: list[tuple] = []

    async def fake_run(*args, **kwargs):
        recorded.append((args, kwargs))
        return {"status": "success", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

    monkeypatch.setattr(automation, "DEMO_MODE", False)
    monkeypatch.setattr(automation, "run_auto_backup_job", fake_run)
    return recorded


def test_cross_site_restore_rejected(client, make_backup, calls):
    backup_id = make_backup()
    response = client.post(
        f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"}, headers=CROSS_SITE
    )
    assert response.status_code == 403
    assert calls["put"] == []
    assert calls["fetch"] == []


def test_cross_site_automation_post_rejected(client, run_calls):
    response = client.post("/automation/run", headers=CROSS_SITE)
    assert response.status_code == 403
    assert run_calls == []


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}])
def test_same_origin_or_headerless_posts_allowed(client, make_backup, calls, run_calls, headers):
    backup_id = make_backup()
    response = client.post(
        f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"}, headers=headers
    )
    assert response.status_code == 200
    assert len(calls["put"]) == 1
    response = client.post("/automation/run", headers=headers)
    assert response.status_code == 200
    assert len(run_calls) == 1


def test_cross_site_get_and_healthz_unaffected(client):
    assert client.get("/healthz", headers=CROSS_SITE).status_code == 200
    assert client.get("/settings", headers=CROSS_SITE).status_code == 200


# --- restore target helper -------------------------------------------------------------


def test_resolve_restore_target_with_profile():
    target = credentials.resolve_restore_target("ACC-1")
    assert target.profile_id == DEFAULT_PROFILE_ID
    assert target.profile_name == "Default"
    assert target.credentials is not None and target.credentials.api_token == TOKEN
    assert target.can_restore


def test_resolve_restore_target_without_profile():
    target = credentials.resolve_restore_target("acc-zzz")
    assert target.profile_id is None
    assert target.credentials is None
    assert not target.can_restore


def test_resolve_restore_target_profile_without_token():
    update_profile(DEFAULT_PROFILE_ID, api_token=None)
    target = credentials.resolve_restore_target("acc-1")
    assert target.profile_id == DEFAULT_PROFILE_ID
    assert target.credentials is None
    assert not target.can_restore


def test_preview_without_profile(client, make_backup, calls):
    backup_id = make_backup(account_id="acc-zzz")
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "preview"})
    assert "No profile has credentials for account acc-zzz" in response.text
    assert calls["fetch"] == []


def test_detail_without_profile_has_no_restore_form(client, make_backup):
    backup_id = make_backup(account_id="acc-zzz")
    text = client.get(f"/backup/{backup_id}").text
    assert f'action="/backup/{backup_id}/restore"' not in text
    assert 'href="/settings"' in text
    assert "Other account" in text


def test_apply_records_restore(client, make_backup, calls):
    from app.backups import get_restore_history

    backup_id = make_backup()
    client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"})
    record = get_restore_history(backup_id)[0]
    assert record.account_id == "acc-1"
    assert record.tunnel_id == "tunnel-1"


def test_failed_restore_notifies_with_profile_account(client, make_backup, monkeypatch, live_snapshots):
    from fastapi import HTTPException

    async def failing_put(*args):
        raise HTTPException(status_code=502, detail="nope")

    sent: list[tuple] = []
    monkeypatch.setattr("app.routers.backups.cloudflare_put", failing_put)
    monkeypatch.setattr("app.routers.backups.queue_notification", lambda *a, **k: sent.append((a, k)))
    backup_id = make_backup()
    client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"})
    assert sent[0][0][0] == "restore_failed"
    assert sent[0][0][2]["account_id"] == "acc-1"
    assert sent[0][0][2]["error"] == "nope"


# --- restore safety: explicit confirmation and an automatic snapshot first (WCAG 3.3.4) ---


def test_restore_form_requires_confirmation(client, make_backup):
    backup_id = make_backup(tunnel_name="Home")
    text = client.get(f"/backup/{backup_id}").text
    checkbox = text.split('id="restore-confirm"', 1)[1].split(">", 1)[0]
    assert "required" in checkbox
    assert 'name="confirm" value="replace"' in text
    assert "I understand this replaces the live configuration of tunnel Home (tunnel-1)." in text
    # Preview needs no confirmation.
    assert '<button type="submit" name="mode" value="preview" formnovalidate>' in text


def test_apply_without_confirmation_changes_nothing(client, make_backup, calls):
    from app.backups import get_restore_history

    backup_id = make_backup()
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply"})
    assert response.status_code == 400
    assert "Tick the confirmation to restore" in response.text
    assert "<title>Error: Backup #" in response.text
    checkbox = response.text.split('id="restore-confirm"', 1)[1].split(">", 1)[0]
    assert 'aria-invalid="true" aria-describedby="restore-notice"' in checkbox
    assert calls["put"] == [] and calls["snapshot"] == []
    assert get_restore_history(backup_id) == []


def test_apply_saves_the_live_configuration_first(client, make_backup, calls, monkeypatch):
    import app.routers.backups as backups_router
    from app.backups import get_backup

    order: list[str] = []
    original_put = backups_router.cloudflare_put

    async def put_after_snapshot(*args):
        order.append("put" if calls["snapshot"] else "put-before-snapshot")
        return await original_put(*args)

    monkeypatch.setattr(backups_router, "cloudflare_put", put_after_snapshot)
    backup_id = make_backup()
    response = client.post(
        f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"}
    )
    assert response.status_code == 200
    assert calls["snapshot"] == [("acc-1", "tunnel-1", TOKEN)]
    assert order == ["put"]
    snapshot_id = int(response.text.split("saved first as <a href=\"/backup/", 1)[1].split('"', 1)[0])
    snapshot = get_backup(snapshot_id)
    assert snapshot.origin == "manual"
    assert snapshot.notes == f"Automatic snapshot before restoring backup #{backup_id}"
    assert snapshot.tunnel_id == "tunnel-1" and snapshot.account_id == "acc-1"
    assert f'<a href="/backup/{snapshot_id}">backup #{snapshot_id}</a>: restore that one to undo.' in response.text


def test_failed_snapshot_aborts_the_restore(client, make_backup, calls, live_snapshots):
    from fastapi import HTTPException

    from app.backups import get_restore_history

    live_snapshots["error"] = HTTPException(status_code=502, detail="Cloudflare is down")
    backup_id = make_backup()
    response = client.post(
        f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply", "confirm": "replace"}
    )
    assert calls["put"] == []
    assert get_restore_history(backup_id) == []
    assert (
        "Could not save a snapshot of the live configuration first, so nothing was restored: Cloudflare is down"
        in response.text
    )
    assert 'role="alert"' in response.text
