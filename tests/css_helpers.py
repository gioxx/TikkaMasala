"""Helpers for tests that read app/static/styles.css: scoped @media blocks and rules."""

from __future__ import annotations

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css"


def load_css(*, strip_comments: bool = True) -> str:
    css = STYLES.read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S) if strip_comments else css


def _block_end(css: str, open_brace: int) -> int:
    """Index of the brace closing the one at ``open_brace`` (brace matching, not a substring)."""
    depth = 0
    for index in range(open_brace, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unbalanced braces")


def media_blocks(css: str, query: str) -> list[str]:
    """The bodies of every ``@media {query} { ... }`` block whose query is exactly ``query``."""
    pattern = re.compile(r"@media\s+" + re.escape(query) + r"\s*\{")
    bodies = []
    for match in pattern.finditer(css):
        open_brace = match.end() - 1
        bodies.append(css[open_brace + 1:_block_end(css, open_brace)])
    if not bodies:
        raise AssertionError(f"no @media {query} block")
    return bodies


def media_block(css: str, query: str) -> str:
    """Every ``@media {query}`` block joined: the rules that apply at exactly that query."""
    return "\n".join(media_blocks(css, query))


def without_at_rules(css: str) -> str:
    """The stylesheet with every @media / @supports / @keyframes block removed."""
    out = []
    index = 0
    for match in re.finditer(r"@(media|supports|keyframes)[^{]*\{", css):
        if match.start() < index:
            continue
        out.append(css[index:match.start()])
        index = _block_end(css, match.end() - 1) + 1
    out.append(css[index:])
    return "".join(out)


def rules(css: str) -> list[tuple[str, str]]:
    """(selector list, declarations) for every flat rule in ``css``."""
    return [(" ".join(sel.split()), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def rule_body(css: str, selector: str) -> str:
    """Declarations of the rule whose selector list is exactly ``selector`` (all of them joined)."""
    bodies = [body for sel, body in rules(css) if sel == selector]
    if not bodies:
        raise AssertionError(f"no rule for {selector}")
    return "\n".join(bodies)


def declarations(body: str) -> dict[str, str]:
    out = {}
    for part in body.split(";"):
        if ":" in part:
            name, value = part.split(":", 1)
            out[name.strip()] = value.strip()
    return out
