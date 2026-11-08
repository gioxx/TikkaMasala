from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app import ingress_overview
from app.ingress import IngressRule
from app.ingress_overview import TunnelSource, build_overview


def rule(hostname, service="http://10.0.0.1:80", path=None, catch_all=False):
    return IngressRule(hostname=hostname, service=service, path=path, is_catch_all=catch_all)


def test_build_overview_sorts_by_hostname_with_catch_all_last():
    overview = build_overview(
        "snapshot",
        [
            (TunnelSource("t1", "Home"), [rule("b.example.com"), rule(None, "http_status:404", catch_all=True)]),
            (TunnelSource("t2", "Office"), [rule("a.example.com")]),
        ],
    )
    assert [row.rule.hostname for row in overview.rows] == ["a.example.com", "b.example.com", None]
    assert overview.rows[1].position == 1
    assert overview.rows[2].position == 2
    assert overview.hostname_count == 2


def test_build_overview_flags_hostname_shared_between_tunnels():
    overview = build_overview(
        "live",
        [
            (TunnelSource("t1", "Home"), [rule("App.example.com"), rule("only.example.com")]),
            (TunnelSource("t2", "Office"), [rule("app.example.com")]),
        ],
    )
    shared = {row.tunnel_name: row.shared_with for row in overview.rows if row.shared_with}
    assert shared == {"Home": ["Office"], "Office": ["Home"]}
    assert overview.shared_hostnames == ["app.example.com"]


def test_same_hostname_twice_in_one_tunnel_is_not_shared():
    overview = build_overview(
        "snapshot",
        [(TunnelSource("t1", "Home"), [rule("app.example.com", path="/api"), rule("app.example.com")])],
    )
    assert all(not row.shared_with for row in overview.rows)


def test_row_link_and_search_text():
    overview = build_overview(
        "snapshot",
        [(TunnelSource("t1", "Home"), [rule("app.example.com"), rule("*.example.com"), rule(None, "http_status:404")])],
    )
    links = {row.rule.hostname: row.link for row in overview.rows}
    assert links == {"app.example.com": "https://app.example.com", "*.example.com": None, None: None}
    assert "home" in overview.rows[0].search_text
    assert "10.0.0.1" in overview.rows[0].search_text


def test_fragile_count():
    fragile = IngressRule(hostname="x.example.com", service="http://localhost:1", path=None, is_fragile=True)
    overview = build_overview("snapshot", [(TunnelSource("t1", "Home"), [fragile, rule("y.example.com")])])
    assert overview.fragile_count == 1


def test_snapshot_overview_uses_latest_backup_per_tunnel(make_backup):
    make_backup(tunnel_id="snap-1", tunnel_name="Old", ingress=[{"hostname": "old.example.com", "service": "http://a"}])
    latest = make_backup(
        tunnel_id="snap-1",
        tunnel_name="New",
        ingress=[{"hostname": "new.example.com", "service": "http://b"}],
        created_at="2026-02-01T00:00:00+00:00",
    )
    overview = ingress_overview.snapshot_overview()
    hostnames = [row.rule.hostname for row in overview.rows if row.tunnel_id == "snap-1"]
    assert hostnames == ["new.example.com"]
    tunnel = next(t for t in overview.tunnels if t.tunnel_id == "snap-1")
    assert tunnel.backup_id == latest
    assert tunnel.snapshot_at == "2026-02-01T00:00:00+00:00"


