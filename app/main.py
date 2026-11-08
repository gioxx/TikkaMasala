"""FastAPI application: wiring of static files, routers and lifecycle hooks."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import setup
from app.access import AccessVerifier, access_guard, cross_site_guard, load_access_settings
from app.backups import get_backup_retention_days
from app.config import (
    API_BASE,
    AUTO_BACKUP_TIMEZONE,
    BASE_DIR,
    CF_ACCESS_AUD,
    CF_ACCESS_TEAM_DOMAIN,
    DATA_DIR,
    DEMO_MODE,
    LOG_LEVEL,
)
from app.notifications import get_notification_log_context, get_notification_status
from app.profiles import DEFAULT_PROFILE_ID, stale_profile_cookie_guard
from app.routers import automation, backups, dashboard, ingress, legacy, notifications, profiles, settings
from app.scheduler import auto_backup_scheduler, configure_all_auto_backup_jobs

logger = logging.getLogger(__name__)

# Raises AccessConfigError (and stops the app) when only one of the two variables is set.
ACCESS_SETTINGS = load_access_settings(CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD)


async def startup_event() -> None:
    if DEMO_MODE:
        logger.warning(
            "DEMO mode is active. Authentication data will not be persisted and the automatic backup scheduler is disabled."
        )
    logger.info(
        "Starting Tikka Masala (data_dir=%s, api_base=%s, log_level=%s, auto_backup_timezone=%s, retention_days=%s, notifications=%s, demo_mode=%s).",
        DATA_DIR,
        API_BASE,
        LOG_LEVEL,
        AUTO_BACKUP_TIMEZONE or "auto",
        get_backup_retention_days(DEFAULT_PROFILE_ID) or "disabled",
        get_notification_status()["summary_label"],
        DEMO_MODE,
    )
    notification_log_context = get_notification_log_context()
    logger.info(
        "Notification channels: summary=%s, events=%s, webhook_enabled=%s, telegram_enabled=%s, webhook_event_count=%s, telegram_event_count=%s.",
        notification_log_context["summary"],
        notification_log_context["events"],
        notification_log_context["webhook_enabled"],
        notification_log_context["telegram_enabled"],
        notification_log_context["webhook_event_count"],
        notification_log_context["telegram_event_count"],
    )
    if ACCESS_SETTINGS:
        logger.info(
            "Cloudflare Access enforcement is on (team_domain=%s). Requests without a valid Access token are rejected.",
            ACCESS_SETTINGS.team_domain,
        )
    else:
        logger.info("Cloudflare Access enforcement is off (CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD not set).")
    if not DEMO_MODE:
        if not auto_backup_scheduler.running:
            auto_backup_scheduler.start()
        configure_all_auto_backup_jobs()
    logger.info("Application startup complete.")


async def shutdown_event() -> None:
    logger.info("Shutting down Tikka Masala.")
    if auto_backup_scheduler.running:
        auto_backup_scheduler.shutdown(wait=False)
    logger.info("Application shutdown complete.")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await startup_event()
    try:
        yield
    finally:
        await shutdown_event()


app = FastAPI(title="Tikka Masala", lifespan=lifespan)
app.state.access_verifier = AccessVerifier(ACCESS_SETTINGS) if ACCESS_SETTINGS else None
# Last registered runs first: Access stays outermost, then the cross-site guard for
# unsafe methods, then the setup gate, then the stale profile cookie cleanup (innermost).
app.middleware("http")(stale_profile_cookie_guard)
app.middleware("http")(setup.credentials_guard)
app.middleware("http")(cross_site_guard)
app.middleware("http")(access_guard)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(setup.router)
app.include_router(dashboard.router)
app.include_router(automation.router)
app.include_router(notifications.router)
app.include_router(backups.router)
app.include_router(ingress.router)
app.include_router(settings.router)
app.include_router(profiles.router)
# Old URLs last: they only redirect to the routes above.
app.include_router(legacy.router)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
