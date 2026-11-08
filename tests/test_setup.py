"""First-run setup screen, the setup gate and the one-time cookie token migration."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import app.cloudflare as cloudflare
import app.credentials as credentials
import app.encryption as encryption
import app.setup as setup
from app.access import AccessDenied
from app.cloudflare import CredentialsInvalid
from app.credentials import get_api_credentials
from app.profiles import DEFAULT_PROFILE_ID, get_profile, update_profile

pytestmark = pytest.mark.setup_gate

GOOD = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_state(client, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(setup, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", tmp_path / "encryption.key")
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)
    client.cookies.clear()
    yield
    client.cookies.clear()
    client.app.state.access_verifier = None
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)


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


def _stored_token() -> str | None:
    return credentials._read_stored_api_token(DEFAULT_PROFILE_ID)


def _assert_cookie_deleted(response) -> None:
    set_cookie = response.headers.get("set-cookie", "")
    assert 'cf_api_token=""' in set_cookie
    assert "Max-Age=0" in set_cookie


# --- the gate -----------------------------------------------------------------


def test_redirects_to_setup_when_unconfigured(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_post_also_redirected(client):
    response = client.post("/backup", data={"tunnel_id": "t"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_htmx_gets_hx_redirect(client):
    response = client.get("/ingress", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/setup"


def test_exempt_paths(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 200
    assert 'name="api_token"' in response.text
    assert 'name="account_id"' in response.text


def test_no_redirect_when_configured(client, configured_default):
    assert client.get("/", follow_redirects=False).status_code == 200
    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_env_only_skips_setup(client, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-123456")
    assert client.get("/", follow_redirects=False).status_code == 200


def test_demo_mode_skips_setup(client, monkeypatch):
    monkeypatch.setattr(setup, "DEMO_MODE", True)
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    assert client.get("/", follow_redirects=False).status_code == 200
    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_access_runs_before_setup(client):
    class _Denying:
        def verify(self, token):
            raise AccessDenied("missing token")

    client.app.state.access_verifier = _Denying()
    assert client.get("/", follow_redirects=False).status_code == 403


# --- the setup form ---------------------------------------------------------------


def test_setup_saves_and_redirects(client, fake_check):
    response = client.post(
        "/setup", data={"account_id": GOOD, "api_token": "tok-setup-123456"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert get_api_credentials(DEFAULT_PROFILE_ID).api_token == "tok-setup-123456"
    assert fake_check.calls == [(GOOD, "tok-setup-123456")]


def test_setup_shows_inline_error(client, monkeypatch):
    monkeypatch.setattr(cloudflare, "check_credentials", _Recorder(CredentialsInvalid("Invalid API Token")))
    response = client.post("/setup", data={"account_id": GOOD, "api_token": "tok-bad-123456"})
    assert response.status_code == 400
    assert "Invalid API Token" in response.text
    assert GOOD in response.text
    assert "tok-bad-123456" not in response.text
    assert get_profile(DEFAULT_PROFILE_ID).api_token is None


def test_setup_requires_both_fields(client, fake_check):
    response = client.post("/setup", data={"account_id": GOOD})
    assert response.status_code == 400
    assert "required" in response.text
    assert fake_check.calls == []


def test_setup_explains_unreadable_token(client):
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="enc:garbage")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert "cannot be decrypted" in client.get("/setup").text


def test_setup_replaces_unreadable_token(client, fake_check):
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="enc:garbage")
    response = client.post(
        "/setup", data={"account_id": GOOD, "api_token": "tok-new-123456"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert _stored_token() == "tok-new-123456"


def test_setup_post_ignored_when_configured(client, configured_default, fake_check):
    response = client.post(
        "/setup", data={"account_id": GOOD, "api_token": "tok-other-123456"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake_check.calls == []
    assert _stored_token() == configured_default.api_token


def test_setup_shows_environment_hints(client, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    text = client.get("/setup").text
    assert "Found in CLOUDFLARE_ACCOUNT_ID" in text
    assert "Found in CLOUDFLARE_API_TOKEN" not in text
    assert f'value="{GOOD}"' in text


# --- cookie migration ---------------------------------------------------------------


def test_cookie_token_migrated_and_deleted(client):
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD)
    client.cookies.set("cf_api_token", "cookie-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert _stored_token() == "cookie-tok-123456"
    assert get_profile(DEFAULT_PROFILE_ID).api_token.startswith("enc:")
    _assert_cookie_deleted(response)


def test_cookie_does_not_overwrite_saved_token(client, configured_default):
    client.cookies.set("cf_api_token", "other-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert _stored_token() == "test-token-0000-wxyz"
    _assert_cookie_deleted(response)


def test_cookie_replaces_unreadable_token(client):
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD, api_token="enc:garbage")
    client.cookies.set("cf_api_token", "cookie-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert _stored_token() == "cookie-tok-123456"
    _assert_cookie_deleted(response)


def test_cookie_ignored_in_demo(client, monkeypatch):
    monkeypatch.setattr(setup, "DEMO_MODE", True)
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    client.cookies.set("cf_api_token", "cookie-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert get_profile(DEFAULT_PROFILE_ID).api_token is None
    _assert_cookie_deleted(response)


def test_cookie_deleted_on_redirect(client):
    """No Account ID yet: the cookie token is migrated, setup still asks for the rest."""
    client.cookies.set("cf_api_token", "cookie-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert _stored_token() == "cookie-tok-123456"
    _assert_cookie_deleted(response)


def test_cookie_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD)
    client.cookies.set("cf_api_token", "cookie-tok-123456")
    client.get("/", follow_redirects=False)
    assert "Migrated the API token from the browser cookie into the database." in caplog.text
    assert "cookie-tok-123456" not in caplog.text


def test_migrate_cookie_token_unit():
    update_profile(DEFAULT_PROFILE_ID, account_id=GOOD)
    assert setup.migrate_cookie_token("  ") is False
    assert setup.migrate_cookie_token("cookie-tok-123456") is True
    assert setup.migrate_cookie_token("second-tok-123456") is False
    assert _stored_token() == "cookie-tok-123456"


def test_setup_rejects_cross_site_post(client, fake_check):
    response = client.post(
        "/setup",
        data={"account_id": GOOD, "api_token": "tok-csrf-123456"},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert fake_check.calls == []
    assert get_profile(DEFAULT_PROFILE_ID).api_token is None


def test_cookie_with_env_token_equal_not_saved(client, monkeypatch):
    """Env-only installs set the cookie to the env token: copying it would pin it in the DB forever."""
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok-123456")
    client.cookies.set("cf_api_token", "env-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert get_profile(DEFAULT_PROFILE_ID).api_token is None
    assert credentials.get_server_api_token_source(DEFAULT_PROFILE_ID) == "environment"
    _assert_cookie_deleted(response)


def test_cookie_with_env_token_different_not_saved(client, monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-tok-123456")
    client.cookies.set("cf_api_token", "cookie-tok-123456")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert get_profile(DEFAULT_PROFILE_ID).api_token is None
    assert credentials.get_server_api_token_source(DEFAULT_PROFILE_ID) == "environment"
    _assert_cookie_deleted(response)
