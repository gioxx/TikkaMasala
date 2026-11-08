"""Notification destinations of the active profile, on Automation: add, edit, delete and test them.

Every route that names a destination loads it first and answers 404 unless it belongs to the active
profile; the repository calls repeat that check in SQL. Secrets (webhook URL, bot token) are never
put back into a form, not even after a validation error, and a test reports only success or the
failure reason (HTTP status or exception type), never a response body.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import DEFAULT_NOTIFICATION_EVENTS, DEMO_MODE
from app.destinations import (
    DESTINATION_EVENTS,
    DESTINATION_KINDS,
    LABEL_MAX,
    DestinationInput,
    DestinationInputError,
    DestinationView,
    create_destination,
    delete_destination,
    get_destination,
    update_destination,
)
from app.notifications import (
    deliver_notification,
    env_delivery_targets,
    humanize_notification_event,
    render_notification_message,
)
from app.profiles import DEFAULT_PROFILE_ID, current_profile_id
from app.web import render_automation_page, templates

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_FOUND = "Destination not found."


def _home() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=303)


def _back(result: str) -> RedirectResponse:
    return RedirectResponse(url=f"/automation?destination={result}#notifications-section", status_code=303)


def _owned(request: Request, destination_id: int) -> DestinationView:
    """The destination when it belongs to the active profile; 404 otherwise (missing or another profile's)."""
    try:
        view = get_destination(destination_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    if view.profile_id != current_profile_id(request):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return view


def _render_form(
    request: Request,
    *,
    kind: str,
    destination: DestinationView | None,
    label: str,
    chat_id: str,
    events: Iterable[str],
    enabled: bool,
    error: str | None = None,
    error_field: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The add / edit form. It never receives the secret: the field always starts empty."""
    chosen = set(events)
    return templates.TemplateResponse(
        request,
        "destination_form.html",
        {
            "kind": kind,
            "destination": destination,
            "label": label,
            "label_max": LABEL_MAX,
            "chat_id": chat_id,
            "event_options": [
                (event, humanize_notification_event(event), event in chosen) for event in DESTINATION_EVENTS
            ],
            "enabled": enabled,
            "error": error,
            "error_field": error_field,
            "demo_mode": DEMO_MODE,
        },
        status_code=status_code,
    )


@router.get("/automation/destinations/new", response_class=HTMLResponse)
async def destination_new_page(request: Request, kind: str = "webhook") -> Response:
    if DEMO_MODE:
        return _home()
    if kind not in DESTINATION_KINDS:
        kind = "webhook"
    return _render_form(
        request,
        kind=kind,
        destination=None,
        label="",
        chat_id="",
        events=DEFAULT_NOTIFICATION_EVENTS,
        enabled=True,
    )


@router.post("/automation/destinations", response_class=HTMLResponse)
async def destination_create_action(
    request: Request,
    kind: str = Form(default="webhook"),
    label: str = Form(default=""),
    secret: str = Form(default=""),
    chat_id: str = Form(default=""),
    events: list[str] = Form(default=[]),
    enabled: str | None = Form(default=None),
) -> Response:
    if DEMO_MODE:
        return _home()
    profile_id = current_profile_id(request)
    data = DestinationInput(
        kind=kind, label=label, secret=secret, chat_id=chat_id, events=tuple(events), enabled=enabled == "on"
    )
    try:
        view = create_destination(profile_id, data)
    except DestinationInputError as exc:
        return _render_form(
            request,
            kind=kind if kind in DESTINATION_KINDS else "webhook",
            destination=None,
            label=label,
            chat_id=chat_id,
            events=events,
            enabled=enabled == "on",
            error=str(exc),
            error_field=exc.field,
            status_code=400,
        )
    logger.info("Notification destination %s (%s) added to profile %s.", view.id, view.kind, profile_id)
    return _back("added")


@router.get("/automation/destinations/{destination_id:int}/edit", response_class=HTMLResponse)
async def destination_edit_page(request: Request, destination_id: int) -> Response:
    if DEMO_MODE:
        return _home()
    view = _owned(request, destination_id)
    return _render_form(
        request,
        kind=view.kind,
        destination=view,
        label=view.label,
        chat_id=view.chat_id,
        events=view.events,
        enabled=view.enabled,
    )


@router.post("/automation/destinations/{destination_id:int}", response_class=HTMLResponse)
async def destination_update_action(
    request: Request,
    destination_id: int,
    label: str = Form(default=""),
    secret: str = Form(default=""),
    chat_id: str = Form(default=""),
    events: list[str] = Form(default=[]),
    enabled: str | None = Form(default=None),
) -> Response:
    if DEMO_MODE:
        return _home()
    view = _owned(request, destination_id)
    # The kind is the stored one: a destination cannot change kind, so the form does not send it.
    data = DestinationInput(
        kind=view.kind, label=label, secret=secret, chat_id=chat_id, events=tuple(events), enabled=enabled == "on"
    )
    try:
        update_destination(destination_id, data, profile_id=view.profile_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    except DestinationInputError as exc:
        return _render_form(
            request,
            kind=view.kind,
            destination=view,
            label=label,
            chat_id=chat_id,
            events=events,
            enabled=enabled == "on",
            error=str(exc),
            error_field=exc.field,
            status_code=400,
        )
    logger.info("Notification destination %s of profile %s saved.", destination_id, view.profile_id)
    return _back("saved")


@router.post("/automation/destinations/{destination_id:int}/delete")
async def destination_delete_action(request: Request, destination_id: int) -> Response:
    if DEMO_MODE:
        return _home()
    view = _owned(request, destination_id)
    try:
        delete_destination(destination_id, profile_id=view.profile_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    logger.info("Notification destination %s of profile %s deleted.", destination_id, view.profile_id)
    return _back("deleted")


async def _send_test(request: Request, profile_id: int, key: str, label: str) -> HTMLResponse:
    details: dict[str, Any] = {"configured_channels": label}
    result = await deliver_notification(
        "notification_test",
        render_notification_message("notification_test", details, profile_id=profile_id),
        details,
        level="info",
        force=True,
        profile_id=profile_id,
        only=key,
    )
    outcome = result["results"][0] if result["results"] else None
    if outcome is not None and outcome["success"]:
        return render_automation_page(
            request, notice=("notifications", "success", f"Test notification sent to {label}.")
        )
    reason = outcome["reason"] if outcome is not None else "the destination is not available"
    return render_automation_page(
        request, notice=("notifications", "error", f"Test notification to {label} failed: {reason}.")
    )


@router.post("/automation/destinations/env/{kind}/test", response_class=HTMLResponse)
async def destination_env_test_action(request: Request, kind: str) -> Response:
    if DEMO_MODE:
        return _home()
    profile_id = current_profile_id(request)
    key = f"env-{kind}"
    target = next((t for t in env_delivery_targets() if t.key == key), None)
    if kind not in DESTINATION_KINDS or profile_id != DEFAULT_PROFILE_ID or target is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    logger.info("Notification test of the %s environment destination requested.", kind)
    return await _send_test(request, profile_id, key, target.label)


@router.post("/automation/destinations/{destination_id:int}/test", response_class=HTMLResponse)
async def destination_test_action(request: Request, destination_id: int) -> Response:
    if DEMO_MODE:
        return _home()
    view = _owned(request, destination_id)
    if not view.enabled:
        return render_automation_page(request, notice=("notifications", "error", f"{view.label} is disabled."))
    if view.unreadable:
        return render_automation_page(
            request, notice=("notifications", "error", f"{view.label}: the saved secret cannot be decrypted.")
        )
    logger.info("Notification test of destination %s (profile %s) requested.", destination_id, view.profile_id)
    return await _send_test(request, view.profile_id, f"dest-{destination_id}", view.label)
