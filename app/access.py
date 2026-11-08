"""Optional Cloudflare Access enforcement.

When ``CF_ACCESS_TEAM_DOMAIN`` and ``CF_ACCESS_AUD`` are both set, every request
except the health check must carry a valid Access JWT in the
``Cf-Access-Jwt-Assertion`` header. With neither set, Access is off and the app
behaves as before. Setting only one of them is a configuration error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

logger = logging.getLogger(__name__)

ACCESS_DOMAIN_SUFFIX = ".cloudflareaccess.com"
# Small tolerance for clock drift between Cloudflare and the container.
CLOCK_SKEW_LEEWAY_SECONDS = 30


class AccessConfigError(RuntimeError):
    """Only one of the two Access variables is set."""


class AccessDenied(Exception):
    """The request carries no valid Access token. ``reason`` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AccessSettings:
    team_domain: str
    audience: str

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"


@dataclass(frozen=True)
class AccessIdentity:
    email: str | None
    subject: str

    @property
    def label(self) -> str:
        return self.email or "service token"


def normalize_team_domain(value: str) -> str:
    domain = value.strip().lower()
    for scheme in ("https://", "http://"):
        if domain.startswith(scheme):
            domain = domain[len(scheme):]
    domain = domain.strip("/")
    if domain and "." not in domain:
        domain += ACCESS_DOMAIN_SUFFIX
    return domain


def load_access_settings(team_domain: str, audience: str) -> AccessSettings | None:
    team_domain = normalize_team_domain(team_domain or "")
    audience = (audience or "").strip()
    if not team_domain and not audience:
        return None
    if not audience:
        raise AccessConfigError("CF_ACCESS_TEAM_DOMAIN is set but CF_ACCESS_AUD is missing: set both to enable Cloudflare Access, or neither.")
    if not team_domain:
        raise AccessConfigError("CF_ACCESS_AUD is set but CF_ACCESS_TEAM_DOMAIN is missing: set both to enable Cloudflare Access, or neither.")
    return AccessSettings(team_domain=team_domain, audience=audience)


class AccessVerifier:
    def __init__(self, settings: AccessSettings, key_resolver: Callable[[str], Any] | None = None) -> None:
        self.settings = settings
        if key_resolver is None:
            jwks_client = jwt.PyJWKClient(settings.certs_url, cache_keys=True)

            def key_resolver(token: str) -> Any:
                return jwks_client.get_signing_key_from_jwt(token).key

        self._key_resolver = key_resolver

    def verify(self, token: str | None) -> AccessIdentity:
        token = (token or "").strip()
        if not token:
            raise AccessDenied("missing token")
        try:
            key = self._key_resolver(token)
        except jwt.DecodeError:
            # The resolver reads the header to find the key id: not a JWT at all.
            raise AccessDenied("invalid token") from None
        except Exception:
            logger.exception("Unable to load Cloudflare Access signing keys from %s.", self.settings.certs_url)
            raise AccessDenied("signing key unavailable") from None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise AccessDenied("expired token") from None
        except jwt.InvalidAudienceError:
            raise AccessDenied("wrong audience") from None
        except jwt.InvalidIssuerError:
            raise AccessDenied("wrong issuer") from None
        except jwt.InvalidTokenError:
            raise AccessDenied("invalid token") from None
        email = claims.get("email")
        return AccessIdentity(email=email if isinstance(email, str) and email else None, subject=str(claims.get("sub") or ""))
