"""Optional Cloudflare Access enforcement.

When ``CF_ACCESS_TEAM_DOMAIN`` and ``CF_ACCESS_AUD`` are both set, every request
except the health check must carry a valid Access JWT in the
``Cf-Access-Jwt-Assertion`` header. With neither set, Access is off and the app
behaves as before. Setting only one of them is a configuration error.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

ACCESS_DOMAIN_SUFFIX = ".cloudflareaccess.com"
# Small tolerance for clock drift between Cloudflare and the container.
CLOCK_SKEW_LEEWAY_SECONDS = 30
# Keep a slow or unreachable certs endpoint from holding requests for long.
JWKS_FETCH_TIMEOUT_SECONDS = 5


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
        self._jwks_client: jwt.PyJWKClient | None = None
        if key_resolver is None:
            jwks_client = jwt.PyJWKClient(settings.certs_url, cache_keys=True, timeout=JWKS_FETCH_TIMEOUT_SECONDS)
            self._jwks_client = jwks_client

            def key_resolver(token: str) -> Any:
                return jwks_client.get_signing_key_from_jwt(token).key

        self._key_resolver = key_resolver

    def verify(self, token: str | None) -> AccessIdentity:
        token = (token or "").strip()
        if not token:
            raise AccessDenied("missing token")
        try:
            key = self._key_resolver(token)
        except jwt.PyJWKClientConnectionError as exc:
            logger.error("Unable to download Cloudflare Access signing keys from %s: %s", self.settings.certs_url, exc)
            raise AccessDenied("signing key unavailable") from None
        except jwt.PyJWKClientError:
            # Keys downloaded fine, but none matches the token's key id: forged or stale token.
            raise AccessDenied("unknown signing key") from None
        except jwt.InvalidTokenError:
            # The resolver reads the header to find the key id: malformed header or not a JWT at all.
            raise AccessDenied("invalid token") from None
        except Exception:
            logger.exception("Unexpected error loading Cloudflare Access signing keys from %s.", self.settings.certs_url)
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


# --- request enforcement -----------------------------------------------------

ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion"
ACCESS_EXEMPT_PATHS = frozenset({"/healthz"})
ACCESS_LOGOUT_PATH = "/cdn-cgi/access/logout"


def access_identity(request: Request) -> AccessIdentity | None:
    """The verified Access identity of this request, or ``None`` when Access is off."""

    return getattr(request.state, "access_identity", None)


def is_cross_site_request(request: Request) -> bool:
    """Whether the browser says this request comes from another site (``Sec-Fetch-Site: cross-site``).

    A cheap guard for state-changing form posts (login CSRF): requests without the
    header (older browsers, scripts) are let through.
    """

    return request.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site"


def cross_site_rejected() -> Response:
    return Response("Cross-site form submissions are not allowed.", status_code=403, media_type="text/plain")


def _denied_response(request: Request) -> Response:
    from app.web import templates  # deferred: app.web pulls in the whole template stack

    response = templates.TemplateResponse(request, "access_denied.html", status_code=403)
    if request.headers.get("HX-Request"):
        # htmx does not swap 4xx responses: reload the page so Cloudflare can show its login.
        response.headers["HX-Refresh"] = "true"
    return response


async def access_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    verifier: AccessVerifier | None = getattr(request.app.state, "access_verifier", None)
    if verifier is None or request.url.path in ACCESS_EXEMPT_PATHS:
        return await call_next(request)
    try:
        # verify() may download signing keys with blocking I/O: keep it off the event loop.
        request.state.access_identity = await run_in_threadpool(verifier.verify, request.headers.get(ACCESS_JWT_HEADER))
    except AccessDenied as exc:
        client_host = request.client.host if request.client else "unknown"
        logger.warning("Cloudflare Access rejected %s %s from %s: %s.", request.method, request.url.path, client_host, exc.reason)
        return _denied_response(request)
    return await call_next(request)
