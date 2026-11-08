"""Backup detail page: header, notes, origin, links and escaping."""

from __future__ import annotations

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"


def test_detail_shows_notes_origin_and_links(client, configured_default, make_backup):
    backup_id = make_backup(T1, account_id=GOOD, origin="automatic", notes="pre-upgrade <b>")
    text = client.get(f"/backup/{backup_id}").text
    assert "pre-upgrade &lt;b&gt;" in text
    assert ">Automatic<" in text
    assert 'href="/automation/runs"' in text
    assert f'href="/backup/{backup_id}/download"' in text
    assert ">Default<" in text
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
