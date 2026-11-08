"""Settings page: profile credentials, Cloudflare Access status and app info."""

from __future__ import annotations

import pytest

import app.cloudflare as cloudflare
import app.credentials as credentials
import app.routers.settings as settings_router
from app.access import AccessIdentity, AccessSettings
from app.cloudflare import CredentialsInvalid
from app.credentials import get_server_api_token
from app.profiles import DEFAULT_PROFILE_ID, get_profile, update_profile

GOOD = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_state(client, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    yield
    client.app.state.access_verifier = None


class _Recorder:
    def __init__(self, exc: BaseException | None = None):
        self.calls: list[tuple] = []
        self.exc = exc

    async def __call__(self, *args):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return 0


@pytest.fixture
def fake_check(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(cloudflare, "check_credentials", recorder)
    return recorder


def test_settings_shows_masked_token(client, configured_default):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "configured, ending in wxyz" in response.text
    assert "test-token-0000-wxyz" not in response.text
    assert configured_default.account_id in response.text
    assert "from database" in response.text
    assert "Replace credentials" in response.text
    assert 'action="/settings/profiles/1/clear-credentials"' in response.text


def test_settings_env_values_read_only(client, monkeypatch):
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-123456")
    text = client.get("/settings").text
    assert "from environment" in text
    assert "Save a different value" in text
    assert "env-token-123456" not in text
    # Nothing saved in the database: nothing to forget.
    assert 'action="/settings/profiles/1/clear-credentials"' not in text


def test_settings_db_value_overrides_env(client, configured_default, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-123456")
    text = client.get("/settings").text
    assert "overrides CLOUDFLARE_API_TOKEN" in text


def test_settings_unreadable_token_warning(client):
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="enc:garbage")
    try:
        text = client.get("/settings").text
        assert "cannot be decrypted" in text
    finally:
        update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)


def test_settings_save_valid(client, configured_default, fake_check):
    response = client.post(
        "/settings/profiles/1/credentials",
        data={"account_id": configured_default.account_id, "api_token": "tok-new-123456"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
    assert get_server_api_token(DEFAULT_PROFILE_ID) == "tok-new-123456"
    assert "tok-new-123456" not in response.text
    assert "Credentials verified and saved." in client.get("/settings?saved=1").text


def test_settings_save_invalid(client, configured_default, monkeypatch):
    monkeypatch.setattr(cloudflare, "check_credentials", _Recorder(CredentialsInvalid("Invalid API Token")))
    response = client.post(
        "/settings/profiles/1/credentials",
        data={"account_id": configured_default.account_id, "api_token": "tok-bad-123456"},
    )
    assert response.status_code == 400
    assert "Invalid API Token" in response.text
    assert "tok-bad-123456" not in response.text
    assert f'value="{configured_default.account_id}"' in response.text
    assert get_server_api_token(DEFAULT_PROFILE_ID) == configured_default.api_token


def test_settings_save_invalid_echoes_submitted_account(client, configured_default, monkeypatch):
    other = "fedcba9876543210fedcba9876543210"
    monkeypatch.setattr(cloudflare, "check_credentials", _Recorder(CredentialsInvalid("Account not found")))
    response = client.post("/settings/profiles/1/credentials", data={"account_id": other, "api_token": ""})
    assert response.status_code == 400
    assert f'value="{other}"' in response.text
    assert get_profile(DEFAULT_PROFILE_ID).account_id == configured_default.account_id


def test_settings_save_unchanged(client, configured_default, fake_check):
    response = client.post(
        "/settings/profiles/1/credentials",
        data={"account_id": configured_default.account_id, "api_token": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?unchanged=1"
    assert fake_check.calls == []
    assert "Nothing changed." in client.get("/settings?unchanged=1").text


def test_settings_clear(client, configured_default):
    response = client.post("/settings/profiles/1/clear-credentials", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?cleared=1"
    profile = get_profile(DEFAULT_PROFILE_ID)
    assert profile.account_id is None
    assert profile.api_token is None
    assert "Saved credentials removed." in client.get("/settings?cleared=1").text


def test_settings_demo_read_only(client, configured_default, monkeypatch, fake_check):
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    monkeypatch.setattr(settings_router, "DEMO_MODE", True)
    before = get_profile(DEFAULT_PROFILE_ID)
    text = client.get("/settings").text
    assert "read-only" in text
    assert 'action="/settings/profiles/1/credentials"' not in text
    assert 'action="/settings/profiles/1/clear-credentials"' not in text
    assert 'href="/settings/notifications"' not in text

    response = client.post(
        "/settings/profiles/1/credentials",
        data={"account_id": GOOD, "api_token": "tok-demo-123456"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    response = client.post("/settings/profiles/1/clear-credentials", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert get_profile(DEFAULT_PROFILE_ID) == before
    assert fake_check.calls == []


def test_settings_unknown_profile_404(client, fake_check):
    assert client.post("/settings/profiles/999/credentials", data={"account_id": GOOD}).status_code == 404
    assert client.post("/settings/profiles/999/clear-credentials").status_code == 404
    assert fake_check.calls == []


def test_settings_cross_site_post_rejected(client, configured_default, fake_check):
    headers = {"Sec-Fetch-Site": "cross-site"}
    response = client.post(
        "/settings/profiles/1/credentials",
        data={"account_id": GOOD, "api_token": "tok-csrf-123456"},
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 403
    response = client.post("/settings/profiles/1/clear-credentials", headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert get_server_api_token(DEFAULT_PROFILE_ID) == configured_default.api_token
    assert get_profile(DEFAULT_PROFILE_ID).account_id == configured_default.account_id
    assert fake_check.calls == []


def test_settings_same_origin_post_allowed(client, configured_default):
    response = client.post(
        "/settings/profiles/1/clear-credentials",
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_settings_access_off(client):
    text = client.get("/settings").text
    assert "Off" in text
    assert "CF_ACCESS_TEAM_DOMAIN" in text


def test_settings_access_on(client):
    class _Verifier:
        settings = AccessSettings("team.cloudflareaccess.com", "aud")

        def verify(self, token):
            return AccessIdentity("a@b.c", "s")

    client.app.state.access_verifier = _Verifier()
    text = client.get("/settings").text
    assert "team.cloudflareaccess.com" in text
    assert "a@b.c" in text


def test_settings_links_and_version(client):
    from app.config import APP_VERSION

    text = client.get("/settings").text
    assert 'href="/settings/notifications"' in text
    assert APP_VERSION in text


def test_settings_key_source(client, monkeypatch):
    monkeypatch.setattr(settings_router, "encryption_key_source", lambda: "missing")
    assert "Not created yet" in client.get("/settings").text
    monkeypatch.setattr(settings_router, "encryption_key_source", lambda: "invalid")
    assert "Invalid: check TOKEN_ENCRYPTION_KEY or encryption.key" in client.get("/settings").text


def test_every_page_links_settings(client):
    for path in ("/", "/ingress", "/automation/runs", "/settings/notifications", "/automation/tunnels"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'href="/settings"' in response.text, path


def test_backup_page_links_settings(client, make_backup):
    backup_id = make_backup()
    assert 'href="/settings"' in client.get(f"/backup/{backup_id}").text


def test_settings_forget_needs_confirmation(client, configured_default):
    text = client.get("/settings").text
    start = text.index('<details class="forget-credentials')
    block = text[start:text.index("</details>", start)]
    assert "Forget saved credentials…" in block
    assert "only once" in block
    assert 'action="/settings/profiles/1/clear-credentials"' in block


def test_settings_clear_falls_back_to_env(client, configured_default, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-123456")
    response = client.post("/settings/profiles/1/clear-credentials", follow_redirects=False)
    assert response.status_code == 303
    text = client.get("/settings?cleared=1").text
    assert "from environment" in text
    assert "from database" not in text


@pytest.mark.setup_gate
def test_settings_clear_without_env_goes_to_setup(client, configured_default, monkeypatch):
    import app.setup as setup

    monkeypatch.setattr(setup, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    response = client.post("/settings/profiles/1/clear-credentials", follow_redirects=False)
    assert response.status_code == 303
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
