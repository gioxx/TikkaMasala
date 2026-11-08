"""Tunnels home: summary tiles, the live tunnel table and its manual backup actions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app.config import BACKUP_DIR, DEMO_MODE
from app.credentials import require_api_credentials
from app.profiles import current_profile_id
from app.tunnels import (
    RowFeedback,
    back_up_all_tunnels,
    back_up_tunnel,
    backup_saved_feedback,
    build_tunnels_home,
    is_valid_tunnel_id,
)
from app.web import render_tunnels_page

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _render_home(
    request: Request,
    *,
    feedback: dict[str, RowFeedback] | None = None,
    notice: tuple[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    home = await build_tunnels_home(current_profile_id(request))
    return render_tunnels_page(
        request, home, feedback=feedback, notice=notice, partial=_is_htmx(request), status_code=status_code
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return render_tunnels_page(request, await build_tunnels_home(current_profile_id(request)))


@router.post("/tunnels/backup-all", response_model=None)
async def backup_all_action(request: Request) -> Response:
    if DEMO_MODE:
        return RedirectResponse("/", status_code=303)
    outcome = await back_up_all_tunnels(current_profile_id(request))
    return await _render_home(request, feedback=outcome.feedback, notice=(outcome.tone, outcome.message))


@router.post("/tunnels/{tunnel_id}/backup", response_model=None)
async def backup_tunnel_action(request: Request, tunnel_id: str) -> Response:
    if not is_valid_tunnel_id(tunnel_id):
        return await _render_home(request, notice=("error", "Unknown tunnel ID."), status_code=400)
    tunnel_id = tunnel_id.lower()
    try:
        creds = require_api_credentials(current_profile_id(request))
    except HTTPException as exc:
        return await _render_home(request, notice=("error", str(exc.detail)))
    try:
        backup = await back_up_tunnel(creds, tunnel_id)
    except HTTPException as exc:
        return await _render_home(request, feedback={tunnel_id: RowFeedback(ok=False, message=str(exc.detail))})
    if DEMO_MODE:
        return FileResponse(path=BACKUP_DIR / backup.filename, filename=backup.filename, media_type="application/json")
    return await _render_home(request, feedback={tunnel_id: backup_saved_feedback(backup)})
