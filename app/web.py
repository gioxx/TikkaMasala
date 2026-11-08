"""Jinja environment, query-string parsing and the shared page renderers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.access import ACCESS_LOGOUT_PATH, access_identity
from app.archive import archive_url, build_archive_page, parse_archive_query
from app.backups import (
    get_backup,
    get_backup_retention_status,
    get_database_stats,
    get_restore_history,
    load_backup_json,
)
from app.config import (
    APP_VERSION,
    BACKUPS_PAGE_SIZE,
    BACKUPS_PAGE_SIZE_OPTIONS,
    BASE_DIR,
    DEMO_MODE,
    SCHEDULED_RUNS_PAGE_SIZE,
    SCHEDULED_RUNS_PAGE_SIZE_OPTIONS,
)
from app.credentials import (
    DEMO_NO_CREDENTIALS_MESSAGE,
    resolve_restore_target,
)
from app.ingress import IngressDiff, extract_ingress, fragile_rules, origin_request_delta
from app.notifications import env_notification_destinations
from app.profiles import current_profile_id, get_profile
from app.scheduler import (
    build_auto_backup_status,
    get_scheduled_runs_page,
    get_scheduled_runs_stats,
    summarize_scheduled_run_details,
)
from app.tunnels import RowFeedback, TunnelsHome, format_when


@dataclass(frozen=True)
class NavSection:
    """One entry of the top navigation; ``icon`` names a Tabler icon drawn by ``base.html``."""

    key: str
    label: str
    href: str
    icon: str


_NAV_SECTIONS = (
    NavSection("tunnels", "Tunnels", "/", "network"),
    NavSection("hostnames", "Hostnames", "/ingress", "world-www"),
    NavSection("backups", "Backups", "/backups", "archive"),
    NavSection("automation", "Automation", "/automation", "clock-play"),
    NavSection("settings", "Settings", "/settings", "settings"),
)
# Hostnames, Backups and Automation redirect to "/" in demo mode, so the nav hides them.
_DEMO_NAV_KEYS = frozenset({"tunnels", "settings"})


def nav_sections() -> list[NavSection]:
    """The top navigation sections; reads ``DEMO_MODE`` at call time so tests can patch it."""

    if DEMO_MODE:
        return [section for section in _NAV_SECTIONS if section.key in _DEMO_NAV_KEYS]
    return list(_NAV_SECTIONS)


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["access_identity"] = access_identity
templates.env.globals["access_logout_path"] = ACCESS_LOGOUT_PATH
templates.env.globals["origin_request_delta"] = origin_request_delta
templates.env.globals["nav_sections"] = nav_sections
templates.env.globals["archive_url"] = archive_url
# Short, human times in the scheduler timezone ("today 03:00", "3 days ago").
templates.env.filters["when"] = lambda value: format_when(value)


def get_requested_scheduled_runs_page(request: Request) -> int:
    raw_page = (request.query_params.get("page") or "1").strip()
    try:
        return max(int(raw_page), 1)
    except ValueError:
        return 1


def get_requested_scheduled_runs_page_size(request: Request) -> int | None:
    raw_page_size = (request.query_params.get("page_size") or str(SCHEDULED_RUNS_PAGE_SIZE)).strip().lower()
    if raw_page_size == "all":
        return None
    try:
        page_size = int(raw_page_size)
    except ValueError:
        return SCHEDULED_RUNS_PAGE_SIZE
    return page_size if page_size in SCHEDULED_RUNS_PAGE_SIZE_OPTIONS else SCHEDULED_RUNS_PAGE_SIZE


def render_tunnels_page(
    request: Request,
    home: TunnelsHome,
    *,
    feedback: dict[str, RowFeedback] | None = None,
    notice: tuple[str, str] | None = None,
    partial: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    """The Tunnels home, or with ``partial`` only its ``#tunnels-home`` region (for htmx swaps).

    ``notice`` is ``(tone, message)`` with tone ``success``, ``error`` or ``muted``;
    ``feedback`` maps a tunnel ID to the outcome of an action on its row.
    """
    response = templates.TemplateResponse(
        request,
        "partials/tunnels_home.html" if partial else "tunnels.html",
        {
            "home": home,
            # Keyed by lower-case tunnel ID; the template looks rows up the same way.
            "feedback": {tunnel_id.lower(): outcome for tunnel_id, outcome in (feedback or {}).items()},
            "notice": notice,
            "live_oob": partial,
            "demo_mode": DEMO_MODE,
            "demo_no_credentials_message": DEMO_NO_CREDENTIALS_MESSAGE,
        },
        status_code=status_code,
    )
    if partial:
        response.headers["Vary"] = "HX-Request"
    return response


def render_backups_page(request: Request) -> HTMLResponse:
    """The backup archive of the active profile (or of other accounts), filtered and paginated."""
    profile_id = current_profile_id(request)
    archive = build_archive_page(profile_id, parse_archive_query(request.query_params))
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "archive": archive,
            "profile_name": get_profile(profile_id).name,
            "default_page_size": BACKUPS_PAGE_SIZE,
            "backup_page_size_options": sorted(BACKUPS_PAGE_SIZE_OPTIONS),
            "database_stats": get_database_stats(),
            "backup_retention": get_backup_retention_status(profile_id),
            "other_retention": get_backup_retention_status(None),
            "demo_mode": DEMO_MODE,
        },
    )


def _profile_retention_value(profile_id: int) -> int | None:
    try:
        return get_profile(profile_id).retention_days
    except LookupError:
        return None


def render_automation_page(
    request: Request,
    *,
    notice: tuple[str, str, str] | None = None,
    retention_input: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The Automation overview. ``notice`` is ``(section, tone, message)``; the section names the card
    the notice sits in (``schedule``, ``run``, ``retention``, ``notifications``)."""
    profile_id = current_profile_id(request)
    return templates.TemplateResponse(
        request,
        "automation.html",
        {
            "auto_backup": build_auto_backup_status(profile_id),
            "backup_retention": get_backup_retention_status(profile_id),
            "destinations": env_notification_destinations(),
            "notice": notice,
            "retention_input": retention_input,
            "retention_value": _profile_retention_value(profile_id),
            "demo_mode": DEMO_MODE,
        },
        status_code=status_code,
    )


