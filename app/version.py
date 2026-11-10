"""How APP_VERSION is shown.

Release images get their tag (``1.3.11``). Images built from main get the output of
``git describe --tags --long`` (``1.3.10-42-g8cce54d``: nearest release, commits since it,
short SHA), or ``dev-<sha>`` when no release tag is reachable. Local runs default to ``dev``.
"""

from __future__ import annotations

import re

_DESCRIBE = re.compile(r"(?P<base>\d+\.\d+(?:\.\d+)?)-(?P<ahead>\d+)-g(?P<sha>[0-9a-f]{4,40})")
_DEV_SHA = re.compile(r"dev-(?P<sha>[0-9a-f]{4,40})")
_RELEASE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def display_version(raw: str | None) -> str:
    """``1.3.10-42-g8cce54d`` -> ``dev · 1.3.10+42 · 8cce54d``; ``dev`` and releases unchanged."""
    value = (raw or "").strip()
    if not value:
        return "dev"
    match = _DESCRIBE.fullmatch(value)
    if match:
        ahead = int(match["ahead"])
        base = match["base"] if ahead == 0 else f"{match['base']}+{ahead}"
        return f"dev · {base} · {match['sha']}"
    match = _DEV_SHA.fullmatch(value)
    if match:
        return f"dev · {match['sha']}"
    return value


def badge_version(raw: str | None) -> str:
    """The top bar badge: ``v`` before a release number, the display form otherwise."""
    shown = display_version(raw)
    return f"v{shown}" if _RELEASE.fullmatch(shown) else shown
