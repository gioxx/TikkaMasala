"""Account ID / API token resolution, validation on save and encryption at rest."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import HTTPException

from app import cloudflare
from app.cloudflare import CredentialsInvalid
from app.config import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_API_TOKEN,
    DEMO_MODE,
    ENCRYPTED_VALUE_PREFIX,
)
from app.db import ProfileRecord
from app.encryption import EncryptionKeyError, encrypt_secret, read_secret
from app.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles, update_profile

logger = logging.getLogger(__name__)


def _env_account_id(profile_id: int) -> str:
    """Environment fallback: only the Default profile ever reads ``CLOUDFLARE_ACCOUNT_ID``."""
    return DEFAULT_ACCOUNT_ID.strip() if profile_id == DEFAULT_PROFILE_ID else ""


def _env_api_token(profile_id: int) -> str:
    """Environment fallback: only the Default profile ever reads ``CLOUDFLARE_API_TOKEN``."""
    return DEFAULT_API_TOKEN.strip() if profile_id == DEFAULT_PROFILE_ID else ""


def _stored_account_id(profile_id: int) -> str:
    return (get_profile(profile_id).account_id or "").strip()


def get_saved_account_id(profile_id: int) -> str:
    if DEMO_MODE:
        return _env_account_id(profile_id)
    return _stored_account_id(profile_id) or _env_account_id(profile_id)


def get_account_id_source(profile_id: int) -> str | None:
    if DEMO_MODE:
        return "environment" if _env_account_id(profile_id) else None
    if _stored_account_id(profile_id):
        return "database"
    if _env_account_id(profile_id):
        return "environment"
    return None


def _read_stored_api_token(profile_id: int) -> str | None:
    """Return the profile's saved token in clear, or ``None`` if unset or unreadable.

    A token still stored in plaintext (legacy install) is re-saved encrypted.
    """
    stored_token = (get_profile(profile_id).api_token or "").strip()
    if not stored_token:
        return None
    decrypted = (read_secret(stored_token) or "").strip()
    if decrypted and not stored_token.startswith(ENCRYPTED_VALUE_PREFIX):
        try:
            remember_api_token(profile_id, decrypted)
        except RuntimeError:
            logger.warning("Unable to encrypt the plaintext API token of profile %s; it stays as is.", profile_id)
    return decrypted or None


def get_server_api_token(profile_id: int) -> str:
    """The profile's API token in clear: database first, then environment (demo: environment only)."""
    if DEMO_MODE:
        return _env_api_token(profile_id)
    return _read_stored_api_token(profile_id) or _env_api_token(profile_id)


def get_server_api_token_source(profile_id: int) -> str | None:
    if DEMO_MODE:
        return "environment" if _env_api_token(profile_id) else None
    if _read_stored_api_token(profile_id):
        return "database"
    if _env_api_token(profile_id):
        return "environment"
    return None


def remember_api_token(profile_id: int, api_token: str) -> None:
    if DEMO_MODE:
        return
    normalized = api_token.strip()
    if normalized:
        update_profile(profile_id, api_token=encrypt_secret(normalized))


def clear_saved_credentials(profile_id: int) -> None:
    """Forget the Account ID and API token saved on the profile (environment values stay)."""
    if DEMO_MODE:
        return
    update_profile(profile_id, account_id=None, api_token=None)


@dataclass(frozen=True)
class ApiCredentials:
    account_id: str
    api_token: str = field(repr=False)


def get_api_credentials(profile_id: int) -> ApiCredentials | None:
    """The effective credentials of a profile (demo: environment only), or ``None`` if incomplete."""
    account_id = get_saved_account_id(profile_id)
    api_token = get_server_api_token(profile_id)
    if not account_id or not api_token:
        return None
    return ApiCredentials(account_id=account_id, api_token=api_token)


DEMO_NO_CREDENTIALS_MESSAGE = (
    "No credentials in demo mode. Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to try it."
)
MISSING_CREDENTIALS_MESSAGE = "Account ID and API token are not configured. Add them in Settings."


def require_api_credentials(profile_id: int) -> ApiCredentials:
    """The profile's effective credentials, or a 400 ``HTTPException`` with a user-facing message."""
    creds = get_api_credentials(profile_id)
    if creds is None:
        detail = DEMO_NO_CREDENTIALS_MESSAGE if DEMO_MODE else MISSING_CREDENTIALS_MESSAGE
        raise HTTPException(status_code=400, detail=detail)
    return creds


def mask_secret(value: str) -> str:
    """Describe a secret without revealing it: the last four characters only for long values."""
    if not value:
        return ""
    if len(value) >= 12:
        return f"configured, ending in {value[-4:]}"
    return "configured"


