"""Shared layout: top navigation, active section, Access identity and the collapsible menu."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.credentials as credentials
import app.encryption as encryption
import app.routers.ingress as ingress_router
import app.setup as setup
import app.web as web
from app.access import AccessIdentity, AccessSettings
from app.profiles import DEFAULT_PROFILE_ID, update_profile

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
SECTIONS = ("tunnels", "hostnames", "backups", "automation", "settings")

PAGES = [
    ("/", "tunnels"),
    ("/ingress", "hostnames"),
    ("/backups", "backups"),
    ("/automation", "automation"),
    ("/settings", "settings"),
    ("/backups/new", "backups"),
    ("/automation/runs", "automation"),
    ("/automation/tunnels", "automation"),
    ("/settings/notifications", "settings"),
]
ACTIVE = re.compile(r'<a class="topnav-link" data-nav="(\w+)"[^>]*aria-current="page"')


@pytest.fixture(autouse=True)
def reset_access(client):
    yield
    client.app.state.access_verifier = None


@pytest.mark.parametrize("path,section", PAGES)
def test_page_uses_layout_with_active_section(client, path, section):
    response = client.get(path)
    assert response.status_code == 200, path
    text = response.text
    for key in SECTIONS:
        assert f'data-nav="{key}"' in text, (path, key)
    assert ACTIVE.findall(text) == [section]
    assert 'class="hero"' not in text
    assert "data-floating-nav" not in text


def test_backup_detail_is_under_backups(client, make_backup):
    backup_id = make_backup()
    response = client.get(f"/backup/{backup_id}")
    assert response.status_code == 200
    assert ACTIVE.findall(response.text) == ["backups"]


def test_identity_and_sign_out_in_nav(client):
    class _Verifier:
        settings = AccessSettings("team.cloudflareaccess.com", "aud")

        def verify(self, token):
            return AccessIdentity("a@b.c", "s")

    client.app.state.access_verifier = _Verifier()
    text = client.get("/").text
    start = text.index('<div class="topnav-identity">')
    block = text[start:text.index("</div>", start)]
    assert "a@b.c" in block
    assert 'href="/cdn-cgi/access/logout"' in block


def test_no_identity_when_access_off(client):
    assert 'class="topnav-identity"' not in client.get("/").text


def test_demo_nav_shows_tunnels_and_settings_only(client, monkeypatch):
    monkeypatch.setattr(web, "DEMO_MODE", True)
    text = client.get("/").text
    assert 'data-nav="tunnels"' in text
    assert 'data-nav="settings"' in text
    for key in ("hostnames", "backups", "automation"):
        assert f'data-nav="{key}"' not in text, key


@pytest.mark.setup_gate
def test_setup_page_has_brand_only(client, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", tmp_path / "encryption.key")
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)
    client.cookies.clear()
    response = client.get("/setup")
    assert response.status_code == 200
    assert 'class="topbar-brand"' in response.text
    assert "data-nav=" not in response.text
    assert "data-topnav-toggle" not in response.text


def test_menu_toggle_markup(client):
    text = client.get("/").text
    assert "data-topnav-toggle" in text
    assert 'aria-controls="topnav-menu"' in text
    assert 'id="topnav-menu"' in text


def test_css_collapses_nav_on_narrow_screens(client):
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    collapse = re.search(r"--topnav-collapse:\s*(\d+px);", css).group(1)
    assert collapse == "960px"
    start = css.index(f"@media (max-width: {collapse})")
    end = css.find("\n@media", start + 1)
    block = css[start:end if end != -1 else len(css)]
    assert ".topnav-toggle" in block
    assert ".topbar.is-open .topnav" in block
    assert ".floating-nav" not in css


def test_app_js_has_menu_toggle_and_no_floating_nav(client):
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "data-topnav-toggle" in js
    assert "data-floating-nav" not in js
    assert "--topnav-collapse" in js and "matchMedia" in js


def test_ingress_redirects_in_demo(client, monkeypatch):
    monkeypatch.setattr(ingress_router, "DEMO_MODE", True)
    response = client.get("/ingress", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_settings_profile_block_not_nested_card(client, configured_default):
    text = client.get("/settings").text
    assert 'class="profile-block" id="profile-1"' in text
    assert 'class="card top-gap" id="profile-1"' not in text


def test_sign_out_never_shrinks(client):
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.topnav-identity a \{[^}]*flex: none", css)
    strong = re.search(r"\.topnav-identity strong \{([^}]*)\}", css).group(1)
    assert "text-overflow: ellipsis" in strong and "overflow: hidden" in strong


def _media_block(css: str, query: str) -> str:
    """The body of the first ``@media {query}`` block, up to its matching closing brace."""
    start = css.index("{", css.index(f"@media {query}")) + 1
    depth = 1
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start:index]
    raise AssertionError(f"unclosed @media {query}")


def test_signed_in_label_visually_hidden_not_display_none():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    block = _media_block(css, "(min-width: 961px) and (max-width: 1200px)")
    rule = re.search(r"\.topnav-identity-label \{([^}]*)\}", block).group(1)
    assert "clip" in rule
    assert "display: none" not in rule


def test_switcher_wraps_below_bar_on_narrow_screens():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    block = _media_block(css, "(max-width: 960px)")
    rule = re.search(r"\.profile-switcher \{([^}]*)\}", block).group(1)
    assert "flex: 1 1 100%" in rule


def _top_level_rule(css: str, selector: str) -> str:
    """The body of the first rule for ``selector`` outside any @media block."""
    depth = 0
    index = 0
    while index < len(css):
        if depth == 0 and css.startswith(selector + " {", index) and (index == 0 or css[index - 1] == "\n"):
            start = index + len(selector) + 2
            return css[start:css.index("}", start)]
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
        index += 1
    raise AssertionError(f"no top-level rule for {selector}")


def test_switcher_select_keeps_readable_min_width():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    rule = _top_level_rule(css, ".profile-switcher select")
    match = re.search(r"min-width:\s*([\d.]+)rem", rule)
    assert match and float(match.group(1)) >= 6


def test_version_tag_never_hidden_without_switcher():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "display: none" in body and ".topbar-brand sub" in selector:
            assert ".has-profile-switcher" in selector, selector
    # Nor is the bar tightened for single-profile installs.
    block = _media_block(css, "(min-width: 961px) and (max-width: 1200px)")
    for selector, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
        if ".topbar-inner" in selector or ".topnav-link" in selector:
            assert ".has-profile-switcher" in selector, selector
