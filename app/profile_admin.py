"""Removing a profile: what it takes with it, the run-lock check, unscheduling and optional backup deletion."""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass

from app.backups import count_backups_for_account, delete_backup_rows_for_account_in, remove_backup_files
from app.credentials import effective_account_id_of
from app.db import ProfileRecord, db
from app.profiles import DEFAULT_NOT_REMOVABLE, DEFAULT_PROFILE_ID, delete_profile_rows_in, get_profile, list_profiles
from app.scheduler import configure_all_auto_backup_jobs, forget_profile_runtime, profile_run_lock

logger = logging.getLogger(__name__)

REMOVE_BUSY_MESSAGE = "A backup run of this profile is in progress. Try again when it finishes."
SHARED_BACKUPS_MESSAGE = "These backups belong to an account another profile still uses, so they are kept."
BACKUP_COUNT_CHANGED_MESSAGE = "The number of backups changed since this page was shown. Check again."


class ProfileBusy(Exception):
    """The profile's run lock is held: removal waits until the run finishes."""

    def __init__(self, message: str = REMOVE_BUSY_MESSAGE) -> None:
        super().__init__(message)


class BackupCountChanged(ValueError):
    """The backups to delete are not the ones the confirmation page counted."""

    def __init__(self, message: str = BACKUP_COUNT_CHANGED_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class RemovalPreview:
    profile_id: int
    name: str
    account_id: str | None
    backup_count: int
    backups_shared: bool  # another profile's effective Account ID is the same (case-insensitive)


def _shared_with_another(account_id: str, profile_id: int, profiles: list[ProfileRecord]) -> bool:
    target = account_id.casefold()
    return any(
        other.id != profile_id and effective_account_id_of(other).casefold() == target for other in profiles
    )


def removal_preview(profile_id: int) -> RemovalPreview:
    """What removing a profile would take with it. ``ValueError`` for Default, ``LookupError`` when missing."""
    if profile_id == DEFAULT_PROFILE_ID:
        raise ValueError(DEFAULT_NOT_REMOVABLE)
    profile = get_profile(profile_id)
    account_id = effective_account_id_of(profile) or None
    if account_id is None:
        return RemovalPreview(profile.id, profile.name, None, 0, False)
    return RemovalPreview(
        profile_id=profile.id,
        name=profile.name,
        account_id=account_id,
        backup_count=count_backups_for_account(account_id),
        backups_shared=_shared_with_another(account_id, profile.id, list_profiles()),
    )


def remove_profile(profile_id: int, *, delete_backups: bool, expected_backup_count: int | None = None) -> int:
    """Remove a profile; with ``delete_backups`` also its account's backups. Returns the backups deleted.

    Synchronous on purpose: called from the event loop, the lock check and the delete cannot be
    interleaved with a run starting. The profile row, its destinations, its run history and (when
    asked) the backup rows go in one transaction; backup files are removed after it commits.

    Raises ``ValueError(DEFAULT_NOT_REMOVABLE)``, ``LookupError``, ``ProfileBusy``,
    ``ValueError(SHARED_BACKUPS_MESSAGE)`` or ``BackupCountChanged``.
    """
    preview = removal_preview(profile_id)
    if profile_run_lock(profile_id).locked():
        raise ProfileBusy()
    if delete_backups and preview.backups_shared:
        raise ValueError(SHARED_BACKUPS_MESSAGE)
    if delete_backups and expected_backup_count != preview.backup_count:
        raise BackupCountChanged()

    filenames: list[str] = []
    with closing(db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if delete_backups and preview.account_id:
                # Checked again under the write lock: the answer must hold for the rows deleted.
                if _shared_with_another(preview.account_id, profile_id, list_profiles(conn)):
                    raise ValueError(SHARED_BACKUPS_MESSAGE)
                current = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM backups WHERE lower(account_id) = lower(?)", (preview.account_id,)
                    ).fetchone()[0]
                )
                if current != expected_backup_count:
                    raise BackupCountChanged()
                filenames = delete_backup_rows_for_account_in(conn, preview.account_id)
            delete_profile_rows_in(conn, profile_id)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()

    forget_profile_runtime(profile_id)
    configure_all_auto_backup_jobs()
    remove_backup_files(filenames)
    logger.info("Profile %s removed (backups deleted: %s).", profile_id, len(filenames))
    return len(filenames)
