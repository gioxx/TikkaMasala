"""Accessibility contracts of the rendered pages (WCAG 2.2 AA): structure, names and states."""

from __future__ import annotations

import re

import pytest

import app.tunnels as tunnels

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
