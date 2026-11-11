"""Accessibility contracts of the rendered pages (WCAG 2.2 AA): structure, names and states."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.tunnels as tunnels

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
GOOD = "0123456789abcdef0123456789abcdef"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
TUNNELS = [
    {"id": T1, "name": "Home", "status": "healthy"},
    {"id": T2, "name": "Lab", "status": "down"},
]

PAGES = [
    ("/", "Tunnels"),
    ("/ingress", "Hostnames"),
    ("/backups", "Backups"),
    ("/backups/new", "Back up with a note"),
    ("/automation", "Automation"),
    ("/automation/tunnels", "Advanced backup schedule"),
    ("/automation/runs", "Run history"),
    ("/automation/destinations/new?kind=webhook", "Add webhook"),
    ("/automation/destinations/new?kind=telegram", "Add Telegram"),
    ("/settings", "Settings"),
    ("/settings/notifications", "Notification messages"),
]


@pytest.fixture
def populated(client, configured_default, make_backup, monkeypatch):
    async def _list(account_id, api_token):
        return TUNNELS

    monkeypatch.setattr(tunnels, "list_tunnels", _list)
    tunnels.clear_tunnel_list_cache()
    ids = [
        make_backup(T1, "Home", account_id=GOOD, notes="before the upgrade"),
        make_backup(T2, "Lab", account_id=GOOD, origin="automatic"),
    ]
    return ids


def _pages(client, populated):
    for path, title in PAGES:
        yield path, title, client.get(path)
    yield f"/backup/{populated[0]}", f"Backup #{populated[0]}", client.get(f"/backup/{populated[0]}")


def _h1s(text: str) -> list[str]:
    return re.findall(r"<h1\b", text)


def test_every_page_has_one_h1_and_a_consistent_title(client, populated):
    for path, title, response in _pages(client, populated):
        assert response.status_code == 200, path
        assert len(_h1s(response.text)) == 1, path
        assert f"<title>{title} · Tikka Masala</title>" in response.text, path


def test_skip_link_main_and_footer_landmarks(client, populated):
    for path, _title, response in _pages(client, populated):
        text = response.text
        body = text.split("<body>", 1)[1].lstrip()
        assert body.startswith('<a class="skip-link" href="#main">Skip to content</a>'), path
        assert text.count('<main id="main" class="container" tabindex="-1"') == 1, path
        # The footer is its own contentinfo landmark: after </main>, not inside it.
        assert text.index("</main>") < text.index('<footer class="container site-footer-region">'), path


def test_skip_link_is_hidden_until_focused():
    from tests.css_helpers import declarations, load_css, rule_body, without_at_rules

    flat = without_at_rules(load_css())
    assert "translateY(-200%)" in declarations(rule_body(flat, ".skip-link"))["transform"]
    assert declarations(rule_body(flat, ".skip-link:focus"))["transform"] == "none"


def test_inline_svgs_are_decorative(client, populated):
    for path, _title, response in _pages(client, populated):
        for tag in re.findall(r"<svg\b[^>]*>", response.text):
            assert 'aria-hidden="true"' in tag and 'focusable="false"' in tag, (path, tag[:80])


def test_new_tab_links_say_so(client, populated):
    for path, _title, response in _pages(client, populated):
        for match in re.finditer(r'<a\b[^>]*target="_blank"[^>]*>(.*?)</a>', response.text, re.S):
            tag = match.group(0)
            assert "(opens in a new tab)" in tag, (path, tag[:120])


def test_tables_have_captions_and_column_headers(client, populated):
    seen = 0
    for path, _title, response in _pages(client, populated):
        for table in re.findall(r"<table\b.*?</table>", response.text, re.S):
            seen += 1
            assert re.search(r"<caption\b[^>]*>[^<]+</caption>", table), path
            for th in re.findall(r"<th\b[^>]*>", table.split("</thead>", 1)[0]):
                assert 'scope="col"' in th, (path, th)
    assert seen >= 4


def test_backups_panels_follow_the_page_heading(client, populated):
    text = client.get("/backups").text
    assert "<h2>Archive statistics</h2>" in text and "<h2>Archive retention</h2>" in text
    assert "<h3>" not in text


# --- Top bar, focus and scrolling (B1, S1, S5, M11, M12, m4, m15, m16) ---

def _css():
    from tests.css_helpers import load_css

    return load_css()


def test_topbar_sticks_only_on_wide_and_tall_windows():
    from tests.css_helpers import declarations, media_block, rule_body, without_at_rules

    css = _css()
    base = declarations(rule_body(without_at_rules(css), ".topbar"))
    assert base["position"] == "relative"
    sticky = media_block(css, "(min-width: 60.0625em) and (min-height: 31.3125em)")
    assert declarations(rule_body(sticky, ".topbar"))["position"] == "sticky"
    assert declarations(rule_body(sticky, ":root"))["--topbar-h"] == "3.5rem"
    # Nowhere else does the bar stick.
    assert css.count("position: sticky") == 1
    collapse = media_block(css, "(max-width: 60em)")
    menu = declarations(rule_body(collapse, ".topbar.is-open .topnav"))
    assert menu["overflow-y"] == "auto" and "100dvh" in menu["max-height"]


def test_scroll_padding_follows_the_measured_topbar_and_reduced_motion():
    from tests.css_helpers import declarations, media_block, rule_body, without_at_rules

    css = _css()
    html = declarations(rule_body(without_at_rules(css), "html"))
    assert html["scroll-padding-top"] == "calc(var(--topbar-h, 0px) + 1rem)"
    assert html["scroll-padding-bottom"] == "4.5rem"
    assert "scroll-behavior" not in html
    assert "smooth" in rule_body(media_block(css, "(prefers-reduced-motion: no-preference)"), "html")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "new ResizeObserver(syncTopbarHeight)" in js
    assert 'setProperty("--topbar-h"' in js
    assert "reducedMotionQuery.matches" in js
    templates = STATIC_DIR.parent / "templates"
    for template in templates.rglob("*.html"):
        assert ", 68)" not in template.read_text(encoding="utf-8"), template.name
    assert ", 68," not in js


def test_focus_ring_is_an_outline_everywhere():
    from tests.css_helpers import declarations, rule_body, rules, without_at_rules

    css = _css()
    for selector, body in rules(css):
        if "outline: none" in body:
            assert selector == '[tabindex="-1"]:focus', selector
    ring = declarations(rule_body(without_at_rules(css), ":focus-visible, select:focus, input:focus, textarea:focus"))
    assert ring["outline"] == "2px solid var(--focus)"


def test_menu_button_has_a_static_name_and_closes_on_focus_out(client):
    text = client.get("/").text
    assert 'aria-expanded="false" aria-controls="topnav-menu" aria-label="Menu"' in text
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '"Close menu"' not in js and '"Open menu"' not in js
    assert 'topbar.addEventListener("focusout"' in js and "!topbar.contains(event.relatedTarget)" in js


def test_back_to_top_and_in_page_links_move_focus():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "focusElement(mainContent);" in js
    assert "element.focus({ preventScroll: true })" in js
    assert "revealElement(target" in js


def test_brand_link_does_not_fill_the_collapsed_bar():
    from tests.css_helpers import declarations, media_block, rule_body

    collapse = media_block(_css(), "(max-width: 60em)")
    assert declarations(rule_body(collapse, ".topbar-brand"))["flex"] == "0 1 auto"


def test_profile_switcher_label_visible_when_collapsed(client, second_profile):
    from tests.css_helpers import declarations, media_block, rule_body

    text = client.get("/").text
    assert '<span class="sr-only profile-switcher-label">Profile</span>' in text
    collapse = media_block(_css(), "(max-width: 60em)")
    label = declarations(rule_body(collapse, ".profile-switcher .profile-switcher-label"))
    assert label["position"] == "static" and label["clip"] == "auto"
