"""Pure helpers for reasoning about Cloudflare Tunnel ingress rules.

No FastAPI / httpx imports here on purpose: everything is unit-testable in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

FRAGILE_HOSTS = frozenset({"localhost", "::1"})

# Services whose scheme implies a network origin we can resolve a host from.
_NETWORK_SCHEMES = frozenset({"http", "https", "tcp", "ssh", "rdp", "smb", "ws", "wss"})


def service_host(service: str) -> str | None:
    """Return the lowercased host for a URL-style ingress service.

    Returns None for non-network services such as ``http_status:404``,
    ``hello_world``, ``bastion`` or ``unix:/path``.
    """

    if not isinstance(service, str) or "://" not in service:
        return None
    parts = urlsplit(service)
    if parts.scheme.lower() not in _NETWORK_SCHEMES:
        return None
    host = parts.hostname  # already lowercased, IPv6 brackets stripped
    return host or None


def is_fragile_service(service: str) -> bool:
    """True when the service points at a host- or container-local origin."""

    host = service_host(service)
    if host is None:
        return False
    if host in FRAGILE_HOSTS:
        return True
    return host.startswith("127.")


@dataclass(frozen=True)
class IngressRule:
    hostname: str | None
    service: str
    path: str | None
    origin_request: dict[str, Any] = field(default_factory=dict)
    is_catch_all: bool = False
    is_fragile: bool = False


def _ingress_list(source: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(source, dict):
        return []
    node: Any = source
    for key in ("configuration", "config"):
        if isinstance(node, dict) and isinstance(node.get(key), dict):
            node = node[key]
    if not isinstance(node, dict):
        return []
    ingress = node.get("ingress")
    return ingress if isinstance(ingress, list) else []


def extract_ingress(source: dict[str, Any]) -> list[IngressRule]:
    """Parse ingress rules out of a snapshot payload or a config dict."""

    raw = _ingress_list(source)
    rules: list[IngressRule] = []
    last_index = len(raw) - 1
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        hostname = entry.get("hostname") or None
        service = str(entry.get("service") or "")
        path = entry.get("path") or None
        origin_request = entry.get("originRequest")
        if not isinstance(origin_request, dict):
            origin_request = {}
        rules.append(
            IngressRule(
                hostname=hostname,
                service=service,
                path=path,
                origin_request=origin_request,
                is_catch_all=hostname is None and index == last_index,
                is_fragile=is_fragile_service(service),
            )
        )
    return rules


def fragile_rules(rules: list[IngressRule]) -> list[IngressRule]:
    return [rule for rule in rules if rule.is_fragile]


def origin_request_delta(
    before: dict[str, Any], after: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """Return ``(key, old, new)`` for every ``originRequest`` key that changed.

    Keys present on only one side report ``None`` for the missing side.
    """

    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    keys = sorted(set(before) | set(after))
    return [
        (key, before.get(key), after.get(key))
        for key in keys
        if before.get(key) != after.get(key)
    ]


@dataclass
class IngressDiff:
    added: list[IngressRule] = field(default_factory=list)
    removed: list[IngressRule] = field(default_factory=list)
    changed: list[tuple[IngressRule, IngressRule]] = field(default_factory=list)
    unchanged: list[IngressRule] = field(default_factory=list)
    reordered: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.reordered)


def _key(rule: IngressRule) -> tuple[str | None, str | None]:
    return (rule.hostname, rule.path)


def diff_ingress(
    current: list[IngressRule], incoming: list[IngressRule]
) -> IngressDiff:
    """Diff the live config (``current``) against the snapshot (``incoming``).

    Cloudflare evaluates ingress rules top to bottom, so a pure reorder of
    otherwise-identical rules still changes routing: it is reported via
    ``reordered`` even when nothing was added, removed, or edited.
    """

    current_by_key = {_key(rule): rule for rule in current}
    incoming_by_key = {_key(rule): rule for rule in incoming}
    diff = IngressDiff()

    for key, inc in incoming_by_key.items():
        cur = current_by_key.get(key)
        if cur is None:
            diff.added.append(inc)
        elif (cur.service, cur.origin_request) != (inc.service, inc.origin_request):
            diff.changed.append((cur, inc))
        else:
            diff.unchanged.append(inc)

    for key, cur in current_by_key.items():
        if key not in incoming_by_key:
            diff.removed.append(cur)

    shared_current = [_key(r) for r in current if _key(r) in incoming_by_key]
    shared_incoming = [_key(r) for r in incoming if _key(r) in current_by_key]
    diff.reordered = shared_current != shared_incoming

    return diff
