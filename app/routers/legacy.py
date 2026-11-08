"""Old URLs: permanent redirects to their new home.

A 308 keeps the method and the body, so a form posted from a tab opened before the upgrade
still reaches the new handler. The query string is carried over. Redirects answer in demo mode
too; the target decides what demo mode allows.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

LEGACY_REDIRECTS: dict[str, str] = {
    "/auto-backup/settings": "/automation/schedule",
    "/auto-backup/run": "/automation/run",
    "/notifications/test": "/automation/notifications/test",
    "/auto-backup/tunnel-filters": "/automation/tunnels",
    "/auto-backup/runs": "/automation/runs",
}


def _redirect_to(target: str) -> Callable[[Request], Awaitable[RedirectResponse]]:
    async def handler(request: Request) -> RedirectResponse:
        query = request.url.query
        return RedirectResponse(target + ("?" + query if query else ""), status_code=308)

    return handler


for _path, _target in LEGACY_REDIRECTS.items():
    router.add_api_route(_path, _redirect_to(_target), methods=["GET", "POST"], include_in_schema=False)
