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
T3 = "33333333-3333-4333-8333-333333333333"
T4 = "44444444-4444-4444-8444-444444444444"
TUNNELS = [
    {"id": T1, "name": "Home", "status": "healthy"},
    {"id": T2, "name": "Lab", "status": "down"},
    {"id": T3, "name": "Media", "status": "degraded"},
    {"id": T4, "name": "Old", "status": "inactive"},
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


# --- Tables, tooltips and pagination (review #122 items 1, 4, 9; S7, M1, M10, m3) ---

def _row(text: str, tunnel_id: str) -> str:
    return text.split(f'id="tunnel-row-{tunnel_id}"', 1)[1].split("</tr>", 1)[0]


def test_tunnel_rows_icon_button_and_status_pills(client, populated):
    text = client.get("/").text
    for tunnel_id, name, status, tone in [
        (T1, "Home", "healthy", "match"),
        (T2, "Lab", "down", "warn"),
        (T3, "Media", "degraded", "alt"),
        (T4, "Old", "inactive", "muted"),
    ]:
        row = _row(text, tunnel_id)
        assert f'aria-label="Back up {name}" data-tooltip="Back up"' in row
        assert f'<span class="status-pill status-{tone}"><span class="status-label">{status}</span></span>' in row
        # No title on the pill: it only repeated the visible word.
        assert "title=" not in row.split('class="col-status"', 1)[1].split("</td>", 1)[0]


def test_row_feedback_is_as_wide_as_its_message():
    from tests.css_helpers import declarations, rule_body, without_at_rules

    rule = declarations(rule_body(without_at_rules(_css()), ".row-feedback"))
    assert rule["width"] == "max-content" and rule["max-width"] == "16rem"


def test_scrollable_regions_are_keyboard_reachable(client, populated):
    detail = client.get(f"/backup/{populated[0]}").text
    assert '<pre class="json-view" tabindex="0" role="region" aria-label="Snapshot JSON">' in detail
    for path, label in [("/backups", "Backups"), ("/automation/runs", "Run history"), ("/", "Tunnels")]:
        text = client.get(path).text
        if "<table" in text:
            assert f'data-scroll-region="{label}"' in text, path
    for wrap in re.findall(r'<div class="(?:tunnel-)?table-wrap[^"]*"[^>]*>', detail + client.get("/backups").text):
        assert "data-scroll-region=" in wrap, wrap
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "region.scrollWidth > region.clientWidth" in js
    assert 'region.setAttribute("role", "region")' in js and "region.tabIndex = 0" in js


def test_page_size_has_an_apply_button_and_pagination_is_a_nav(client, populated, make_backup):
    for _ in range(7):
        make_backup(T1, "Home", account_id=GOOD)
    for path in ["/backups", "/automation/runs"]:
        text = client.get(path).text
        assert "onchange" not in text, path
        assert "aria-disabled" not in text, path
    text = client.get("/backups").text
    form = text.split('class="page-size-form"', 1)[1].split("</form>", 1)[0]
    assert '<button type="submit" class="secondary-button">Apply</button>' in form
    assert '<nav class="pagination-actions" aria-label="Pagination">' in text


# --- Status and forced colours (S2, S8, M7; review #122 item 2) ---

def test_phone_status_keeps_its_word():
    from tests.css_helpers import declarations, media_block, rule_body, rules

    phone = media_block(_css(), "(max-width: 40em)")
    for selector, body in rules(phone):
        if ".status-label" in selector:
            assert "clip" not in body and "position: absolute" not in body, selector
    pill = declarations(rule_body(phone, ".tunnel-table .col-status .status-pill"))
    assert "background" not in pill and pill["padding"] == "0 0.4rem"


def test_forced_colours_keep_dots_and_current_markers():
    from tests.css_helpers import declarations, media_block, rule_body

    forced = media_block(_css(), "(forced-colors: active)")
    dots = declarations(rule_body(forced, ".status-dot, .status-pill::before"))
    assert dots["forced-color-adjust"] == "none" and dots["background"] == "CanvasText"
    assert "Highlight" in rule_body(forced, '.topnav-link[aria-current="page"], .subnav a[aria-current="page"]')
    assert "Highlight" in rule_body(forced, ".theme-button.is-active, .source-switch a[aria-current]")


def test_long_ingress_pills_wrap():
    from tests.css_helpers import declarations, rule_body, without_at_rules

    rule = declarations(rule_body(without_at_rules(_css()), ".ingress-table .status-pill"))
    assert rule["white-space"] == "normal" and rule["overflow-wrap"] == "anywhere"


# --- htmx focus and live regions (S6, M2, m17) ---

def test_htmx_controls_have_stable_ids_for_refocus(client, populated):
    text = client.get("/").text
    assert 'id="backup-all"' in text
    assert f'id="backup-{T1}"' in text and f'id="backup-{T2}"' in text
    ingress = client.get("/ingress").text
    assert 'id="source-snapshot"' in ingress and 'id="source-live"' in ingress
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'addEventListener("htmx:beforeRequest"' in js and "htmxFocusId" in js
    assert "target.focus({ preventScroll: true })" in js


def test_htmx_swap_response_keeps_the_ids(client, populated):
    fragment = client.get("/ingress?source=live", headers={"HX-Request": "true"}).text
    assert 'id="source-live"' in fragment and 'aria-current="true"' in fragment
    assert 'aria-current="page"' not in fragment.split('class="source-switch"', 1)[1].split("</div>", 1)[0]


def test_ingress_status_region_outside_the_swapped_results(client, populated):
    text = client.get("/ingress").text
    status = text.index('<p id="ingress-status" class="sr-only" role="status"></p>')
    assert status < text.index('<div id="ingress-results"')
    # The loading indicator is visual only (its live region is #ingress-status).
    assert '<span id="ingress-loading" class="htmx-indicator hint" aria-hidden="true">' in text
    assert 'data-hostname-count="' in text and 'data-source="snapshot"' in text
    assert "of ${rows.length} rule" in text and "Live configuration loaded" in text


# --- Forms and errors (S3, M5, M6, M8, M9, m2, m7, m14, m18; review #122 item 7) ---

def test_frequency_selects_are_named_and_hidden_includes_disabled(client, populated):
    text = client.get("/automation/tunnels").text
    assert f'id="freq_{T1}" class="compact-select" aria-label="Backup frequency for Home"' in text
    # "All tunnels" mode: the include checkboxes are hidden and disabled.
    checkbox = text.split(f'id="include_{T1}"', 1)[1].split(">", 1)[0]
    assert "disabled" in checkbox
    assert "checkbox.disabled = !isSelected" in text


def test_invalid_cron_keeps_what_was_typed(client, populated):
    response = client.post(
        "/automation/schedule", data={"enabled": "on", "cron_expression": "every tuesday", "browser_timezone": ""}
    )
    assert response.status_code == 400
    text = response.text
    assert "<title>Error: Automation · Tikka Masala</title>" in text
    assert 'id="schedule-notice" role="alert" tabindex="-1" data-focus-notice' in text
    field = text.split('name="cron_expression"', 1)[1].split(">", 1)[0]
    assert 'value="every tuesday"' in field
    assert 'aria-invalid="true"' in field
    assert 'aria-describedby="schedule-notice cron-timezone-note cron-explanation"' in field
    enabled = text.split('name="enabled"', 1)[1].split(">", 1)[0]
    assert "checked" in enabled


def test_retention_error_marks_the_field(client, populated):
    text = client.post("/automation/retention", data={"retention_days": "-4"}).text
    field = text.split('name="retention_days"', 1)[1].split(">", 1)[0]
    assert 'aria-invalid="true"' in field and "retention-notice retention-hint" in field


def test_cron_hints_are_outside_the_label_and_announced(client, populated):
    text = client.get("/automation").text
    label = text.split("<span>Cron expression</span>", 1)[1].split("</label>", 1)[0]
    assert "<small" not in label and "cron-timezone-note\"" not in label.split("aria-describedby", 1)[0]
    assert '<p class="hint field-hint cron-timezone-note" id="cron-timezone-note">' in text
    assert '<p class="hint cron-explanation" id="cron-explanation" aria-live="polite"></p>' in text
    assert '<div class="preset-list" role="group" aria-label="Suggested cron expressions">' in text
    assert "data-notice-target" not in text


def test_form_results_receive_focus():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'document.querySelector("[data-focus-notice]")' in js
    assert "revealElement(formResultNotice)" in js


def test_destination_form_hints_are_visible_text(client, populated):
    telegram = client.get("/automation/destinations/new?kind=telegram").text
    assert 'aria-describedby="secret-hint"' in telegram and 'id="secret-hint"' in telegram
    assert 'aria-describedby="chat-id-hint"' in telegram and 'id="chat-id-hint"' in telegram
    response = client.post(
        "/automation/destinations", data={"kind": "webhook", "label": "W", "secret": "not a url", "events": []}
    )
    text = response.text
    assert "<title>Error: Add webhook · Tikka Masala</title>" in text
    field = text.split('name="secret"', 1)[1].split(">", 1)[0]
    assert 'aria-invalid="true"' in field and 'aria-describedby="destination-error secret-hint"' in field


def test_settings_credential_errors_mark_the_fields(client, populated):
    text = client.post("/settings/profiles/1/credentials", data={"account_id": "nope", "api_token": ""}).text
    assert "<title>Error: Settings · Tikka Masala</title>" in text
    assert 'id="profile-1-error" role="alert" tabindex="-1" data-focus-notice' in text
    for name in ("account_id", "api_token"):
        block = text.split('action="/settings/profiles/1/credentials"', 1)[1]
        field = block.split(f'name="{name}"', 1)[1].split(">", 1)[0]
        assert 'aria-invalid="true"' in field and "profile-1-error" in field, name
    assert 'id="api-token-hint-1">Leave empty to keep the current token.</p>' in text


def test_repeated_names_get_context(client, populated):
    text = client.get("/settings").text
    assert '<span class="sr-only"> of Default</span></summary>' in text


def test_notification_messages_use_event_names(client):
    text = client.get("/settings/notifications").text
    assert '<span class="notification-event-label">Manual backup completed</span>' in text
    assert 'role="group" aria-label="Placeholders for Manual backup completed"' in text
    assert '<span aria-hidden="true">↩</span> Use default<span class="sr-only"> for Manual backup completed</span>' in text
    assert "flex: 1" not in text


def test_long_codes_wrap_anywhere():
    from tests.css_helpers import declarations, rule_body, without_at_rules

    assert declarations(rule_body(without_at_rules(_css()), "code"))["overflow-wrap"] == "anywhere"
