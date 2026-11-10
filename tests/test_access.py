"""Cloudflare Access: configuration, token verification and request enforcement."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.access import (
    AccessConfigError,
    AccessDenied,
    AccessIdentity,
    AccessVerifier,
    load_access_settings,
    normalize_team_domain,
)

AUDIENCE = "aud-123"
ISSUER = "https://team.cloudflareaccess.com"


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def settings():
    return load_access_settings("team", AUDIENCE)


@pytest.fixture(scope="module")
def verifier(settings, private_key):
    public_key = private_key.public_key()
    return AccessVerifier(settings, key_resolver=lambda _token: public_key)


@pytest.fixture
def make_token(private_key):
    def _make(signing_key=None, **overrides) -> str:
        now = int(time.time())
        claims = {
            "aud": [AUDIENCE],
            "iss": ISSUER,
            "email": "user@example.org",
            "sub": "user-1",
            "exp": now + 300,
            "iat": now,
        }
        claims.update(overrides)
        claims = {key: value for key, value in claims.items() if value is not None}
        return jwt.encode(claims, signing_key or private_key, algorithm="RS256", headers={"kid": "k1"})

    return _make


# --- configuration -----------------------------------------------------------


def test_load_settings_off_when_both_empty():
    assert load_access_settings("", "") is None
    assert load_access_settings("  ", " ") is None


@pytest.mark.parametrize(
    "team,aud,missing",
    [("team", "", "CF_ACCESS_AUD"), ("", "aud", "CF_ACCESS_TEAM_DOMAIN")],
)
def test_load_settings_rejects_half_config(team, aud, missing):
    with pytest.raises(AccessConfigError, match=missing):
        load_access_settings(team, aud)


@pytest.mark.parametrize(
    "raw",
    [
        "team",
        "team.cloudflareaccess.com",
        "https://team.cloudflareaccess.com/",
        " TEAM.cloudflareaccess.com ",
        "http://team.cloudflareaccess.com",
    ],
)
def test_normalize_team_domain(raw):
    assert normalize_team_domain(raw) == "team.cloudflareaccess.com"


def test_settings_urls(settings):
    assert settings.audience == AUDIENCE
    assert settings.issuer == ISSUER
    assert settings.certs_url == "https://team.cloudflareaccess.com/cdn-cgi/access/certs"


# --- verification ------------------------------------------------------------


def test_verify_valid_token(verifier, make_token):
    assert verifier.verify(make_token()) == AccessIdentity(email="user@example.org", subject="user-1")


def test_verify_service_token_without_email(verifier, make_token):
    identity = verifier.verify(make_token(email=None))
    assert identity.email is None
    assert identity.label == "service token"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("none", "missing token"),
        ("empty", "missing token"),
        ("garbage", "invalid token"),
        ("expired", "expired token"),
        ("audience", "wrong audience"),
        ("issuer", "wrong issuer"),
        ("other_key", "invalid token"),
    ],
)
def test_verify_rejects(verifier, make_token, other_private_key, case, reason):
    tokens = {
        "none": lambda: None,
        "empty": lambda: "",
        "garbage": lambda: "not-a-jwt",
        "expired": lambda: make_token(exp=int(time.time()) - 120),
        "audience": lambda: make_token(aud=["other-app"]),
        "issuer": lambda: make_token(iss="https://evil.cloudflareaccess.com"),
        "other_key": lambda: make_token(signing_key=other_private_key),
    }
    with pytest.raises(AccessDenied) as exc:
        verifier.verify(tokens[case]())
    assert exc.value.reason == reason


def test_verify_fails_closed_when_keys_unavailable(settings, make_token):
    def broken(_token):
        raise jwt.PyJWKClientConnectionError("fetch failed")

    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings, key_resolver=broken).verify(make_token())
    assert exc.value.reason == "signing key unavailable"


def test_verify_fails_closed_on_unexpected_resolver_error(settings, make_token):
    def broken(_token):
        raise OSError("disk on fire")

    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings, key_resolver=broken).verify(make_token())
    assert exc.value.reason == "signing key unavailable"


# --- enforcement -------------------------------------------------------------

ACCESS_HEADER = "Cf-Access-Jwt-Assertion"


@pytest.fixture
def access_on(client, verifier):
    client.app.state.access_verifier = verifier
    yield
    client.app.state.access_verifier = None


def test_access_off_by_default(client):
    assert client.app.state.access_verifier is None
    assert client.get("/").status_code == 200


def test_healthz_exempt(client, access_on):
    assert client.get("/healthz").status_code == 200


def test_request_without_token_rejected(client, access_on, caplog):
    caplog.set_level("WARNING", logger="app.access")
    response = client.get("/")
    assert response.status_code == 403
    assert "Cloudflare Access" in response.text
    assert "/static/" not in response.text
    assert "missing token" in caplog.text


def test_access_denied_shows_formatted_version_badge(client, access_on, monkeypatch):
    from app.web import templates

    monkeypatch.setitem(templates.env.globals, "app_version_badge", "dev · 1.3.10+42 · 8cce54d")
    response = client.get("/")
    assert response.status_code == 403
    assert "Tikka Masala dev · 1.3.10+42 · 8cce54d" in response.text
    assert "1.3.10-42-g8cce54d" not in response.text


def test_garbage_token_is_403_not_500(client, access_on):
    assert client.get("/", headers={ACCESS_HEADER: "garbage"}).status_code == 403


def test_static_assets_also_protected(client, access_on):
    assert client.get("/static/styles.css").status_code == 403


def test_htmx_rejection_asks_for_refresh(client, access_on):
    response = client.get("/ingress", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert response.headers["HX-Refresh"] == "true"


def test_plain_rejection_has_no_refresh_header(client, access_on):
    assert "HX-Refresh" not in client.get("/ingress").headers


def test_valid_token_shows_identity_and_sign_out(client, access_on, make_token):
    token = make_token()
    response = client.get("/", headers={ACCESS_HEADER: token})
    assert response.status_code == 200
    assert "user@example.org" in response.text
    assert 'href="/cdn-cgi/access/logout"' in response.text
    assert token not in response.text


def test_valid_token_reaches_other_pages(client, access_on, make_token):
    headers = {ACCESS_HEADER: make_token()}
    assert client.get("/ingress", headers=headers).status_code == 200
    assert client.get("/static/styles.css", headers=headers).status_code == 200


def test_token_never_logged(client, access_on, make_token, caplog):
    caplog.set_level("WARNING", logger="app.access")
    token = make_token(aud=["other-app"])
    assert client.get("/", headers={ACCESS_HEADER: token}).status_code == 403
    assert token not in caplog.text
    assert "wrong audience" in caplog.text


def test_no_identity_markup_when_access_off(client):
    assert "/cdn-cgi/access/logout" not in client.get("/").text


# --- final review fixes ------------------------------------------------------


def _jwks_for(private_key, kid="k1"):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def test_real_resolver_accepts_token_from_jwks(settings, private_key, make_token, monkeypatch):
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: _jwks_for(private_key))
    assert AccessVerifier(settings).verify(make_token()).email == "user@example.org"


def test_real_resolver_unknown_kid_is_not_reported_as_outage(settings, private_key, make_token, monkeypatch, caplog):
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: _jwks_for(private_key, kid="other"))
    caplog.set_level("WARNING", logger="app.access")
    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings).verify(make_token())
    assert exc.value.reason == "unknown signing key"
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_real_resolver_malformed_kid_is_invalid_token(settings, private_key, monkeypatch):
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: _jwks_for(private_key))
    import base64
    import json

    valid = jwt.encode({"aud": AUDIENCE, "iss": ISSUER, "exp": int(time.time()) + 60}, private_key, algorithm="RS256", headers={"kid": "k1"})
    bad_header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT", "kid": ["k1"]}).encode()).rstrip(b"=").decode()
    token = ".".join([bad_header, *valid.split(".")[1:]])
    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings).verify(token)
    assert exc.value.reason == "invalid token"


def test_real_resolver_garbage_token_is_invalid_token(settings, private_key, monkeypatch):
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: _jwks_for(private_key))
    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings).verify("not-a-jwt")
    assert exc.value.reason == "invalid token"


def test_real_resolver_connection_error_fails_closed(settings, make_token, monkeypatch):
    def unreachable(self):
        raise jwt.PyJWKClientConnectionError("connection refused")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", unreachable)
    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings).verify(make_token())
    assert exc.value.reason == "signing key unavailable"


def test_jwks_client_uses_short_timeout(settings):
    assert AccessVerifier(settings)._jwks_client.timeout <= 5


def test_slow_key_fetch_does_not_block_other_requests(client, settings, private_key, make_token):
    import asyncio

    import httpx

    public_key = private_key.public_key()

    def slow_resolver(_token):
        time.sleep(0.6)
        return public_key

    async def scenario():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            finished = {}

            async def timed(name, coro):
                response = await coro
                finished[name] = time.perf_counter()
                return response

            start = time.perf_counter()
            slow, health = await asyncio.gather(
                timed("slow", http.get("/healthz-probe-protected", headers={ACCESS_HEADER: make_token()})),
                timed("health", http.get("/healthz")),
            )
            return slow, health, finished["health"] - start, finished["slow"] - start

    client.app.state.access_verifier = AccessVerifier(settings, key_resolver=slow_resolver)
    try:
        _slow, health, health_elapsed, slow_elapsed = asyncio.run(scenario())
    finally:
        client.app.state.access_verifier = None
    assert health.status_code == 200
    assert health_elapsed < 0.3 < slow_elapsed


def test_rejection_log_escapes_newlines_in_path(client, verifier, caplog):
    import asyncio

    from starlette.requests import Request

    from app.access import access_guard

    client.app.state.access_verifier = verifier
    scope = {
        "type": "http", "method": "GET", "path": "/\nFAKE LINE", "raw_path": b"/%0AFAKE%20LINE",
        "query_string": b"", "headers": [], "client": ("10.0.0.9", 1234), "server": ("test", 80),
        "scheme": "http", "app": client.app,
    }

    async def never_called(_request):
        raise AssertionError("request must be rejected")

    caplog.set_level("WARNING", logger="app.access")
    try:
        response = asyncio.run(access_guard(Request(scope), never_called))
    finally:
        client.app.state.access_verifier = None
    assert response.status_code == 403
    assert "\nFAKE LINE" not in caplog.text
