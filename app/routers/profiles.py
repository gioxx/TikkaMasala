"""Profile switch: choose which profile this browser works on."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse

from app.config import DEMO_MODE
from app.profiles import parse_profile_id, set_active_profile_cookie, switch_destination

router = APIRouter()


@router.post("/profiles/active")
async def switch_active_profile(
    profile_id: str = Form(""),
    next_url: str = Form("/", alias="next"),
) -> RedirectResponse:
    if DEMO_MODE:
        # Demo mode always works on Default; nothing to switch.
        return RedirectResponse("/", status_code=303)
    target_id = parse_profile_id(profile_id.strip())
    if target_id is None:
        raise HTTPException(status_code=404, detail="Profile not found.")
    response = RedirectResponse(switch_destination(next_url), status_code=303)
    set_active_profile_cookie(response, target_id)
    return response
