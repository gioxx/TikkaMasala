"""Tunnels home page at ``/``: summary tiles and a live tunnel table."""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException

import app.credentials as credentials
import app.tunnels as tunnels
import app.web as web
from app.credentials import DEMO_NO_CREDENTIALS_MESSAGE

GOOD = "0123456789abcdef0123456789abcdef"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
T3 = "33333333-3333-4333-8333-333333333333"
T4 = "44444444-4444-4444-8444-444444444444"

TUNNEL_LIST = [
    {"id": T1, "name": "Home", "status": "healthy"},
    {"id": T2, "name": "Lab", "status": "degraded"},
    {"id": T3, "name": "Office", "status": "down"},
    {"id": T4, "name": "Old", "status": "inactive"},
]


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")


class _Recorder:
    def __init__(self, result=None, error: BaseException | None = None):
        self.calls: list[tuple] = []
        self.result = result if result is not None else []
        self.error = error

    async def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_list(monkeypatch):
    def _install(result=None, error: BaseException | None = None) -> _Recorder:
        recorder = _Recorder(result, error)
        monkeypatch.setattr(tunnels, "list_tunnels", recorder)
        return recorder

    return _install


@pytest.fixture
def seeded(make_backup):
    make_backup(
        T1,
        "Home",
        ingress=[
            {"hostname": "app.example.com", "service": "http://10.0.0.5:8080"},
            {"hostname": "local.example.com", "service": "http://localhost:3000"},
            {"service": "http_status:404"},
        ],
        created_at="2026-01-01T00:00:00+00:00",
        account_id=GOOD,
    )
    make_backup(
        T2,
        "Lab",
        ingress=[{"hostname": "lab.example.com", "service": "http://10.0.0.6"}, {"service": "http_status:404"}],
        account_id=GOOD.upper(),
    )
    make_backup("t-other", "Other", account_id="acc-other")


def _demo(monkeypatch):
    for module in (credentials, tunnels, web):
        monkeypatch.setattr(module, "DEMO_MODE", True)


def _row(text: str, tunnel_id: str) -> str:
    start = text.index(f'id="tunnel-row-{tunnel_id}"')
    return text[start:text.index("</tr>", start)]


def _tile(text: str, key: str) -> str:
    start = text.index(f'data-tile="{key}"')
    end = text.find('data-tile="', start + 1)
    return text[start:end if end != -1 else text.index("</section>", start)]


def test_home_shows_tiles_and_table(client, configured_default, seeded, fake_list):
    fake_list(TUNNEL_LIST)
    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    assert 'id="tunnels-home"' in text
    for key in ("tunnels", "hostnames", "auto_backup", "backups"):
        assert f'data-tile="{key}"' in text
    assert "1 degraded, 1 down" in text
    assert f'id="tunnel-row-{T1}"' in text
    assert f'hx-post="/tunnels/{T1}/backup"' in text
    assert 'hx-post="/tunnels/backup-all"' in text
    assert "Back up all now" in text
    assert f"https://dash.cloudflare.com/{GOOD}/tunnels/{T1}/overview" in text


def test_home_local_columns(client, configured_default, seeded, fake_list):
    fake_list(TUNNEL_LIST)
    text = client.get("/").text
    home_row = _row(text, T1)
    assert ">2<" in home_row
    assert '<time datetime="2026-01-01T00:00:00+00:00"' in home_row
    assert "Never" in _row(text, T3)


def test_home_cloudflare_failure_keeps_local_data(client, configured_default, seeded, fake_list):
    fake_list(error=HTTPException(status_code=502, detail="boom"))
    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    assert "Could not load tunnel status from Cloudflare: boom" in text
    assert f'id="tunnel-row-{T1}"' in text
    assert '<strong class="tile-value">3</strong>' in _tile(text, "hostnames")
    assert '<strong class="tile-value">2</strong>' in _tile(text, "backups")


def test_home_survives_unexpected_list_error(client, configured_default, fake_list):
    fake_list(error=ValueError("not json"))
    response = client.get("/")
    assert response.status_code == 200
    assert "Unexpected error while listing tunnels." in response.text


