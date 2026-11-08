"""Notification message editor and test delivery."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import DEMO_MODE
from app.notifications import (
    DEFAULT_NOTIFICATION_MESSAGES,
    NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS,
    NOTIFICATION_MESSAGE_PLACEHOLDERS,
    deliver_notification,
    get_notification_message_templates,
    get_notification_status,
    get_telegram_compact_notifications,
    render_notification_message,
    set_notification_message_templates,
    set_telegram_compact_notifications,
)
from app.web import render_automation_page, templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/notifications/messages", response_class=HTMLResponse)
async def notification_messages_page(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    templates_data = get_notification_message_templates()
    return templates.TemplateResponse(
        request,
        "notification_messages.html",
        {
            "events": list(DEFAULT_NOTIFICATION_MESSAGES.keys()),
            "defaults": DEFAULT_NOTIFICATION_MESSAGES,
            "placeholders": NOTIFICATION_MESSAGE_PLACEHOLDERS,
            "global_placeholders": NOTIFICATION_MESSAGE_GLOBAL_PLACEHOLDERS,
            "saved": templates_data,
            "telegram_compact": get_telegram_compact_notifications(),
            "demo_mode": DEMO_MODE,
        },
    )


@router.post("/notifications/messages", response_class=HTMLResponse)
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
        return RedirectResponse(url="/notifications/messages?saved=1", status_code=303)
    except Exception:
        logger.exception("Unexpected error saving notification message templates.")
        return RedirectResponse(url="/notifications/messages?error=1", status_code=303)


@router.post("/notifications/messages/reset", response_class=HTMLResponse)
async def notification_messages_reset_action() -> HTMLResponse:
    if DEMO_MODE:
        return RedirectResponse(url="/", status_code=303)
    set_notification_message_templates({})
    logger.info("Notification message templates reset to defaults.")
    return RedirectResponse(url="/notifications/messages?reset=1", status_code=303)


@router.post("/notifications/test", response_class=HTMLResponse)
async def notifications_test_action(request: Request) -> HTMLResponse:
    if DEMO_MODE:
        return render_automation_page(request, error="Notifications are not available in Demo mode.", success_target="notifications")
    status = get_notification_status()
    if not status["webhook_enabled"] and not status["telegram_enabled"]:
        logger.warning("Notification test requested, but no notification channel is configured.")
        return render_automation_page(
            request,
            error="Notification test failed: no configured channel found. Set a webhook URL and/or Telegram bot settings first.",
            success_target="notifications",
        )

    logger.info(
        "Notification test requested from UI (channels=%s, events=%s).",
        status["channel_label"],
        ", ".join(status["events"]) if status["events"] else "none",
    )
    _nt_details: dict[str, Any] = {
        "configured_channels": status["channel_label"],
        "enabled_events": status["events"],
    }
    result = await deliver_notification(
        "notification_test",
        render_notification_message("notification_test", _nt_details),
        _nt_details,
        level="info",
        force=True,
    )
    logger.info(
        "Notification test finished (sent=%s, successful_channels=%s, failed_channels=%s).",
        result["sent_count"],
        ", ".join(result["successful_channels"]) or "none",
        ", ".join(result["failed_channels"]) or "none",
    )
    if result["sent_count"]:
        successful_channels = [channel.title() for channel in result["successful_channels"]]
        failed_channels = [channel.title() for channel in result["failed_channels"]]
        message = f"Notification test sent successfully via {', '.join(successful_channels)}."
        if result["failed_channels"]:
            message += f" Failed channels: {', '.join(failed_channels)}."
        return render_automation_page(request, message=message, success_target="notifications")

    return render_automation_page(
        request,
        error="Notification test failed. Check the container logs for channel-specific errors.",
        success_target="notifications",
    )