@dataclass(frozen=True)
class CredentialField:
    value: str  # the Account ID in clear, or the masked token
    source: str | None
    env_value_present: bool


@dataclass(frozen=True)
class ProfileCredentialsView:
    profile_id: int
    name: str
    account_id: CredentialField
    api_token: CredentialField
    saved_token_unreadable: bool
    has_saved_values: bool


def describe_profile_credentials(profile_id: int) -> ProfileCredentialsView:
    """What Settings shows for a profile. Never carries the token in clear."""
    profile = get_profile(profile_id)
    stored_account = (profile.account_id or "").strip()
    stored_token = (profile.api_token or "").strip()
    saved_token_unreadable = (
        not DEMO_MODE and bool(stored_token) and not (read_secret(stored_token) or "").strip()
    )
    return ProfileCredentialsView(
        profile_id=profile.id,
        name=profile.name,
        account_id=CredentialField(
            value=get_saved_account_id(profile_id),
            source=get_account_id_source(profile_id),
            env_value_present=bool(_env_account_id(profile_id)),
        ),
        api_token=CredentialField(
            value=mask_secret(get_server_api_token(profile_id)),
            source=get_server_api_token_source(profile_id),
            env_value_present=bool(_env_api_token(profile_id)),
        ),
        saved_token_unreadable=saved_token_unreadable,
        has_saved_values=not DEMO_MODE and bool(stored_account or stored_token),
    )


def _effective_account_id_of(profile: ProfileRecord) -> str:
    """Same precedence as ``get_saved_account_id``, from an already loaded row."""
    if DEMO_MODE:
        return _env_account_id(profile.id)
    return (profile.account_id or "").strip() or _env_account_id(profile.id)


def find_profile_for_account(account_id: str) -> int | None:
    """The lowest-id profile whose effective Account ID matches (case-insensitive), if any."""
    target = account_id.strip().casefold()
    if not target:
        return None
    for profile in list_profiles():
        if _effective_account_id_of(profile).casefold() == target:
            return profile.id
    return None


async def save_profile_credentials(
    profile_id: int, *, account_id: str | None, api_token: str | None
) -> bool:
    """Validate and save a profile's credentials. Empty input keeps the current value.

    A value equal to the current effective one is not written (an unchanged
    environment value stays environment-sourced). The resulting pair is checked
    for completeness, duplicate Account IDs and against Cloudflare before any
    write. Returns whether anything was written; raises ``CredentialsInvalid``.
    """
    account_in = (account_id or "").strip()
    token_in = (api_token or "").strip()
    if DEMO_MODE:
        raise CredentialsInvalid("Credentials cannot be changed in demo mode.")

    current_account = get_saved_account_id(profile_id)
    current_token = get_server_api_token(profile_id)
    new_account = account_in if account_in and account_in != current_account else None
    new_token = token_in if token_in and token_in != current_token else None
    if new_account is None and new_token is None:
        return False

    resulting_account = new_account or current_account
    resulting_token = new_token or current_token
    if not resulting_account or not resulting_token:
        raise CredentialsInvalid("Both an Account ID and an API token are required.")

    target = resulting_account.casefold()
    for other in list_profiles():
        if other.id != profile_id and _effective_account_id_of(other).casefold() == target:
            raise CredentialsInvalid(
                f"Account ID {resulting_account} is already used by profile '{other.name}'."
            )

    await cloudflare.check_credentials(resulting_account, resulting_token)

    fields: dict[str, str] = {}
    if new_account is not None:
        fields["account_id"] = new_account
    if new_token is not None:
        try:
            fields["api_token"] = encrypt_secret(new_token)
        except EncryptionKeyError as exc:
            raise CredentialsInvalid(f"The API token cannot be saved: {exc}") from exc
    update_profile(profile_id, **fields)
    logger.info("Credentials of profile %s saved (account_id=%s).", profile_id, resulting_account)
    return True


@dataclass(frozen=True)
class RestoreTarget:
    """Who can restore a backup of ``account_id``: the owning profile and its credentials, if any."""

    account_id: str
    profile_id: int | None
    profile_name: str | None
    credentials: ApiCredentials | None

    @property
    def can_restore(self) -> bool:
        return self.credentials is not None


def resolve_restore_target(account_id: str) -> RestoreTarget:
    """The profile that covers ``account_id`` and its effective credentials (``None`` when incomplete)."""
    profile_id = find_profile_for_account(account_id)
    if profile_id is None:
        return RestoreTarget(account_id=account_id, profile_id=None, profile_name=None, credentials=None)
    return RestoreTarget(
        account_id=account_id,
        profile_id=profile_id,
        profile_name=get_profile(profile_id).name,
        credentials=get_api_credentials(profile_id),
    )
