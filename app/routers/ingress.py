"""Hostnames page: every ingress rule of every tunnel in one table."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import DEMO_MODE
from app.credentials import get_saved_account_id, get_saved_api_token
from app.ingress_overview import IngressOverview, live_overview, snapshot_overview
from app.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()

MISSING_CREDENTIALS_ERROR = (
    "Live data needs an Account ID and an API token. Load your tunnels once from the dashboard, "
    "or set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN."
)


@router.get("/ingress", response_class=HTMLResponse)
async def ingress_overview_page(request: Request, source: str = "snapshot") -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)

    account_id = get_saved_account_id()
    if source == "live":
        api_token = get_saved_api_token(request)
        if account_id and api_token:
            logger.info("Live ingress overview requested (account_id=%s).", account_id)
            overview = await live_overview(account_id, api_token)
        else:
            overview = IngressOverview(source="live", error=MISSING_CREDENTIALS_ERROR)
    else:
        overview = snapshot_overview()

    # htmx swaps only need the results block. History restores also send
    # HX-Request but expect the full page, as do normal requests.
    is_partial = request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request")
    template = "partials/ingress_results.html" if is_partial else "ingress.html"
    response = templates.TemplateResponse(
        request,
        template,
        {"overview": overview, "demo_mode": DEMO_MODE},
    )
    response.headers["Vary"] = "HX-Request"
    return response
