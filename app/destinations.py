"""Per-profile notification destinations (webhook, Telegram) stored in ``notification_destinations``.

The webhook URL or bot token lives in ``target``, always Fernet-encrypted (``enc:`` prefix). Nothing
here ever returns it except ``delivery_targets``, whose ``DeliveryTarget.secret`` is kept out of
``repr``. Views expose only the webhook host or the last four characters of the Telegram chat ID.
A secret that cannot be decrypted marks the destination unusable: it is flagged in views and skipped
for delivery, never raised to callers.

This module must not import ``app.notifications``: that module imports this one.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.config import DEFAULT_NOTIFICATION_EVENTS
from app.db import db
from app.encryption import EncryptionKeyError, encrypt_secret, read_secret
from app.profiles import DEFAULT_PROFILE_ID, is_control_character

logger = logging.getLogger(__name__)

DESTINATION_KINDS = ("webhook", "telegram")
DESTINATION_EVENTS = (
    "manual_backup_success",
    "manual_backup_failed",
    "auto_backup_success",
    "auto_backup_partial",
    "auto_backup_failed",
    "restore_success",
    "restore_failed",
    "retention_cleanup",
)
LABEL_MAX = 60
WEBHOOK_URL_MAX = 2048
TELEGRAM_TOKEN_PATTERN = r"\d{3,}:[A-Za-z0-9_-]{20,}"
TELEGRAM_CHAT_PATTERN = r"-?\d{1,20}|@[A-Za-z0-9_]{4,64}"

_DEFAULT_LABELS = {"webhook": "Webhook", "telegram": "Telegram"}
# ASCII only: ``\d`` would otherwise accept Arabic-Indic and other Unicode digits.
_TOKEN_RE = re.compile(TELEGRAM_TOKEN_PATTERN, re.ASCII)
_CHAT_RE = re.compile(TELEGRAM_CHAT_PATTERN, re.ASCII)
_WEBHOOK_URL_MESSAGE = "The webhook URL must start with http:// or https:// and include a host."
UNREADABLE_HINT = "Saved secret cannot be decrypted. Edit the destination and enter it again."


def encode_events(events: Iterable[str]) -> str:
    """A JSON array of the known event keys in ``DESTINATION_EVENTS`` order, without duplicates."""
    chosen = set(events)
    return json.dumps([event for event in DESTINATION_EVENTS if event in chosen])


def decode_events(raw: str | None) -> frozenset[str]:
    """The stored events: missing or malformed means the defaults, ``[]`` means none; unknown keys drop."""
    if not raw:
        return DEFAULT_NOTIFICATION_EVENTS
    try:
        value = json.loads(raw)
    except ValueError:
        return DEFAULT_NOTIFICATION_EVENTS
    if not isinstance(value, list):
        return DEFAULT_NOTIFICATION_EVENTS
    return frozenset(item for item in value if isinstance(item, str) and item in DESTINATION_EVENTS)


def webhook_host(url: str) -> str | None:
    """The host of a webhook URL, or None when the URL cannot be parsed (never the path or query)."""
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


class DestinationInputError(ValueError):
    """A destination form value is invalid. ``field`` names the input; the message never holds a secret."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class DestinationInput:
    kind: str
    label: str
    secret: str = field(repr=False)
    chat_id: str
    events: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class DestinationView:
    id: int
    profile_id: int
    kind: str
    label: str
    target_hint: str
    events: frozenset[str]
    enabled: bool
    unreadable: bool
    chat_id: str


@dataclass(frozen=True)
class DeliveryTarget:
    key: str
    label: str
    kind: str
    events: frozenset[str]
    secret: str = field(repr=False)
    chat_id: str