def test_live_overview(monkeypatch):
    async def fake_list_tunnels(account_id, api_token):
        return [
            {"id": "t1", "name": "Home"},
            {"id": "t2", "name": "Gone", "deleted_at": "2026-01-01T00:00:00Z"},
            {"id": "t3", "name": "Local"},
            {"id": "t4", "name": "Broken"},
        ]

    async def fake_get(account_id, path, api_token):
        if path == "cfd_tunnel/t1/configurations":
            return {"success": True, "result": {"config": {"ingress": [
                {"hostname": "live.example.com", "service": "http://10.0.0.9:80"},
                {"service": "http_status:404"},
            ]}}}
        if path == "cfd_tunnel/t3/configurations":
            return {"success": True, "result": {"config": None}}
        raise HTTPException(status_code=400, detail="Authentication error (code 10000)")

    monkeypatch.setattr(ingress_overview, "list_tunnels", fake_list_tunnels)
    monkeypatch.setattr(ingress_overview, "cloudflare_get", fake_get)

    overview = asyncio.run(ingress_overview.live_overview("acc", "token"))
    assert overview.source == "live"
    assert overview.error is None
    assert [row.rule.hostname for row in overview.rows] == ["live.example.com", None]
    by_id = {t.tunnel_id: t for t in overview.tunnels}
    assert "t2" not in by_id
    assert by_id["t3"].note
    assert not [row for row in overview.rows if row.tunnel_id == "t3"]
    assert by_id["t4"].error == "Authentication error (code 10000)"
    assert overview.generated_at


def test_live_overview_reports_listing_failure(monkeypatch):
    async def failing_list(account_id, api_token):
        raise HTTPException(status_code=400, detail="Invalid API token")

    monkeypatch.setattr(ingress_overview, "list_tunnels", failing_list)
    overview = asyncio.run(ingress_overview.live_overview("acc", "token"))
    assert overview.rows == []
    assert "Invalid API token" in overview.error


def test_ingress_page_snapshot(client, make_backup):
    make_backup(tunnel_id="page-1", tunnel_name="PageTunnel", ingress=[
        {"hostname": "page.example.com", "service": "http://localhost:8080"},
        {"service": "http_status:404"},
    ])
    response = client.get("/ingress")
    assert response.status_code == 200
    body = response.text
    assert "<title>Tikka Masala · Hostnames</title>" in body
    assert 'href="https://page.example.com"' in body
    assert "http://localhost:8080" in body
    assert "PageTunnel" in body
    assert "/static/vendor/htmx.min.js" in body
    assert response.headers["vary"] == "HX-Request"


