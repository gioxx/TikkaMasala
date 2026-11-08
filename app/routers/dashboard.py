"""Tunnels home: summary tiles, the live tunnel table and its manual backup actions."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.background import finish_or_continue
from app.config import DEMO_MODE
from app.credentials import require_api_credentials
from app.profiles import current_profile_id
from app.tunnels import (
    RowFeedback,
    back_up_all_tunnels,
    back_up_tunnel,
    backup_saved_feedback,
    build_tunnels_home,
    is_valid_tunnel_id,
    snapshot_for_download,
)
from app.web import render_tunnels_page

router = APIRouter()

BACKGROUND_NOTICE = "Back up all continues in the background. Reload the page to see each tunnel's last backup."


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
    profile_id = current_profile_id(request)
    done, outcome = await finish_or_continue(back_up_all_tunnels(profile_id), name=f"backup-all-{profile_id}")
    if not done or outcome is None:
        return await _render_home(request, notice=("muted", BACKGROUND_NOTICE))
    return await _render_home(request, feedback=outcome.feedback, notice=(outcome.tone, outcome.message))


@router.post("/tunnels/{tunnel_id}/backup", response_model=None)
async def backup_tunnel_action(request: Request, tunnel_id: str) -> Response:
    if not is_valid_tunnel_id(tunnel_id):
        return await _render_home(
            request, notice=("error", "Unknown tunnel ID."), status_code=200 if _is_htmx(request) else 400
        )
    tunnel_id = tunnel_id.lower()
    try:
        profile_id = current_profile_id(request)
        creds = require_api_credentials(profile_id)
    except HTTPException as exc:
        return await _render_home(request, notice=("error", str(exc.detail)))
    if DEMO_MODE:
        # Demo: hand the snapshot over as a download; nothing is written and nothing is notified.
        try:
            snapshot = await snapshot_for_download(creds, tunnel_id)
        except HTTPException as exc:
            return await _render_home(request, feedback={tunnel_id: RowFeedback(ok=False, message=str(exc.detail))})
        return Response(
            json.dumps(snapshot.payload, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{snapshot.filename}"'},
        )
    try:
        backup = await back_up_tunnel(creds, tunnel_id, profile_id=profile_id)
    except HTTPException as exc:
        return await _render_home(request, feedback={tunnel_id: RowFeedback(ok=False, message=str(exc.detail))})
    return await _render_home(request, feedback={tunnel_id: backup_saved_feedback(backup)})
