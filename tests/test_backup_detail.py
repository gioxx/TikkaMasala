"""Backup detail page: header, notes, origin, links and escaping."""

from __future__ import annotations

import pytest

from app.profiles import update_profile

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"
CLIENT = "fedcba9876543210fedcba9876543210"


def _meta(text: str) -> str:
    """The header facts block (``<dl class="meta-grid">`` … ``</dl>``) of a detail page."""
    start = text.index('<dl class="meta-grid')
    return text[start : text.index("</dl>", start)]


def test_detail_shows_notes_origin_and_links(client, configured_default, make_backup):
    backup_id = make_backup(T1, account_id=GOOD, origin="automatic", notes="pre-upgrade <b>")
    text = client.get(f"/backup/{backup_id}").text
    assert "pre-upgrade &lt;b&gt;" in text
    assert ">Automatic<" in _meta(text)
    assert 'href="/automation/runs"' in text
    assert f'href="/backup/{backup_id}/download"' in text
    assert "<span>Default</span>" in _meta(text)
    assert 'aria-current="page"' in text and 'href="/backups"' in text


def test_detail_manual_has_no_run_history_link(client, configured_default, make_backup):
    backup_id = make_backup(T1, account_id=GOOD, origin="manual")
    assert 'href="/automation/runs"' not in client.get(f"/backup/{backup_id}").text


def test_detail_created_notice(client, configured_default, make_backup):
    backup_id = make_backup(T1, account_id=GOOD)
    assert f"Backup #{backup_id} saved." in client.get(f"/backup/{backup_id}?created=1").text
    assert f"Backup #{backup_id} saved." not in client.get(f"/backup/{backup_id}").text


def test_detail_tunnel_name_escaped(client, make_backup):
    backup_id = make_backup(tunnel_name="<script>x</script>")
    text = client.get(f"/backup/{backup_id}").text
    assert "&lt;script&gt;" in text
    assert "<script>x" not in text


def test_detail_other_account_links_other_scope(client, make_backup):
    backup_id = make_backup(account_id=OTHER)
    assert 'href="/backups?scope=other"' in client.get(f"/backup/{backup_id}").text


def test_detail_account_cell(client, second_profile, make_backup):
    backup_id = make_backup(T1, account_id=CLIENT)
    update_profile(second_profile.id, api_token=None)
    meta = _meta(client.get(f"/backup/{backup_id}").text)
    assert "Client" in meta
    assert "(profile credentials incomplete)" in meta


def test_detail_account_cell_complete_has_no_hint(client, second_profile, make_backup):
    backup_id = make_backup(T1, account_id=CLIENT)
    meta = _meta(client.get(f"/backup/{backup_id}").text)
    assert "Client" in meta
    assert "credentials incomplete" not in meta


def test_detail_other_account_cell(client, make_backup):
    meta = _meta(client.get(f"/backup/{make_backup(account_id=OTHER)}").text)
    assert "Other account" in meta
    assert "credentials incomplete" not in meta