def test_ingress_page_htmx_returns_partial(client, make_backup):
    make_backup(tunnel_id="page-2", tunnel_name="Partial")
    response = client.get("/ingress", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div id="ingress-results"')
    assert "<html" not in response.text


def test_ingress_page_live_without_credentials(client):
    response = client.get("/ingress?source=live")
    assert response.status_code == 200
    assert "needs an Account ID and an API token" in response.text


def test_dashboard_links_to_ingress_page(client):
    assert 'href="/ingress"' in client.get("/").text


# --- review follow-ups -------------------------------------------------------


def test_hostname_shared_between_tunnels_even_with_different_paths():
    # DNS routes by hostname, so only one of these tunnels can ever receive traffic.
    overview = build_overview(
        "live",
        [
            (TunnelSource("t1", "Home"), [rule("app.example.com", path="^/api")]),
            (TunnelSource("t2", "Office"), [rule("app.example.com", path="^/web")]),
        ],
    )
    assert {row.tunnel_name: row.shared_with for row in overview.rows} == {"Home": ["Office"], "Office": ["Home"]}


def test_rows_carry_their_tunnel_account():
    overview = build_overview(
        "snapshot",
        [
            (TunnelSource("t1", "Old", account_id="acc-old"), [rule("a.example.com")]),
            (TunnelSource("t2", "New", account_id="acc-new"), [rule("b.example.com")]),
        ],
    )
    assert {row.tunnel_name: row.account_id for row in overview.rows} == {"Old": "acc-old", "New": "acc-new"}


def test_snapshot_rows_use_backup_account(make_backup):
    make_backup(tunnel_id="acct-1", tunnel_name="Acct", ingress=[{"hostname": "acct.example.com", "service": "http://a"}])
    row = next(r for r in ingress_overview.snapshot_overview().rows if r.tunnel_id == "acct-1")
    assert row.account_id == "acc-1"


def test_pathed_rule_without_hostname_is_not_catch_all(client, make_backup):
    make_backup(tunnel_id="path-1", tunnel_name="Paths", ingress=[
        {"path": "^/health", "service": "http://10.0.0.3:9000"},
        {"service": "http_status:404"},
    ])
    body = client.get("/ingress").text
    start = body.index('<tr data-search="any hostname ^/health')
    row = body[start: body.index("</tr>", start)]
    assert 'data-catch-all="false"' in row
    assert "any hostname" in row


def test_live_overview_handles_transport_errors(monkeypatch):
    async def timeout(account_id, api_token):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(ingress_overview, "list_tunnels", timeout)
    overview = asyncio.run(ingress_overview.live_overview("acc", "token"))
    assert overview.rows == []
    assert "Could not load tunnels" in overview.error


def test_list_tunnels_follows_pagination(monkeypatch):
    from app import cloudflare

    calls = []

    async def fake_get(account_id, path, api_token):
        calls.append(path)
        page = int(path.split("page=")[1].split("&")[0])
        results = {1: [{"id": "a"}, {"id": "b"}], 2: [{"id": "c"}]}[page]
        return {"success": True, "result": results, "result_info": {"page": page, "total_pages": 2}}

    monkeypatch.setattr(cloudflare, "cloudflare_get", fake_get)
    tunnels = asyncio.run(cloudflare.list_tunnels("acc", "token"))
    assert [t["id"] for t in tunnels] == ["a", "b", "c"]
    assert len(calls) == 2


def test_list_tunnels_single_page_without_result_info(monkeypatch):
    from app import cloudflare

    async def fake_get(account_id, path, api_token):
        return {"success": True, "result": [{"id": "only"}]}

    monkeypatch.setattr(cloudflare, "cloudflare_get", fake_get)
    assert [t["id"] for t in asyncio.run(cloudflare.list_tunnels("acc", "token"))] == ["only"]


# --- local review follow-ups -------------------------------------------------


def test_origin_settings_tooltip_is_escaped(client, make_backup):
    make_backup(tunnel_id="esc-1", tunnel_name="Esc", ingress=[
        {"hostname": "esc.example.com", "service": "http://a", "originRequest": {"httpHostHeader": 'x" onmouseover="alert(1)'}},
        {"service": "http_status:404"},
    ])
    body = client.get("/ingress").text
    assert 'onmouseover="alert(1)' not in body
    assert "httpHostHeader=&#34;x" in body


def test_history_restore_request_gets_full_page(client):
    response = client.get("/ingress", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert "<html" in response.text


def test_source_switch_does_not_push_history(client):
    assert "hx-push-url" not in client.get("/ingress").text


def test_list_tunnels_excludes_deleted(monkeypatch):
    from app import cloudflare

    paths = []

    async def fake_get(account_id, path, api_token):
        paths.append(path)
        return {"success": True, "result": []}

    monkeypatch.setattr(cloudflare, "cloudflare_get", fake_get)
    asyncio.run(cloudflare.list_tunnels("acc", "token"))
    assert "is_deleted=false" in paths[0]


def test_snapshot_shared_warning_suggests_live_check(client, make_backup):
    make_backup(tunnel_id="dup-a", tunnel_name="DupA", ingress=[{"hostname": "dup.example.com", "service": "http://a"}])
    make_backup(tunnel_id="dup-b", tunnel_name="DupB", ingress=[{"hostname": "dup.example.com", "service": "http://b"}])
    body = client.get("/ingress").text
    assert "<strong>Hostname served by more than one tunnel.</strong>" in body
    assert "also in DupB" in body
    assert "check with <em>Live from Cloudflare</em>" in body


def test_undecodable_snapshot_marks_tunnel_unreadable(make_backup, data_dir):
    import sqlite3
    from contextlib import closing

    backup_id = make_backup(tunnel_id="bad-1", tunnel_name="Bad")
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        filename = conn.execute("SELECT filename FROM backups WHERE id = ?", (backup_id,)).fetchone()[0]
    (data_dir / "backups" / filename).write_bytes(b"\xff\xfe\x00 not utf-8")
    tunnel = next(t for t in ingress_overview.snapshot_overview().tunnels if t.tunnel_id == "bad-1")
    assert tunnel.error == "Snapshot file could not be read."
