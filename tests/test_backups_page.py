"""Backups page: profile / other-account archive, filters, notes, pagination and retention."""

from __future__ import annotations

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"


def _seed(make_backup):
    home = make_backup(T1, "Home", account_id=GOOD, created_at="2026-01-01T00:00:00+00:00")
    renamed = make_backup(
        T1, "Home renamed", account_id=GOOD.upper(), origin="automatic", created_at="2026-01-02T00:00:00+00:00"
    )
    lab = make_backup(T2, "Lab", account_id=GOOD, notes="before upgrade")
    old = make_backup("t-old", "Old", account_id=OTHER)
    return home, renamed, lab, old


def test_backups_page_profile_scope(client, configured_default, make_backup):
    _home, _renamed, lab, old = _seed(make_backup)
    response = client.get("/backups")
    assert response.status_code == 200
    text = response.text
    assert 'id="saved-backups-section"' in text
    assert f'id="backup-row-{lab}"' in text
    assert f'id="backup-row-{old}"' not in text
    assert "Default (3)" in text
    assert 'href="/backups?scope=other"' in text
    assert "Other accounts (1)" in text


def test_backups_page_other_scope(client, configured_default, make_backup):
    _home, _renamed, lab, old = _seed(make_backup)
    text = client.get("/backups?scope=other").text
    assert f'id="backup-row-{old}"' in text
    assert f'id="backup-row-{lab}"' not in text
    assert '<a href="/backups?scope=other" aria-current="page">' in text
    assert '<a href="/backups">' in text


def test_backups_other_tab_hidden_without_other_backups(client, configured_default, make_backup):
    make_backup(T1, "Home", account_id=GOOD)
    text = client.get("/backups").text
    assert "Other accounts (" not in text


def test_backups_filters_and_origin_pill(client, configured_default, make_backup):
    home, renamed, lab, _old = _seed(make_backup)
    text = client.get("/backups?origin=automatic").text
    assert f'id="backup-row-{renamed}"' in text
    assert f'id="backup-row-{home}"' not in text
    assert f'id="backup-row-{lab}"' not in text
    assert ">Automatic<" in text
    assert f'value="{T1}"' in text
    assert "Clear filters" in text


def test_backups_pagination_keeps_filters(client, configured_default, make_backup):
    # 5 is the default page size (omitted from URLs), so use 10 to see it carried.
    for _ in range(12):
        make_backup(T1, account_id=GOOD, origin="automatic")
    make_backup(T1, account_id=GOOD, origin="manual")
    text = client.get(f"/backups?tunnel={T1}&origin=automatic&page_size=10").text
    assert f'href="/backups?tunnel={T1}&amp;origin=automatic&amp;page=2&amp;page_size=10"' in text
    assert "Showing 1–10 of 12 backup(s)" in text
    assert '<input type="hidden" name="page_size" value="10">' in text
    default = client.get(f"/backups?tunnel={T1}&origin=automatic").text
    assert f'href="/backups?tunnel={T1}&amp;origin=automatic&amp;page=2"' in default


def test_backups_notes_column(client, configured_default, make_backup):
    _seed(make_backup)
    text = client.get("/backups").text
    # Truncated on screen; the full note is a hover / keyboard-focus tooltip. The text itself is
    # the only copy screen readers get (no title, which would be announced a second time).
    assert (
        '<span class="archive-notes-tip" tabindex="0" data-tooltip="before upgrade" data-tooltip-align="start">'
        '<span class="archive-notes">before upgrade</span></span>'
    ) in text
    assert 'title="before upgrade"' not in text


def test_backups_notes_tooltip_escapes_the_note(client, configured_default, make_backup):
    make_backup(T1, account_id=GOOD, notes='say "hi" <b>')
    text = client.get("/backups").text
    assert 'data-tooltip="say &#34;hi&#34; &lt;b&gt;"' in text


