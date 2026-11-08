"""Encryption key resolution (environment, generated key file) and secret helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import app.encryption as encryption
from app.encryption import (
    EncryptionKeyError,
    decrypt_secret,
    encrypt_secret,
    encryption_key_source,
    get_fernet,
    read_secret,
)


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "encryption.key"
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", path)
    return path


def test_generates_key_file_with_mode_0600(key_file):
    assert decrypt_secret(encrypt_secret("abc")) == "abc"
    assert key_file.exists() and (key_file.stat().st_mode & 0o777) == 0o600
    Fernet(key_file.read_bytes().strip())  # valid key


def test_generated_key_is_reused(key_file):
    secret = encrypt_secret("abc")
    first = key_file.read_bytes()
    assert decrypt_secret(secret) == "abc" and key_file.read_bytes() == first


def test_env_key_wins_and_no_file_written(key_file, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", key)
    secret = encrypt_secret("abc")
    assert not key_file.exists()
    assert Fernet(key.encode()).decrypt(secret.removeprefix("enc:").encode()) == b"abc"


def test_invalid_env_key_raises(key_file, monkeypatch):
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "not-a-key")
    with pytest.raises(EncryptionKeyError, match="TOKEN_ENCRYPTION_KEY is invalid"):
        get_fernet()
    assert not key_file.exists()


def test_invalid_key_file_is_not_overwritten(key_file):
    key_file.write_bytes(b"garbage")
    with pytest.raises(EncryptionKeyError) as excinfo:
        get_fernet()
    assert str(key_file) in str(excinfo.value)
    assert key_file.read_bytes() == b"garbage"


def test_encryption_key_error_is_runtime_error():
    assert issubclass(EncryptionKeyError, RuntimeError)


def test_key_source(key_file, monkeypatch):
    assert encryption_key_source() == "missing"
    assert not key_file.exists()
    encrypt_secret("abc")
    assert encryption_key_source() == "file"
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert encryption_key_source() == "environment"
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "bad")
    assert encryption_key_source() == "invalid"


def test_key_source_invalid_file(key_file):
    key_file.write_bytes(b"garbage")
    assert encryption_key_source() == "invalid"


def test_decrypt_plaintext_passthrough(key_file):
    assert decrypt_secret("plain") == "plain"
    assert not key_file.exists()


def test_decrypt_failure_raises(key_file):
    with pytest.raises(EncryptionKeyError):
        decrypt_secret("enc:garbage")


def test_read_secret_logs_once(key_file, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    secret = encrypt_secret("abc")
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert read_secret(secret) is None and read_secret(secret) is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and warnings[0].exc_info is None and secret not in caplog.text


def test_read_secret_decrypts(key_file):
    secret = encrypt_secret("abc")
    assert read_secret(secret) == "abc"
    assert read_secret(secret) == "abc"


def test_read_secret_plaintext_passthrough(key_file):
    assert read_secret("plain") == "plain"
    assert not key_file.exists()


@pytest.fixture
def readonly_key_dir(tmp_path: Path, monkeypatch):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod does not restrict root")
    directory = tmp_path / "ro"
    directory.mkdir()
    directory.chmod(0o555)
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", directory / "encryption.key")
    yield directory
    directory.chmod(0o755)


def test_unwritable_key_path_raises_key_error(readonly_key_dir):
    with pytest.raises(EncryptionKeyError, match="cannot be created") as excinfo:
        encrypt_secret("abc")
    assert str(readonly_key_dir / "encryption.key") in str(excinfo.value)
    assert read_secret("enc:something") is None
    assert not (readonly_key_dir / "encryption.key").exists()
