"""Dashboard and Hostnames read the saved profile credentials; no cookie, no credential forms."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.credentials as credentials
import app.routers.dashboard as dashboard
import app.web as web
from app.credentials import DEMO_NO_CREDENTIALS_MESSAGE
from app.ingress_overview import IngressOverview

GOOD = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")


class _Recorder:
    def __init__(self, result):
        self.calls: list[tuple] = []
        self.result = result

    async def __call__(self, *args):
        self.calls.append(args)
        return self.result


@pytest.fixture
def fake_list(monkeypatch):
    recorder = _Recorder([{"id": "t1", "name": "Home tunnel", "status": "healthy"}])
    monkeypatch.setattr(dashboard, "list_tunnels", recorder)
    return recorder


@pytest.fixture
def fake_backup(monkeypatch):
    recorder = _Recorder(
        SimpleNamespace(id=42, tunnel_name="Home tunnel", route_count=1, filename="unused.json")
    )
    monkeypatch.setattr(dashboard, "create_backup", recorder)
    return recorder


def _demo(monkeypatch):
    for module in (credentials, web, dashboard):
        monkeypatch.setattr(module, "DEMO_MODE", True)


def test_index_has_no_credential_inputs(client, configured_default):
    text = client.get("/").text
    assert 'name="api_token"' not in text
    assert 'name="account_id"' not in text
    assert "/clear-saved-auth" not in text
    assert "/verify-token" not in text
    assert 'href="/settings"' in text
    assert configured_default.account_id in text


def test_list_tunnels_uses_saved_credentials(client, configured_default, fake_list):
    response = client.post("/list-tunnels")
    assert response.status_code == 200
    assert fake_list.calls == [(configured_default.account_id, "test-token-0000-wxyz")]
    assert "Home tunnel" in response.text


def test_list_tunnels_ignores_posted_credentials(client, configured_default, fake_list):
    client.post("/list-tunnels", data={"account_id": "x", "api_token": "y"})
    assert fake_list.calls == [(configured_default.account_id, "test-token-0000-wxyz")]


def test_list_tunnels_without_credentials(client, fake_list):
    response = client.post("/list-tunnels")
    assert "Add them in Settings" in response.text
    assert fake_list.calls == []


def test_backup_uses_saved_credentials(client, configured_default, fake_backup):
    response = client.post("/backup", data={"tunnel_id": "t1", "notes": "n"})
    assert response.status_code == 200
    assert fake_backup.calls == [(configured_default.account_id, "t1", "test-token-0000-wxyz", "n")]
    assert "Backup created: #42" in response.text


def test_removed_routes(client):
    assert client.post("/verify-token", data={"api_token": "x"}).status_code in (404, 405)
    assert client.post("/clear-saved-auth").status_code in (404, 405)


def test_no_cookie_set(client, configured_default, fake_list, fake_backup):
    for response in (
        client.post("/list-tunnels"),
        client.post("/backup", data={"tunnel_id": "t1", "notes": ""}),
    ):
        assert "cf_api_token" not in response.headers.get("set-cookie", "")


def test_demo_without_credentials_shows_inline_state(client, monkeypatch, fake_list):
    _demo(monkeypatch)
    page = client.get("/").text
    assert page.count(DEMO_NO_CREDENTIALS_MESSAGE) == 2
    assert 'action="/list-tunnels"' not in page
    assert 'action="/backup"' not in page
    response = client.post("/list-tunnels")
    assert DEMO_NO_CREDENTIALS_MESSAGE in response.text
    assert fake_list.calls == []


def test_demo_with_env_credentials_lists_tunnels(client, monkeypatch, fake_list):
    _demo(monkeypatch)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok-123456")
    response = client.post("/list-tunnels")
    assert fake_list.calls == [(GOOD, "env-tok-123456")]
    assert DEMO_NO_CREDENTIALS_MESSAGE not in response.text
    assert 'action="/list-tunnels"' in response.text


def test_ingress_live_uses_saved_credentials(client, configured_default, monkeypatch):
    calls: list[tuple] = []

    async def fake_live(*args):
        calls.append(args)
        return IngressOverview(source="live")

    monkeypatch.setattr("app.routers.ingress.live_overview", fake_live)
    response = client.get("/ingress?source=live")
    assert response.status_code == 200
    assert calls == [(configured_default.account_id, "test-token-0000-wxyz")]


def test_api_credentials_repr_hides_token():
    creds = credentials.ApiCredentials(account_id=GOOD, api_token="secret-tok-123456")
    assert "secret-tok-123456" not in repr(creds)
    assert GOOD in repr(creds)


def test_dashboard_never_renders_token(client, configured_default):
    text = client.get("/").text
    assert configured_default.account_id in text
    assert configured_default.api_token not in text