def test_backups_row_actions_are_icon_links_with_tooltips(client, configured_default, make_backup):
    _home, _renamed, lab, _old = _seed(make_backup)
    text = client.get("/backups").text
    view = (
        f'<a class="icon-action" href="/backup/{lab}" aria-label="View backup #{lab}" data-tooltip="View">'
    )
    download = (
        f'<a class="icon-action" href="/backup/{lab}/download" aria-label="Download backup #{lab}"'
        ' data-tooltip="Download">'
    )
    assert view in text
    assert download in text
    row = text.split(f'id="backup-row-{lab}"', 1)[1].split("</tr>", 1)[0]
    actions = row.split('data-label="Actions"', 1)[1]
    assert actions.count("<svg") == 2
    assert actions.count('aria-hidden="true"') == 2
    assert ">View<" not in actions and ">Download<" not in actions


def _css_rules() -> list[tuple[str, str]]:
    import re
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_tooltip_pattern_shows_on_hover_and_keyboard_focus():
    rules = _css_rules()
    shown = [sel for sel, body in rules if "visibility: visible" in body and "[data-tooltip]" in sel]
    assert shown, "no rule reveals [data-tooltip]"
    assert any(":hover::after" in sel and ":focus-visible::after" in sel for sel in shown)
    base = next(body for sel, body in rules if sel == "[data-tooltip]::after")
    assert "content: attr(data-tooltip)" in base
    # Alt text "" keeps the generated copy out of the accessibility tree (no double reading).
    assert 'content: attr(data-tooltip) / "";' in base
    assert "max-width: min(" in base and "100vw" in base
    assert "var(--" in base


def test_icon_actions_meet_target_sizes():
    import re
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    bodies = [body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css) if sel.strip().endswith(".icon-action")]
    assert any("width: 2rem" in b and "height: 2rem" in b for b in bodies)  # 32px
    phone = css.split("@media (max-width: 640px)", 1)[1]
    assert re.search(r"\.icon-action\s*\{[^}]*width: 2\.75rem;[^}]*height: 2\.75rem;", phone)  # 44px


def test_backups_empty_states(client, configured_default, make_backup):
    _seed(make_backup)
    text = client.get("/backups?origin=manual&tunnel=nope").text
    assert "No backups match these filters." in text
    assert "Clear filters" in text


def test_backups_empty_without_account(client, make_backup):
    make_backup(T1, "Home", account_id=GOOD)
    text = client.get("/backups").text
    assert 'Set the Account ID in <a href="/settings">Settings</a> to see this profile\'s backups.' in text


def test_backups_empty_no_backups(client, configured_default):
    assert "No backups yet." in client.get("/backups").text


def test_backups_garbage_params(client, configured_default, make_backup):
    _seed(make_backup)
    response = client.get("/backups?page=999&page_size=7&origin=evil&scope=x&tunnel=%3Cscript%3E")
    assert response.status_code == 200
    # base.html has its own inline <script> blocks: the crafted value must add none.
    assert response.text.count("<script>") == client.get("/backups").text.count("<script>")
    assert "&lt;script&gt;" in response.text
    assert "No backups match these filters." in response.text


def test_backups_page_beyond_last_clamps(client, configured_default, make_backup):
    _home, _renamed, lab, _old = _seed(make_backup)
    text = client.get("/backups?page=999").text
    assert f'id="backup-row-{lab}"' in text


def test_backups_tunnel_name_escaped(client, configured_default, make_backup):
    make_backup(T1, "<img src=x onerror=alert(1)>", account_id=GOOD)
    text = client.get("/backups").text
    assert "&lt;img src=x" in text
    assert "<img src=x" not in text


def test_backups_retention_panel(client, configured_default, make_backup):
    _seed(make_backup)
    text = client.get("/backups").text
    assert 'id="backup-retention-status"' in text
    assert "Other accounts:" in text
    assert 'href="/automation#retention-section"' in text
    assert "Default: Kept until deleted (no retention set)" in text


def test_backups_retention_panel_shows_source(client, configured_default, make_backup):
    from app.profiles import DEFAULT_PROFILE_ID, update_profile
    update_profile(DEFAULT_PROFILE_ID, retention_days=30)
    text = client.get("/backups").text
    assert "Default: Kept for 30 days (set on this profile)" in text


def test_backups_links(client, configured_default):
    text = client.get("/backups").text
    assert 'href="/backups/new"' in text
    assert 'href="/automation/runs"' in text


def test_backups_demo_redirects(client, monkeypatch):
    monkeypatch.setattr("app.routers.backups.DEMO_MODE", True)
    response = client.get("/backups", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