def test_home_has_no_guide_or_load_forms(client, configured_default):
    text = client.get("/").text
    for fragment in (
        'id="guide-section"',
        'action="/list-tunnels"',
        'name="tunnel_id"',
        "How to use Tikka Masala",
        "data-fill-tunnel-id",
    ):
        assert fragment not in text, fragment


def test_removed_dashboard_routes(client):
    assert client.post("/list-tunnels").status_code in (404, 405)
    assert client.post("/backup", data={"tunnel_id": T1}).status_code in (404, 405)


def test_home_without_credentials_points_to_settings(client, fake_list):
    recorder = fake_list(TUNNEL_LIST)
    text = client.get("/").text
    assert 'Add them in <a href="/settings">Settings</a>' in text
    assert "tunnel-table" not in text
    assert recorder.calls == []


def test_demo_without_credentials(client, monkeypatch, fake_list):
    _demo(monkeypatch)
    recorder = fake_list(TUNNEL_LIST)
    text = client.get("/").text
    assert text.count(DEMO_NO_CREDENTIALS_MESSAGE) == 1
    assert "hx-post" not in text
    assert recorder.calls == []


def test_demo_with_env_credentials(client, monkeypatch, fake_list):
    _demo(monkeypatch)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok-123456")
    recorder = fake_list([{"id": T1, "name": "Home", "status": "healthy"}])
    text = client.get("/").text
    assert f'action="/tunnels/{T1}/backup"' in text
    assert "hx-post" not in text
    assert "Back up all now" not in text
    assert recorder.calls == [(GOOD, "env-tok-123456")]


def test_home_never_renders_token(client, configured_default, seeded, fake_list):
    fake_list(TUNNEL_LIST)
    assert "test-token-0000-wxyz" not in client.get("/").text


def test_home_uses_cache_between_loads(client, configured_default, fake_list):
    recorder = fake_list(TUNNEL_LIST)
    client.get("/")
    client.get("/")
    assert len(recorder.calls) == 1


def _media_block(css: str, query: str) -> str:
    start = css.index(f"@media ({query})")
    depth = 0
    for index in range(css.index("{", start), len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start:index]
    raise AssertionError(f"unterminated @media ({query})")


def test_css_phone_layout(client):
    css = client.get("/static/styles.css").text
    assert re.search(r"\.tiles\s*\{[^}]*repeat\(4, minmax\(0, 1fr\)\)", css)
    block = _media_block(css, "max-width: 640px")
    assert re.search(r"\.tiles\s*\{[^}]*repeat\(2", block)
    assert ".tunnel-table .col-hostnames" in block
    # Status words and column headers are hidden visually only, never removed (WCAG 1.4.1).
    for rule in re.findall(r"([^{}]+)\{([^}]*)\}", block):
        selectors, body = rule
        if ".status-label" in selectors or "thead" in selectors:
            assert "display: none" not in body, selectors
    assert re.search(r"\.tunnel-table \.status-label\s*\{[^}]*clip: rect\(0 0 0 0\)", block)
    # The phone layout has its own breakpoint, not the nav collapse.
    assert ".tunnel-table" not in _media_block(css, "max-width: 960px")


def test_partial_region_with_feedback_and_notice(configured_default, seeded, fake_list):
    import asyncio

    from starlette.requests import Request

    from app.tunnels import RowFeedback, build_tunnels_home
    from app.web import render_tunnels_page

    fake_list(TUNNEL_LIST)
    home = asyncio.run(build_tunnels_home(1))
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
    response = render_tunnels_page(
        request,
        home,
        feedback={T1: RowFeedback(ok=True, message="Backup #42 created.", backup_id=42), T2: RowFeedback(ok=False, message="Nope.")},
        notice=("success", "All done."),
        partial=True,
    )
    text = response.body.decode()
    assert response.headers["vary"] == "HX-Request"
    assert text.lstrip().startswith("<div id=\"tunnels-home\" class=\"tunnels-home\">")
    assert "<html" not in text
    assert '<p class="notice success top-gap">All done.</p>' in text
    assert f'<tr id="tunnel-row-{T1}" class="tunnel-row has-feedback">' in text
    assert '<span class="row-feedback row-feedback-ok"><a href="/backup/42">Backup #42 created.</a></span>' in text
    assert '<span class="row-feedback row-feedback-error">Nope.</span>' in text
    assert f'<tr id="tunnel-row-{T3}" class="tunnel-row">' in text
