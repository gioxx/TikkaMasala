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
def calls(monkeypatch):
    recorded = {"put": [], "fetch": []}

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
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply"})
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
        data={"tunnel_id": "tunnel-1", "mode": "apply", "account_id": "evil-acc", "api_token": "evil"},
    )
    assert calls["put"][0][0] == "acc-1"
    assert calls["put"][0][2] == TOKEN


def test_restore_without_matching_profile(client, make_backup, calls):
    backup_id = make_backup(account_id="acc-other")
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply"})
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
    response = client.post(f"/backup/{backup_id}/restore", data={"tunnel_id": "tunnel-1", "mode": "apply"})
    assert "cf_api_token" not in response.headers.get("set-cookie", "")