def render_backup_page(
    request: Request,
    backup_id: int,
    message: str | None = None,
    error: str | None = None,
    ingress_diff: IngressDiff | None = None,
    settings_delta: list[tuple[str, Any, Any]] | None = None,
    form_tunnel_id: str | None = None,
    created: bool = False,
) -> HTMLResponse:
    backup = get_backup(backup_id)
    content = load_backup_json(backup_id)
    restores = get_restore_history(backup_id)
    ingress_rules = extract_ingress(content)
    restore_target = resolve_restore_target(backup.account_id)
    return templates.TemplateResponse(
        request,
        "backup.html",
        {
            "backup": backup,
            "restores": restores,
            "content": json.dumps(content, indent=2, ensure_ascii=False),
            "ingress_rules": ingress_rules,
            "fragile_rules": fragile_rules(ingress_rules),
            "ingress_diff": ingress_diff,
            "settings_delta": settings_delta,
            "message": message,
            "error": error,
            "restore_target": restore_target,
            "created": created,
            "form_tunnel_id": form_tunnel_id,
            "demo_mode": DEMO_MODE,
        },
    )


def render_scheduled_runs_page(request: Request) -> HTMLResponse:
    profile_id = current_profile_id(request)
    runs, scheduled_runs_pagination = get_scheduled_runs_page(
        profile_id,
        get_requested_scheduled_runs_page(request),
        get_requested_scheduled_runs_page_size(request),
    )
    scheduled_runs_stats = get_scheduled_runs_stats(profile_id)
    run_items = [
        {
            "record": run,
            "details_summary": summarize_scheduled_run_details(run.details),
        }
        for run in runs
    ]
    return templates.TemplateResponse(
        request,
        "scheduled_runs.html",
        {
            "runs": run_items,
            "scheduled_runs_pagination": scheduled_runs_pagination,
            "scheduled_runs_page_size_options": SCHEDULED_RUNS_PAGE_SIZE_OPTIONS,
            "scheduled_runs_stats": scheduled_runs_stats,
            "demo_mode": DEMO_MODE,
        },
    )