def _valid_webhook_url(url: str) -> bool:
    if len(url) > WEBHOOK_URL_MAX:
        return False
    if any(char.isspace() or not char.isprintable() for char in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def validate_destination(data: DestinationInput, *, creating: bool) -> DestinationInput:
    """The input with every field stripped and checked; raise ``DestinationInputError`` on the first problem.

    While editing (``creating=False``) an empty secret is allowed: it means "keep the saved one".
    """
    kind = data.kind.strip()
    if kind not in DESTINATION_KINDS:
        raise DestinationInputError("kind", "Choose webhook or Telegram.")
    label = data.label.strip() or _DEFAULT_LABELS[kind]
    if len(label) > LABEL_MAX:
        raise DestinationInputError("label", f"Labels can be at most {LABEL_MAX} characters.")
    if any(is_control_character(ch) for ch in label):
        raise DestinationInputError("label", "Labels cannot contain control characters.")
    secret = data.secret.strip()
    chat_id = ""
    if kind == "webhook":
        if not secret:
            if creating:
                raise DestinationInputError("secret", "Enter the webhook URL.")
        elif not _valid_webhook_url(secret):
            raise DestinationInputError("secret", _WEBHOOK_URL_MESSAGE)
    else:
        if not secret:
            if creating:
                raise DestinationInputError("secret", "Enter the Telegram bot token.")
        elif not _TOKEN_RE.fullmatch(secret):
            raise DestinationInputError("secret", "The bot token should look like 123456789:AA…")
        chat_id = data.chat_id.strip()
        if not _CHAT_RE.fullmatch(chat_id):
            raise DestinationInputError("chat_id", "Enter the Telegram chat ID: a number, or @channelname.")
    events = [event.strip() for event in data.events]
    if not events:
        raise DestinationInputError("events", "Pick at least one event.")
    for event in events:
        if event not in DESTINATION_EVENTS:
            raise DestinationInputError("events", f"Unknown event: {event}.")
    ordered = tuple(event for event in DESTINATION_EVENTS if event in set(events))
    return DestinationInput(
        kind=kind, label=label, secret=secret, chat_id=chat_id, events=ordered, enabled=bool(data.enabled)
    )


def _reveal(target: str | None) -> str | None:
    """The secret in clear, or None when it cannot be decrypted (never raises)."""
    if not target:
        return None
    try:
        return read_secret(target)
    except (EncryptionKeyError, ValueError, TypeError):
        return None


def _label(row: sqlite3.Row) -> str:
    return row["label"] or _DEFAULT_LABELS.get(row["kind"], "Destination")


def _view(row: sqlite3.Row) -> DestinationView:
    kind = str(row["kind"])
    chat_id = str(row["chat_id"] or "") if kind == "telegram" else ""
    secret = _reveal(row["target"])
    if secret is None:
        hint = UNREADABLE_HINT
    elif kind == "webhook":
        hint = webhook_host(secret) or "URL saved"
    else:
        hint = f"chat ending in {chat_id[-4:]}"
    return DestinationView(
        id=int(row["id"]),
        profile_id=int(row["profile_id"]),
        kind=kind,
        label=_label(row),
        target_hint=hint,
        events=decode_events(row["events"]),
        enabled=bool(row["enabled"]),
        unreadable=secret is None,
        chat_id=chat_id,
    )


def _row(conn: sqlite3.Connection, destination_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notification_destinations WHERE id = ?", (destination_id,)).fetchone()
    if row is None:
        raise LookupError(f"Notification destination {destination_id} not found.")
    return row


def _encrypt(secret: str) -> str:
    try:
        return encrypt_secret(secret)
    except EncryptionKeyError as exc:
        raise DestinationInputError("secret", f"The secret cannot be saved: {exc}") from None


def list_destinations(profile_id: int) -> list[DestinationView]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM notification_destinations WHERE profile_id = ? ORDER BY id", (profile_id,)
        ).fetchall()
    return [_view(row) for row in rows]


def get_destination(destination_id: int) -> DestinationView:
    with closing(db()) as conn:
        return _view(_row(conn, destination_id))


def create_destination(profile_id: int, data: DestinationInput) -> DestinationView:
    clean = validate_destination(data, creating=True)
    target = _encrypt(clean.secret)
    with closing(db()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO notification_destinations (profile_id, kind, label, target, chat_id, events, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                clean.kind,
                clean.label,
                target,
                clean.chat_id,
                encode_events(clean.events),
                int(clean.enabled),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        destination_id = int(cursor.lastrowid)
    return get_destination(destination_id)


def update_destination(
    destination_id: int, data: DestinationInput, *, profile_id: int | None = None
) -> DestinationView:
    """Save the edited values; an empty secret keeps the saved one. The kind cannot change.

    With ``profile_id``, a destination of another profile is treated as missing (``LookupError``).
    """
    with closing(db()) as conn:
        row = _row(conn, destination_id)
    if profile_id is not None and int(row["profile_id"]) != profile_id:
        raise LookupError(f"Notification destination {destination_id} not found.")
    stored_kind = str(row["kind"])
    clean = validate_destination(data, creating=False)
    if clean.kind != stored_kind:
        raise DestinationInputError("kind", "The kind of a destination cannot change.")
    target = _encrypt(clean.secret) if clean.secret else None
    owner = int(row["profile_id"]) if profile_id is None else profile_id
    with closing(db()) as conn:
        cursor = conn.execute(
            """
            UPDATE notification_destinations
            SET label = ?, target = COALESCE(?, target), chat_id = ?, events = ?, enabled = ?
            WHERE id = ? AND profile_id = ?
            """,
            (
                clean.label,
                target,
                clean.chat_id,
                encode_events(clean.events),
                int(clean.enabled),
                destination_id,
                owner,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise LookupError(f"Notification destination {destination_id} not found.")
    return get_destination(destination_id)


def delete_destination(destination_id: int, *, profile_id: int | None = None) -> None:
    """Delete a destination. With ``profile_id``, only one of that profile, else ``LookupError``."""
    with closing(db()) as conn:
        if profile_id is None:
            conn.execute("DELETE FROM notification_destinations WHERE id = ?", (destination_id,))
        else:
            cursor = conn.execute(
                "DELETE FROM notification_destinations WHERE id = ? AND profile_id = ?", (destination_id, profile_id)
            )
            if cursor.rowcount == 0:
                raise LookupError(f"Notification destination {destination_id} not found.")
        conn.commit()


def delivery_targets(profile_id: int | None) -> list[DeliveryTarget]:
    """The enabled destinations of a profile (``None`` means Default) whose secret decrypts, in id order."""
    pid = DEFAULT_PROFILE_ID if profile_id is None else profile_id
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM notification_destinations WHERE profile_id = ? AND enabled = 1 ORDER BY id", (pid,)
        ).fetchall()
    targets: list[DeliveryTarget] = []
    for row in rows:
        label = _label(row)
        secret = _reveal(row["target"])
        if secret is None:
            logger.warning(
                "Notification destination %s skipped: its secret cannot be decrypted.", f"{row['id']} ({label!r})"
            )
            continue
        targets.append(
            DeliveryTarget(
                key=f"dest-{row['id']}",
                label=label,
                kind=str(row["kind"]),
                events=decode_events(row["events"]),
                secret=secret,
                chat_id=str(row["chat_id"] or "") if row["kind"] == "telegram" else "",
            )
        )
    return targets
