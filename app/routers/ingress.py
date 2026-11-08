"""Hostnames page: every ingress rule of every tunnel in one table."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.archive import ArchiveQuery, build_archive_page
from app.config import DEMO_MODE
from app.credentials import get_api_credentials, get_saved_account_id
from app.ingress_overview import IngressOverview, build_overview, live_overview, snapshot_overview
from app.profiles import current_profile_id, get_profile
from app.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()

MISSING_CREDENTIALS_ERROR = "Live data needs an Account ID and an API token. Add them in Settings."


@router.get("/ingress", response_class=HTMLResponse)
async def ingress_overview_page(request: Request, source: str = "snapshot") -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)

    profile_id = current_profile_id(request)
    account_id = get_saved_account_id(profile_id).strip()
    has_account = bool(account_id)
    if source == "live":
        creds = get_api_credentials(profile_id)
        if creds is not None:
            logger.info("Live ingress overview requested (account_id=%s).", creds.account_id)
            overview = await live_overview(creds.account_id, creds.api_token)
        else:
            overview = IngressOverview(source="live", error=MISSING_CREDENTIALS_ERROR)
    else:
        # Same scope as Backups: a profile without an account sees no snapshot hostnames.
        overview = snapshot_overview(account_id) if has_account else build_overview("snapshot", [])

    # htmx swaps only need the results block. History restores also send
    # HX-Request but expect the full page, as do normal requests.
    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request")
    template = "partials/ingress_results.html" if is_partial else "ingress.html"
    other_account_backups = (
        build_archive_page(profile_id, ArchiveQuery(scope="other")).other_count if has_account else 0
    )
    response = templates.TemplateResponse(
        request,
        template,
        {
            "overview": overview,
            "demo_mode": DEMO_MODE,
            "profile_name": get_profile(profile_id).name,
            "has_account": has_account,
            "other_account_backups": other_account_backups,
        },
    )
    response.headers["Vary"] = "HX-Request"
    return response
