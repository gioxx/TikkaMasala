"""Tunnels home: summary tiles and the live tunnel table of the active profile."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.profiles import current_profile_id
from app.tunnels import build_tunnels_home
from app.web import render_tunnels_page

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return render_tunnels_page(request, await build_tunnels_home(current_profile_id(request)))
