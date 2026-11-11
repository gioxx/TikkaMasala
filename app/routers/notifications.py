"""Notification message editor."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import DEMO_MODE
from app.notifications import (
    DEFAULT_NOTIFICATION_MESSAGES,
    NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS,
    NOTIFICATION_MESSAGE_PLACEHOLDERS,
    get_notification_message_templates,
    get_telegram_compact_notifications,
    humanize_notification_event,
    set_notification_message_templates,
    set_telegram_compact_notifications,
)
from app.web import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings/notifications", response_class=HTMLResponse)
async def notification_messages_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    templates_data = get_notification_message_templates()
    return templates.TemplateResponse(
        request,
        "notification_messages.html",
        {
            "events": list(DEFAULT_NOTIFICATION_MESSAGES.keys()),
            "event_label": humanize_notification_event,
            "defaults": DEFAULT_NOTIFICATION_MESSAGES,
            "placeholders": NOTIFICATION_MESSAGE_PLACEHOLDERS,
            "global_placeholders": NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS,
            "saved": templates_data,
            "telegram_compact": get_telegram_compact_notifications(),
            "demo_mode": DEMO_MODE,
        },
    )


@router.post("/settings/notifications", response_class=HTMLResponse)
async def notification_messages_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    try:
        form = await request.form()
        new_templates: dict[str, str] = {}
        for event in DEFAULT_NOTIFICATION_MESSAGES:
            value = str(form.get(f"msg_{event}", "")).strip()
            if value and value != DEFAULT_NOTIFICATION_MESSAGES[event]:
                new_templates[event] = value
        set_notification_message_templates(new_templates)
        compact_flag = form.get("telegram_compact") == "on"
        set_telegram_compact_notifications(compact_flag)
        logger.info(
            "Notification message templates updated (%s custom, telegram_compact=%s).",
            len(new_templates),
            compact_flag,
        )
        return RedirectResponse(url="/settings/notifications?saved=1", status_code=303)
    except Exception:
        logger.exception("Unexpected error saving notification message templates.")
        return RedirectResponse(url="/settings/notifications?error=1", status_code=303)


@router.post("/settings/notifications/reset", response_class=HTMLResponse)
async def notification_messages_reset_action() -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    set_notification_message_templates({})
    logger.info("Notification message templates reset to defaults.")
    return RedirectResponse(url="/settings/notifications?reset=1", status_code=303)
