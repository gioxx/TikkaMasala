"""Colour contrast computed from the design tokens (WCAG 1.4.3 text, 1.4.11 non-text), both themes.

The ratios come from the token values themselves, so a palette change is checked, not frozen.
"""

from __future__ import annotations

import re

import pytest

from tests.css_helpers import load_css

SURFACES = ("--bg", "--topbar-bg", "--panel", "--panel-2", "--input-bg")
TEXT_TOKENS = ("--text", "--text-2", "--muted", "--link", "--link-hover")
NON_TEXT_TOKENS = ("--border-strong", "--focus")
# Status text sits on its own tinted background, itself over a card or the page.
STATUS_PAIRS = (
    ("--status-match-text", "--status-match-bg"),
    ("--status-alt-text", "--status-alt-bg"),
    ("--status-warn-text", "--status-warn-bg"),
    ("--status-muted-text", "--status-muted-bg"),
    ("--status-info-text", "--status-info-bg"),
    ("--danger-text", "--danger-bg"),
)


def _declarations(block: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", block)}


def _parse(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        hex_value = value[1:]
        if len(hex_value) == 3:
            hex_value = "".join(c * 2 for c in hex_value)
        return (int(hex_value[0:2], 16), int(hex_value[2:4], 16), int(hex_value[4:6], 16), 1.0)
    match = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    assert match, value
    parts = [float(p) for p in re.split(r"[\s,/]+", match.group(1).strip()) if p]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def _over(top, bottom):
    alpha = top[3]
    return tuple(top[i] * alpha + bottom[i] * (1 - alpha) for i in range(3)) + (1.0,)


def _luminance(color) -> float:
    def channel(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in color[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(foreground, background) -> float:
    fg = _over(foreground, background) if foreground[3] < 1 else foreground
    lighter, darker = sorted((_luminance(fg), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _themes(css: str) -> dict[str, dict[str, str]]:
    light = _declarations(re.search(r"^:root \{([^}]*)\}", css, re.M).group(1))
    system_dark = _declarations(re.search(r':root:not\(\[data-theme="light"\]\) \{([^}]*)\}', css).group(1))
    forced_dark = _declarations(re.search(r':root\[data-theme="dark"\] \{([^}]*)\}', css).group(1))
    return {"light": light, "system dark": {**light, **system_dark}, "forced dark": {**light, **forced_dark}}


APP_THEMES = _themes(load_css())


def test_both_dark_paths_use_the_same_tokens():
    assert APP_THEMES["system dark"] == APP_THEMES["forced dark"]


@pytest.mark.parametrize("theme", ["light", "forced dark"])
@pytest.mark.parametrize("token", TEXT_TOKENS)
def test_text_tokens_on_every_surface(theme, token):
    tokens = APP_THEMES[theme]
    for surface in SURFACES:
        value = ratio(_parse(tokens[token]), _parse(tokens[surface]))
        assert value >= 4.5, f"{theme}: {token} on {surface} = {value:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "forced dark"])
@pytest.mark.parametrize("token", NON_TEXT_TOKENS)
def test_control_borders_and_focus_ring_on_every_surface(theme, token):
    tokens = APP_THEMES[theme]
    for surface in SURFACES:
        value = ratio(_parse(tokens[token]), _parse(tokens[surface]))
        assert value >= 3, f"{theme}: {token} on {surface} = {value:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "forced dark"])
@pytest.mark.parametrize("text,background", STATUS_PAIRS)
def test_status_text_on_its_tint(theme, text, background):
    tokens = APP_THEMES[theme]
    for surface in ("--panel", "--panel-2", "--bg"):
        tint = _over(_parse(tokens[background]), _parse(tokens[surface]))
        value = ratio(_parse(tokens[text]), tint)
        assert value >= 4.5, f"{theme}: {text} on {background} over {surface} = {value:.2f}:1"


def test_tooltip_text_on_its_bubble():
    # The bubble inverts the page colours: --bg text on a --text background.
    for theme, tokens in APP_THEMES.items():
        value = ratio(_parse(tokens["--bg"]), _parse(tokens["--text"]))
        assert value >= 4.5, f"{theme}: tooltip {value:.2f}:1"


def _access_denied_themes(text: str) -> dict[str, dict[str, str]]:
    def block(pattern: str) -> dict[str, str]:
        return _declarations(re.search(pattern, text).group(1))

    light = block(r":root \{([^}]*)\}")
    system_dark = block(r':root:not\(\[data-theme="light"\]\) \{([^}]*)\}')
    forced_dark = block(r':root\[data-theme="dark"\] \{([^}]*)\}')
    return {"light": light, "system dark": {**light, **system_dark}, "forced dark": {**light, **forced_dark}}


def test_access_denied_page_contrast(client):
    from app.access import AccessDenied, AccessSettings

    class _Deny:
        settings = AccessSettings("team.cloudflareaccess.com", "aud")

        def verify(self, token):
            raise AccessDenied("no token")

    client.app.state.access_verifier = _Deny()
    try:
        response = client.get("/")
    finally:
        client.app.state.access_verifier = None
    assert response.status_code == 403
    themes = _access_denied_themes(response.text)
    assert themes["system dark"] == themes["forced dark"]
    for theme, tokens in themes.items():
        for text in ("--fg", "--muted"):
            for surface in ("--bg", "--card", "--badge"):
                value = ratio(_parse(tokens[text]), _parse(tokens[surface]))
                assert value >= 4.5, f"{theme}: {text} on {surface} = {value:.2f}:1"
