"""Jinja environment, query-string parsing and the shared page renderers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.access import ACCESS_LOGOUT_PATH, access_identity
from app.backups import (
    get_backup,
    get_backup_retention_status,
    get_backups_page,
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
    find_profile_for_account,
    get_api_credentials,
)
from app.ingress import IngressDiff, extract_ingress, fragile_rules, origin_request_delta
from app.profiles import current_profile_id, get_profile
from app.scheduler import (
    build_auto_backup_status,
    get_scheduled_runs_page,
    get_scheduled_runs_stats,
    summarize_scheduled_run_details,
)


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["access_identity"] = access_identity
templates.env.globals["access_logout_path"] = ACCESS_LOGOUT_PATH
templates.env.globals["origin_request_delta"] = origin_request_delta


def get_requested_backup_page(request: Request) -> int:
    raw_page = (request.query_params.get("page") or "1").strip()
    try:
        return max(int(raw_page), 1)
    except ValueError:
        return 1


def get_requested_backup_page_size(request: Request) -> int:
    raw_page_size = (request.query_params.get("page_size") or str(BACKUPS_PAGE_SIZE)).strip()
    try:
        page_size = int(raw_page_size)
    except ValueError:
        return BACKUPS_PAGE_SIZE
    return page_size if page_size in BACKUPS_PAGE_SIZE_OPTIONS else BACKUPS_PAGE_SIZE


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


def render_index_page(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    success_target: str | None = "tunnels",
    tunnels: list[dict[str, Any]] | None = None,
    backup_page: int | None = None,
    backup_page_size: int | None = None,
) -> HTMLResponse:
    resolved_page = backup_page if isinstance(backup_page, int) else get_requested_backup_page(request)
    resolved_page_size = backup_page_size if isinstance(backup_page_size, int) else get_requested_backup_page_size(request)
    backups, backup_pagination = get_backups_page(resolved_page, resolved_page_size)
    profile_id = current_profile_id(request)
    credentials = get_api_credentials(profile_id)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "message": message,
            "error": error,
            "success_target": success_target,
            "backups": backups,
            "backup_pagination": backup_pagination,
            "backup_page_size_options": sorted(BACKUPS_PAGE_SIZE_OPTIONS),
            "database_stats": get_database_stats(),
            "backup_retention": get_backup_retention_status(profile_id),
            "tunnels": tunnels,
            "credentials": credentials,
            "demo_missing_credentials": DEMO_MODE and credentials is None,
            "demo_no_credentials_message": DEMO_NO_CREDENTIALS_MESSAGE,
            "auto_backup": build_auto_backup_status(profile_id),
            "demo_mode": DEMO_MODE,
        },
    )


def render_backup_page(
    request: Request,
    backup_id: int,
    message: str | None = None,
    error: str | None = None,
    ingress_diff: IngressDiff | None = None,
    settings_delta: list[tuple[str, Any, Any]] | None = None,
    form_tunnel_id: str | None = None,
) -> HTMLResponse:
    backup = get_backup(backup_id)
    content = load_backup_json(backup_id)
    restores = get_restore_history(backup_id)
    ingress_rules = extract_ingress(content)
    restore_profile_id = find_profile_for_account(backup.account_id)
    restore_profile_name = None
    if restore_profile_id is not None and get_api_credentials(restore_profile_id) is not None:
        restore_profile_name = get_profile(restore_profile_id).name
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
            "restore_profile_name": restore_profile_name,
            "restore_account_id": backup.account_id,
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
