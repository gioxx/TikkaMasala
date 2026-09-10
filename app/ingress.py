"""Pure helpers for reasoning about Cloudflare Tunnel ingress rules.

No FastAPI / httpx imports here on purpose: everything is unit-testable in
isolation.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# Non-IP host names that resolve differently depending on where the tunnel runs.
FRAGILE_HOST_NAMES = frozenset({"localhost"})

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
    if host in FRAGILE_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def dict_delta(
    before: dict[str, Any],
    after: dict[str, Any],
    ignore: "set[str] | frozenset[str] | tuple[str, ...]" = (),
) -> list[tuple[str, Any, Any]]:
    """Return sorted ``(key, old, new)`` for every key whose value changed.

    Keys present on only one side report ``None`` for the missing side. Keys in
    ``ignore`` are skipped.
    """

    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    skip = set(ignore)
    keys = sorted((set(before) | set(after)) - skip)
    return [
        (key, before.get(key), after.get(key))
        for key in keys
        if before.get(key) != after.get(key)
    ]


def origin_request_delta(
    before: dict[str, Any], after: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """Return ``(key, old, new)`` for every ``originRequest`` key that changed."""

    return dict_delta(before, after)


def config_settings_delta(
    before_config: dict[str, Any], after_config: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """Return changed top-level tunnel-config keys other than ``ingress``.

    Restore PUTs the whole snapshot ``config`` body, so fields such as
    ``warp-routing`` or top-level ``originRequest`` defaults are applied even
    though the ingress diff never mentions them.
    """

    return dict_delta(before_config, after_config, ignore={"ingress"})


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


_RuleKey = tuple[str | None, str | None, int]


def _keyed(rules: list[IngressRule]) -> list[tuple[_RuleKey, IngressRule]]:
    """Pair each rule with ``(hostname, path, occurrence)``.

    The occurrence counter keeps duplicate ``(hostname, path)`` rules distinct so
    the diff does not silently collapse them (Cloudflare routes on first match).
    """

    seen: dict[tuple[str | None, str | None], int] = {}
    keyed: list[tuple[_RuleKey, IngressRule]] = []
    for rule in rules:
        base = (rule.hostname, rule.path)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        keyed.append(((rule.hostname, rule.path, occurrence), rule))
    return keyed


def diff_ingress(
    current: list[IngressRule], incoming: list[IngressRule]
) -> IngressDiff:
    """Diff the live config (``current``) against the snapshot (``incoming``).

    Cloudflare evaluates ingress rules top to bottom, so a pure reorder of
    otherwise-identical rules still changes routing: it is reported via
    ``reordered`` even when nothing was added, removed, or edited.
    """

    current_keyed = _keyed(current)
    incoming_keyed = _keyed(incoming)
    current_by_key = dict(current_keyed)
    incoming_by_key = dict(incoming_keyed)
    diff = IngressDiff()

    for key, inc in incoming_keyed:
        cur = current_by_key.get(key)
        if cur is None:
            diff.added.append(inc)
        elif (cur.service, cur.origin_request) != (inc.service, inc.origin_request):
            diff.changed.append((cur, inc))
        else:
            diff.unchanged.append(inc)

    for key, cur in current_keyed:
        if key not in incoming_by_key:
            diff.removed.append(cur)

    shared_current = [key for key, _ in current_keyed if key in incoming_by_key]
    shared_incoming = [key for key, _ in incoming_keyed if key in current_by_key]
    diff.reordered = shared_current != shared_incoming

    return diff
