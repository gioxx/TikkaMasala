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
        raise jwt.PyJWKClientError("fetch failed")

    with pytest.raises(AccessDenied) as exc:
        AccessVerifier(settings, key_resolver=broken).verify(make_token())
    assert exc.value.reason == "signing key unavailable"
