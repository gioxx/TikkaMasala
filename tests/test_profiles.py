"""Profile repository and profile-aware credential resolution."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

import app.credentials as credentials
import app.encryption as encryption
from app.credentials import (
    build_prefill_status,
    clear_saved_credentials,
    get_account_id_source,
    get_api_token_source,
    get_saved_account_id,
    get_saved_api_token,
    get_server_api_token,
    get_server_api_token_source,
    remember_account_id,
    remember_api_token,
)
from app.profiles import (
    DEFAULT_PROFILE_ID,
    create_profile,
    current_profile_id,
    get_profile,
    list_profiles,
    update_profile,
)


def _request(cookies: dict[str, str] | None = None) -> Request:
    headers = []
    if cookies:
        cookie = "; ".join(f"{key}={value}" for key, value in cookies.items())
        headers.append((b"cookie", cookie.encode("latin-1")))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


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


def test_default_profile_exists():
    assert get_profile(DEFAULT_PROFILE_ID).name == "Default"


def test_get_profile_missing_raises_lookup_error():
    with pytest.raises(LookupError):
        get_profile(9999)


def test_list_profiles_ordered_by_id():
    created = create_profile("Client")
    ids = [profile.id for profile in list_profiles()]
    assert ids == sorted(ids)
    assert ids[0] == DEFAULT_PROFILE_ID
    assert created.id in ids
    assert created.name == "Client"
    assert created.auto_backup_enabled is False


def test_current_profile_id_is_default():
    assert current_profile_id(_request()) == DEFAULT_PROFILE_ID


def test_update_profile_rejects_unknown_column():
    with pytest.raises(ValueError):
        update_profile(1, nope=1)


@pytest.mark.parametrize("column", ["id", "created_at", "name; DROP TABLE profiles"])
def test_update_profile_rejects_protected_and_injected_names(column):
    with pytest.raises(ValueError):
        update_profile(1, **{column: "x"})


def test_update_profile_missing_raises_lookup_error():
    with pytest.raises(LookupError):
        update_profile(9999, account_id="x")


def test_update_profile_stores_bool_and_clears_with_none():
    update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=True, retention_days=7)
    try:
        profile = get_profile(DEFAULT_PROFILE_ID)
        assert profile.auto_backup_enabled is True
        assert profile.retention_days == 7
    finally:
        update_profile(DEFAULT_PROFILE_ID, auto_backup_enabled=False, retention_days=None)
    assert get_profile(DEFAULT_PROFILE_ID).retention_days is None


def test_db_value_wins_over_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "env-acc")
    update_profile(DEFAULT_PROFILE_ID, account_id="db-acc")
    assert get_saved_account_id(DEFAULT_PROFILE_ID) == "db-acc"
    assert get_account_id_source(DEFAULT_PROFILE_ID) == "database"


def test_env_fallback_per_field(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "env-acc")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok")
    update_profile(DEFAULT_PROFILE_ID, account_id="db-acc", api_token=None)
    assert get_saved_account_id(DEFAULT_PROFILE_ID) == "db-acc"
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "env-tok"
    assert get_server_api_token_source(DEFAULT_PROFILE_ID) == "environment"


def test_env_only_install_uses_env_values(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "env-acc")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok")
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None and profile.api_token is None
    assert get_saved_account_id(DEFAULT_PROFILE_ID) == "env-acc"
    assert get_saved_api_token(_request(), DEFAULT_PROFILE_ID) == "env-tok"
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "env-tok"
    status = build_prefill_status(_request(), DEFAULT_PROFILE_ID)
    assert status["account_id_source"] == "environment"
    assert status["api_token_source"] == "environment"


def test_env_never_feeds_other_profiles(monkeypatch):
    p = create_profile("Client")
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "env-acc")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok")
    assert get_saved_account_id(p.id) == ""
    assert get_account_id_source(p.id) is None
    assert get_server_api_token(p.id) == ""
    assert get_server_api_token_source(p.id) is None
    assert get_saved_api_token(_request(), p.id) == ""
    assert get_api_token_source(_request(), p.id) is None


def test_cookie_token_still_wins_for_request_reads(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok")
    request = _request({credentials.API_TOKEN_COOKIE_NAME: "cookie-tok"})
    assert get_saved_api_token(request, DEFAULT_PROFILE_ID) == "cookie-tok"
    assert get_api_token_source(request, DEFAULT_PROFILE_ID) == "browser"


def test_plaintext_token_upgraded_on_read(monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    update_profile(DEFAULT_PROFILE_ID, api_token="plain-1")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "plain-1"
    assert get_profile(DEFAULT_PROFILE_ID).api_token.startswith("enc:")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "plain-1"


def test_plaintext_token_upgraded_on_request_read(monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    update_profile(DEFAULT_PROFILE_ID, api_token="plain-2")
    assert get_saved_api_token(_request(), DEFAULT_PROFILE_ID) == "plain-2"
    assert get_profile(DEFAULT_PROFILE_ID).api_token.startswith("enc:")


def test_plaintext_upgraded_with_generated_key():
    update_profile(DEFAULT_PROFILE_ID, api_token="plain-3")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "plain-3"
    assert get_profile(DEFAULT_PROFILE_ID).api_token.startswith("enc:")
    assert encryption.ENCRYPTION_KEY_FILE.exists()
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "plain-3"


def test_plaintext_row_with_malformed_env_key(monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "not-a-key")
    update_profile(DEFAULT_PROFILE_ID, api_token="plain-x")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "plain-x"
    assert get_profile(DEFAULT_PROFILE_ID).api_token == "plain-x"


def test_can_persist_api_token_without_env_key():
    assert build_prefill_status(_request(), DEFAULT_PROFILE_ID)["can_persist_api_token"] is True


def test_undecryptable_token_falls_back_without_crash(monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok")
    update_profile(DEFAULT_PROFILE_ID, api_token="enc:garbage")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "env-tok"
    assert get_server_api_token_source(DEFAULT_PROFILE_ID) == "environment"
    assert get_saved_api_token(_request(), DEFAULT_PROFILE_ID) == "env-tok"
    assert get_api_token_source(_request(), DEFAULT_PROFILE_ID) == "environment"
    assert get_profile(DEFAULT_PROFILE_ID).api_token == "enc:garbage"


def test_undecryptable_token_without_key_or_env_has_no_source():
    update_profile(DEFAULT_PROFILE_ID, api_token="enc:garbage")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == ""
    assert get_server_api_token_source(DEFAULT_PROFILE_ID) is None


def test_remember_writes_profile_row(monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    remember_account_id(DEFAULT_PROFILE_ID, " acc-9 ")
    remember_api_token(DEFAULT_PROFILE_ID, " tok-9 ")
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id == "acc-9"
    assert profile.api_token.startswith("enc:")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "tok-9"


def test_clear_saved_credentials():
    update_profile(DEFAULT_PROFILE_ID, account_id="acc", api_token="tok")
    clear_saved_credentials(DEFAULT_PROFILE_ID)
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None
    assert profile.api_token is None


def test_remember_is_noop_in_demo(monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    update_profile(DEFAULT_PROFILE_ID, account_id="keep-acc", api_token="keep-tok")
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    remember_account_id(DEFAULT_PROFILE_ID, "new-acc")
    remember_api_token(DEFAULT_PROFILE_ID, "new-tok")
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id == "keep-acc"
    assert profile.api_token == "keep-tok"


def test_server_token_reads_env_only_in_demo_mode(monkeypatch):
    update_profile(DEFAULT_PROFILE_ID, api_token="db-tok")
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    assert get_server_api_token(DEFAULT_PROFILE_ID) == ""
    assert get_server_api_token_source(DEFAULT_PROFILE_ID) is None
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok")
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "env-tok"
    assert get_server_api_token_source(DEFAULT_PROFILE_ID) == "environment"


def test_plaintext_row_with_unwritable_key_path(tmp_path, monkeypatch):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod does not restrict root")
    directory = tmp_path / "ro"
    directory.mkdir()
    directory.chmod(0o555)
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", directory / "encryption.key")
    try:
        update_profile(DEFAULT_PROFILE_ID, api_token="plain-ro")
        assert get_server_api_token(DEFAULT_PROFILE_ID) == "plain-ro"
        assert get_profile(DEFAULT_PROFILE_ID).api_token == "plain-ro"
    finally:
        directory.chmod(0o755)
