"""Encryption key resolution and secret encryption at rest.

The key comes from ``TOKEN_ENCRYPTION_KEY`` when set; otherwise it is read from
``ENCRYPTION_KEY_FILE`` (``DATA_DIR/encryption.key``), which is generated with
mode 0600 the first time a key is needed. An existing key file with invalid
content is never overwritten: doing so would orphan every encrypted value.
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import ENCRYPTED_VALUE_PREFIX, ENCRYPTION_KEY_FILE, TOKEN_ENCRYPTION_KEY

logger = logging.getLogger(__name__)


class EncryptionKeyError(RuntimeError):
    """The encryption key is invalid, or a value cannot be decrypted with it."""


def _invalid_file_error(path: Path) -> EncryptionKeyError:
    return EncryptionKeyError(
        f"The encryption key file {path} is invalid. Restore the original key file or set TOKEN_ENCRYPTION_KEY."
    )


def _validated(key: bytes) -> bytes:
    """Return ``key`` if it is a usable Fernet key; raise ``ValueError``/``TypeError`` otherwise."""
    Fernet(key)
    return key


def _read_key_file(path: Path) -> bytes:
    """Read and validate the key file; ``FileNotFoundError`` passes through, other failures raise."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise EncryptionKeyError(f"The encryption key file {path} cannot be read: {exc.strerror}.") from exc
    try:
        return _validated(raw.strip())
    except (ValueError, TypeError) as exc:
        raise _invalid_file_error(path) from exc


def _create_key_file(path: Path) -> bytes:
    """Create the key file exclusively (0600); if another writer won the race, read theirs."""
    key = Fernet.generate_key()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_key_file(path)
    except OSError as exc:
        raise EncryptionKeyError(f"The encryption key file {path} cannot be created: {exc.strerror}.") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    except OSError as exc:
        # Do not leave an empty or partial key file behind: it would be "invalid" forever.
        path.unlink(missing_ok=True)
        raise EncryptionKeyError(f"The encryption key file {path} cannot be created: {exc.strerror}.") from exc
    logger.info("Generated a new encryption key at %s.", path)
    return key


def _key_bytes() -> bytes:
    env_key = TOKEN_ENCRYPTION_KEY.strip()
    if env_key:
        try:
            return _validated(env_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise EncryptionKeyError("TOKEN_ENCRYPTION_KEY is invalid. Provide a valid Fernet key.") from exc
    path = Path(ENCRYPTION_KEY_FILE)
    try:
        return _read_key_file(path)
    except FileNotFoundError:
        return _create_key_file(path)


def get_fernet() -> Fernet:
    return Fernet(_key_bytes())


def encryption_key_source() -> str:
    """Where the key comes from: ``environment``, ``file``, ``missing`` or ``invalid``. Never creates the file."""
    env_key = TOKEN_ENCRYPTION_KEY.strip()
    if env_key:
        try:
            _validated(env_key.encode("utf-8"))
        except (ValueError, TypeError):
            return "invalid"
        return "environment"
    path = Path(ENCRYPTION_KEY_FILE)
    try:
        _read_key_file(path)
    except FileNotFoundError:
        return "missing"
    except EncryptionKeyError:
        return "invalid"
    return "file"


def encrypt_secret(value: str) -> str:
    token = get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    if not value.startswith(ENCRYPTED_VALUE_PREFIX):
        return value
    fernet = get_fernet()
    encrypted = value.removeprefix(ENCRYPTED_VALUE_PREFIX).encode("utf-8")
    try:
        return fernet.decrypt(encrypted).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise EncryptionKeyError("A saved secret cannot be decrypted with the current encryption key.") from exc


@functools.lru_cache(maxsize=64)
def _decrypt_cached(value: str, key: bytes | None) -> str | None:
    """Decrypt ``value`` with ``key``; on failure log one warning per (value, key) pair and return ``None``."""
    if key is not None:
        encrypted = value.removeprefix(ENCRYPTED_VALUE_PREFIX).encode("utf-8")
        try:
            return Fernet(key).decrypt(encrypted).decode("utf-8")
        except (InvalidToken, ValueError):
            pass
    logger.warning("A saved secret cannot be decrypted with the current encryption key.")
    return None


def read_secret(value: str) -> str | None:
    """Return ``value`` in clear (plaintext passes through), or ``None`` if it cannot be decrypted."""
    if not value.startswith(ENCRYPTED_VALUE_PREFIX):
        return value
    try:
        key: bytes | None = _key_bytes()
    except EncryptionKeyError:
        key = None
    return _decrypt_cached(value, key)
