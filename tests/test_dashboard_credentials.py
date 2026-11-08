"""Dashboard and Hostnames read the saved profile credentials; no cookie, no credential forms."""

from __future__ import annotations

import pytest

import app.credentials as credentials
from app.ingress_overview import IngressOverview

GOOD = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")


def test_index_has_no_credential_inputs(client, configured_default):
    text = client.get("/").text
    assert 'name="api_token"' not in text
    assert 'name="account_id"' not in text
    assert "/clear-saved-auth" not in text
    assert "/verify-token" not in text
    assert 'href="/settings"' in text
    assert configured_default.account_id in text


def test_removed_routes(client):
    assert client.post("/verify-token", data={"api_token": "x"}).status_code in (404, 405)
    assert client.post("/clear-saved-auth").status_code in (404, 405)


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


def test_missing_credentials_wording_points_to_settings(client):
    """No cookie or in-page credential forms any more: missing values are added in Settings."""
    for path in ("/automation", "/automation/tunnels"):
        text = client.get(path).text
        assert "Browser cookies" not in text, path
        assert "through the app" not in text, path
        assert "Server-side Account ID" not in text, path
        assert "Missing: Account ID, API token." in text, path
        assert "Add them in <a href=\"/settings\">Settings</a>" in text, path
