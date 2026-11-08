"""Credential validation against Cloudflare and the shared save path."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

import app.cloudflare as cloudflare
import app.credentials as credentials
import app.encryption as encryption
from app.cloudflare import CredentialsInvalid, check_credentials
from app.credentials import (
    ApiCredentials,
    clear_saved_credentials,
    describe_profile_credentials,
    find_profile_for_account,
    get_api_credentials,
    get_server_api_token,
    mask_secret,
    save_profile_credentials,
)
from app.profiles import DEFAULT_PROFILE_ID, create_profile, get_profile, update_profile

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def reset_profiles(data_dir: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", tmp_path / "encryption.key")
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)
    yield
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM profiles WHERE id != ?", (DEFAULT_PROFILE_ID,))
        conn.commit()


class _Recorder:
    """Async fake that records its calls and returns or raises a fixed outcome."""

    def __init__(self, result=None, exc: BaseException | None = None):
        self.calls: list[tuple] = []
        self.result = result
        self.exc = exc

    async def __call__(self, *args):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return self.result


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_api(monkeypatch):
    verify = _Recorder(result={"success": True})
    tunnels = _Recorder(result=[])
    monkeypatch.setattr(cloudflare, "verify_token", verify)
    monkeypatch.setattr(cloudflare, "list_tunnels", tunnels)
    return verify, tunnels


@pytest.fixture
def check(monkeypatch):
    recorder = _Recorder(result=0)
    monkeypatch.setattr(cloudflare, "check_credentials", recorder)
    return recorder


# --- check_credentials ---------------------------------------------------


def test_check_rejects_malformed_account_id_without_http(fake_api):
    verify, tunnels = fake_api
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(check_credentials("../user", "tok"))
    assert "32-character" in excinfo.value.message
    assert verify.calls == [] and tunnels.calls == []


def test_check_reports_bad_token(fake_api):
    verify, _ = fake_api
    verify.exc = HTTPException(400, "Token verification failed: Invalid API Token")
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(check_credentials(GOOD, "tok"))
    assert "Invalid API Token" in excinfo.value.message


def test_check_reports_tunnel_list_failure(fake_api):
    _, tunnels = fake_api
    tunnels.exc = HTTPException(400, "Authentication error (code 10000)")
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(check_credentials(GOOD, "tok"))
    assert excinfo.value.message.startswith("Token is valid but cannot list tunnels")
    assert "Authentication error" in excinfo.value.message


def test_check_network_error(fake_api):
    verify, _ = fake_api
    verify.exc = httpx.ConnectError("boom")
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(check_credentials(GOOD, "tok"))
    assert excinfo.value.message.startswith("Could not reach the Cloudflare API")
    assert "ConnectError" in excinfo.value.message


def test_check_non_json(fake_api):
    verify, _ = fake_api
    verify.exc = ValueError("Expecting value")
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(check_credentials(GOOD, "tok"))
    assert excinfo.value.message == "Unexpected response from the Cloudflare API."


def test_check_ok_returns_count(fake_api):
    verify, tunnels = fake_api
    tunnels.result = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert _run(check_credentials(GOOD, "tok")) == 3
    assert verify.calls == [("tok",)]
    assert tunnels.calls == [(GOOD, "tok")]


# --- save_profile_credentials ---------------------------------------------


def test_save_writes_both_after_validation(check):
    written = _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="tok-new-123456"))
    assert written is True
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id == GOOD
    assert profile.api_token.startswith("enc:")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "tok-new-123456"
    assert check.calls == [(GOOD, "tok-new-123456")]


def test_save_validation_failure_writes_nothing(check):
    check.exc = CredentialsInvalid("nope")
    with pytest.raises(CredentialsInvalid):
        _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="tok-new-123456"))
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None
    assert profile.api_token is None


def test_save_keeps_env_account_when_unchanged(check, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    assert _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="tok-new-123456")) is True
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None
    assert profile.api_token.startswith("enc:")
    assert check.calls == [(GOOD, "tok-new-123456")]


def test_save_token_only_uses_current_account(check):
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD)
    assert _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=None, api_token="t2-123456789")) is True
    assert check.calls == [(GOOD, "t2-123456789")]
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "t2-123456789"


def test_save_rejects_duplicate_account(check):
    client = create_profile("Client")
    update_profile(client.id, account_id=OTHER)
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=OTHER, api_token="x" * 20))
    assert "already used by profile 'Client'" in excinfo.value.message
    assert check.calls == []
    assert get_profile(DEFAULT_PROFILE_ID).account_id is None


def test_save_rejects_duplicate_env_account(check, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    client = create_profile("Client")
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(save_profile_credentials(client.id, account_id=GOOD.upper(), api_token="x" * 20))
    assert "already used by profile 'Default'" in excinfo.value.message
    assert check.calls == []
    assert get_profile(client.id).account_id is None


def test_save_unchanged_returns_false_without_api_call(check, configured_default):
    written = _run(
        save_profile_credentials(
            DEFAULT_PROFILE_ID,
            account_id=configured_default.account_id,
            api_token=configured_default.api_token,
        )
    )
    assert written is False
    assert check.calls == []


def test_save_empty_inputs_return_false(check, configured_default):
    assert _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id="  ", api_token=None)) is False
    assert check.calls == []


def test_save_missing_token(check):
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=GOOD, api_token=None))
    assert excinfo.value.message == "Both an Account ID and an API token are required."
    assert check.calls == []
    assert get_profile(DEFAULT_PROFILE_ID).account_id is None


def test_save_refused_in_demo(check, monkeypatch):
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="tok-new-123456"))
    assert "demo mode" in excinfo.value.message
    assert check.calls == []
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None and profile.api_token is None


def test_save_reports_encryption_key_error(check, monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(CredentialsInvalid) as excinfo:
        _run(save_profile_credentials(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="tok-new-123456"))
    assert "TOKEN_ENCRYPTION_KEY" in excinfo.value.message
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None and profile.api_token is None


# --- read helpers ----------------------------------------------------------


def test_mask_secret():
    assert mask_secret("abcdefghijklmnop1234") == "configured, ending in 1234"
    assert mask_secret("short") == "configured"
    assert mask_secret("") == ""


def test_describe_env_only(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-abcd-1234")
    view = describe_profile_credentials(DEFAULT_PROFILE_ID)
    assert view.profile_id == DEFAULT_PROFILE_ID
    assert view.name == "Default"
    assert view.account_id.value == GOOD
    assert view.account_id.source == "environment"
    assert view.account_id.env_value_present is True
    assert view.api_token.source == "environment"
    assert view.api_token.env_value_present is True
    assert view.api_token.value == "configured, ending in 1234"
    assert view.has_saved_values is False
    assert view.saved_token_unreadable is False


def test_describe_db_overrides_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    update_profile(DEFAULT_PROFILE_ID, account_id=OTHER)
    view = describe_profile_credentials(DEFAULT_PROFILE_ID)
    assert view.account_id.value == OTHER
    assert view.account_id.source == "database"
    assert view.account_id.env_value_present is True
    assert view.has_saved_values is True


def test_describe_unreadable_token(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-abcd-1234")
    update_profile(DEFAULT_PROFILE_ID, api_token="enc:garbage")
    view = describe_profile_credentials(DEFAULT_PROFILE_ID)
    assert view.saved_token_unreadable is True
    assert view.api_token.source == "environment"
    assert view.has_saved_values is True


def test_describe_never_contains_full_token(configured_default):
    view = describe_profile_credentials(DEFAULT_PROFILE_ID)
    assert "test-token-0000-wxyz" not in repr(view)
    assert view.api_token.value == "configured, ending in wxyz"
    assert view.api_token.source == "database"


def test_find_profile_for_account():
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD)
    client = create_profile("Client")
    update_profile(client.id, account_id=OTHER)
    assert find_profile_for_account(GOOD.upper()) == DEFAULT_PROFILE_ID
    assert find_profile_for_account(OTHER) == client.id
    assert find_profile_for_account("x" * 32) is None
    assert find_profile_for_account("") is None


def test_clear_saved_credentials_noop_in_demo(configured_default, monkeypatch):
    before = get_profile(DEFAULT_PROFILE_ID)
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    clear_saved_credentials(DEFAULT_PROFILE_ID)
    after = get_profile(DEFAULT_PROFILE_ID)
    assert (after.account_id, after.api_token) == (before.account_id, before.api_token)


def test_get_api_credentials_none_when_missing():
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD)
    assert get_api_credentials(DEFAULT_PROFILE_ID) is None


def test_get_api_credentials_effective_values(configured_default):
    assert get_api_credentials(DEFAULT_PROFILE_ID) == ApiCredentials(GOOD, "test-token-0000-wxyz")


def test_get_api_credentials_demo_uses_env_only(configured_default, monkeypatch):
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    assert get_api_credentials(DEFAULT_PROFILE_ID) is None
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", OTHER)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-abcd-1234")
    assert get_api_credentials(DEFAULT_PROFILE_ID) == ApiCredentials(OTHER, "env-token-abcd-1234")
