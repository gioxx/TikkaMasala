from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "app.db"
API_BASE = os.getenv("CLOUDFLARE_API_BASE", "https://api.cloudflare.com/client/v4")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
DEFAULT_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
DEFAULT_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
API_TOKEN_COOKIE_NAME = "cf_api_token"
API_TOKEN_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
ENCRYPTED_VALUE_PREFIX = "enc:"

BACKUP_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Cloudflare Tunnel Route Backup")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger(__name__)


@dataclass
class BackupRecord:
    id: int
    created_at: str
    account_id: str
    tunnel_id: str
    tunnel_name: str | None
    route_count: int
    filename: str
    notes: str | None


@dataclass
class RestoreRecord:
    id: int
    backup_id: int
    restored_at: str
    account_id: str
    tunnel_id: str


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                account_id TEXT NOT NULL,
                tunnel_id TEXT NOT NULL,
                tunnel_name TEXT,
                route_count INTEGER NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS restores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_id INTEGER NOT NULL,
                restored_at TEXT NOT NULL,
                account_id TEXT NOT NULL,
                tunnel_id TEXT NOT NULL,
                FOREIGN KEY (backup_id) REFERENCES backups(id)
            )
            """
        )
        conn.commit()


init_db()


def headers(api_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_token.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_setting(key: str) -> str | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def delete_setting(key: str) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def get_saved_account_id() -> str:
    return (get_setting("account_id") or DEFAULT_ACCOUNT_ID).strip()


def get_account_id_source() -> str | None:
    stored_value = (get_setting("account_id") or "").strip()
    if stored_value:
        return "database"
    if DEFAULT_ACCOUNT_ID:
        return "environment"
    return None


def resolve_account_id(account_id: str | None) -> str:
    resolved = (account_id or "").strip() or get_saved_account_id()
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="Account ID is required. Set it in the form or via CLOUDFLARE_ACCOUNT_ID.",
        )
    return resolved


def remember_account_id(account_id: str) -> None:
    normalized = account_id.strip()
    if normalized:
        set_setting("account_id", normalized)


def get_fernet() -> Fernet | None:
    if not TOKEN_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode("utf-8"))
    except (ValueError, TypeError):
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is invalid. Provide a valid Fernet key.")


def encrypt_secret(value: str) -> str:
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to persist API tokens securely.")
    token = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_secret(value: str) -> str:
    if not value.startswith(ENCRYPTED_VALUE_PREFIX):
        return value
    fernet = get_fernet()
    if fernet is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to read encrypted API tokens from the database.")
    encrypted = value.removeprefix(ENCRYPTED_VALUE_PREFIX).encode("utf-8")
    try:
        return fernet.decrypt(encrypted).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Failed to decrypt stored API token. Check TOKEN_ENCRYPTION_KEY.") from exc


def get_saved_api_token(request: Request) -> str:
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return cookie_token
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypted = decrypt_secret(stored_token).strip()
            if decrypted and not stored_token.startswith(ENCRYPTED_VALUE_PREFIX):
                remember_api_token(decrypted)
            return decrypted
        except RuntimeError:
            logger.exception("Unable to decrypt saved API token; falling back to environment/default prefill.")
    return DEFAULT_API_TOKEN.strip()


def get_api_token_source(request: Request) -> str | None:
    cookie_token = request.cookies.get(API_TOKEN_COOKIE_NAME, "").strip()
    if cookie_token:
        return "browser"
    stored_token = (get_setting("api_token") or "").strip()
    if stored_token:
        try:
            decrypt_secret(stored_token)
            return "database"
        except RuntimeError:
            logger.exception("Saved API token exists in the database but is not readable with the current key.")
    if DEFAULT_API_TOKEN:
        return "environment"
    return None


def resolve_api_token(request: Request, api_token: str | None) -> str:
    resolved = (api_token or "").strip() or get_saved_api_token(request)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="API token is required. Set it in the form or via CLOUDFLARE_API_TOKEN.",
        )
    return resolved


def remember_api_token(api_token: str) -> None:
    normalized = api_token.strip()
    if normalized and TOKEN_ENCRYPTION_KEY:
        set_setting("api_token", encrypt_secret(normalized))


def set_api_token_cookie(response: HTMLResponse | RedirectResponse, api_token: str) -> None:
    response.set_cookie(
        key=API_TOKEN_COOKIE_NAME,
        value=api_token.strip(),
        max_age=API_TOKEN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_api_token_cookie(response: HTMLResponse | RedirectResponse) -> None:
    response.delete_cookie(key=API_TOKEN_COOKIE_NAME, samesite="lax")


def build_prefill_status_from_sources(account_source: str | None, api_token_source: str | None) -> dict[str, Any]:
    return {
        "account_id_source": account_source,
        "api_token_source": api_token_source,
        "has_prefill": bool(account_source or api_token_source),
        "uses_environment_prefill": "environment" in {account_source, api_token_source},
        "uses_database_prefill": "database" in {account_source, api_token_source},
        "uses_browser_prefill": api_token_source == "browser",
        "can_persist_api_token": bool(TOKEN_ENCRYPTION_KEY),
    }


def build_prefill_status(request: Request) -> dict[str, Any]:
    return build_prefill_status_from_sources(get_account_id_source(), get_api_token_source(request))


async def cloudflare_get(account_id: str, path: str, api_token: str) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers(api_token))
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=message)
    return data


async def cloudflare_put(account_id: str, path: str, api_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.put(url, headers=headers(api_token), json=payload)
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=message)
    return data


async def verify_token(api_token: str) -> dict[str, Any]:
    url = f"{API_BASE}/user/tokens/verify"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(url, headers=headers(api_token))
    data = response.json()
    if response.status_code >= 400 or not data.get("success", False):
        message = extract_error_message(data)
        raise HTTPException(status_code=400, detail=f"Token verification failed: {message}")
    return data


async def fetch_tunnel_configuration(account_id: str, tunnel_id: str, api_token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    tunnel_data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}", api_token)
    config_data = await cloudflare_get(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token)
    return tunnel_data, config_data


async def list_tunnels(account_id: str, api_token: str) -> list[dict[str, Any]]:
    data = await cloudflare_get(account_id, "cfd_tunnel", api_token)
    result = data.get("result", [])
    if isinstance(result, list):
        return result
    return result if isinstance(result, list) else []


async def create_backup(account_id: str, tunnel_id: str, api_token: str, notes: str | None = None) -> BackupRecord:
    tunnel_data, config_data = await fetch_tunnel_configuration(account_id, tunnel_id, api_token)
    tunnel = tunnel_data.get("result", {})
    config = config_data.get("result", {})

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tunnel_name = tunnel.get("name") or "Unknown tunnel"
    ingress = config.get("config", {}).get("ingress", [])
    route_count = len(ingress)
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{tunnel_id}.json"

    payload = {
        "exported_at": created_at,
        "account_id": account_id,
        "tunnel_id": tunnel_id,
        "tunnel": tunnel,
        "configuration": config,
        "notes": notes or "",
    }

    file_path = BACKUP_DIR / filename
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    with closing(db()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO backups (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes or ""),
        )
        conn.commit()
        backup_id = cursor.lastrowid

    return BackupRecord(
        id=backup_id,
        created_at=created_at,
        account_id=account_id,
        tunnel_id=tunnel_id,
        tunnel_name=tunnel_name,
        route_count=route_count,
        filename=filename,
        notes=notes,
    )


def extract_error_message(payload: dict[str, Any]) -> str:
    errors = payload.get("errors") or []
    if errors:
        first = errors[0]
        code = first.get("code")
        message = first.get("message", "Unknown Cloudflare API error")
        return f"{message} (code {code})" if code else message
    messages = payload.get("messages") or []
    if messages:
        first = messages[0]
        return first.get("message", "Unknown Cloudflare API response")
    return "Unknown Cloudflare API error"


def get_backups() -> list[BackupRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes FROM backups ORDER BY id DESC"
        ).fetchall()
    return [BackupRecord(**dict(row)) for row in rows]


def get_backup(backup_id: int) -> BackupRecord:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id, created_at, account_id, tunnel_id, tunnel_name, route_count, filename, notes FROM backups WHERE id = ?",
            (backup_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return BackupRecord(**dict(row))


def load_backup_json(backup_id: int) -> dict[str, Any]:
    backup = get_backup(backup_id)
    file_path = BACKUP_DIR / backup.filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    with file_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def record_restore(backup_id: int, account_id: str, tunnel_id: str) -> None:
    restored_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO restores (backup_id, restored_at, account_id, tunnel_id)
            VALUES (?, ?, ?, ?)
            """,
            (backup_id, restored_at, account_id, tunnel_id),
        )
        conn.commit()


def get_restore_history(backup_id: int) -> list[RestoreRecord]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, backup_id, restored_at, account_id, tunnel_id
            FROM restores
            WHERE backup_id = ?
            ORDER BY id DESC
            """,
            (backup_id,),
        ).fetchall()
    return [RestoreRecord(**dict(row)) for row in rows]


def render_backup_page(
    request: Request,
    backup_id: int,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    backup = get_backup(backup_id)
    content = load_backup_json(backup_id)
    restores = get_restore_history(backup_id)
    return templates.TemplateResponse(
        request,
        "backup.html",
        {
            "backup": backup,
            "restores": restores,
            "content": json.dumps(content, indent=2, ensure_ascii=False),
            "message": message,
            "error": error,
            "prefill_account_id": get_saved_account_id(),
            "prefill_api_token": get_saved_api_token(request),
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    success_target: str | None = "tunnels",
    prefill_status: dict[str, Any] | None = None,
    prefill_account_id: str | None = None,
    prefill_api_token: str | None = None,
) -> HTMLResponse:
    backups = get_backups()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "message": message,
            "error": error,
            "success_target": success_target,
            "backups": backups,
            "prefill_status": prefill_status or build_prefill_status(request),
            "prefill_account_id": prefill_account_id if prefill_account_id is not None else get_saved_account_id(),
            "prefill_api_token": prefill_api_token if prefill_api_token is not None else get_saved_api_token(request),
        },
    )


@app.post("/verify-token", response_class=HTMLResponse)
async def verify_token_action(request: Request, api_token: str = Form(default="")) -> HTMLResponse:
    try:
        api_token = resolve_api_token(request, api_token)
        result = await verify_token(api_token)
        status = result.get("result", {}).get("status", "unknown")
        message = f"Token verified successfully. Status: {status}."
        remember_api_token(api_token)
        response = await index(
            request,
            message=message,
            success_target="tunnels",
            prefill_status=build_prefill_status_from_sources(get_account_id_source(), "browser"),
            prefill_account_id=get_saved_account_id(),
            prefill_api_token=api_token,
        )
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        return await index(request, error=exc.detail)


@app.post("/list-tunnels", response_class=HTMLResponse)
async def list_tunnels_action(request: Request, account_id: str = Form(default=""), api_token: str = Form(default="")) -> HTMLResponse:
    try:
        account_id = resolve_account_id(account_id)
        api_token = resolve_api_token(request, api_token)
        tunnels = await list_tunnels(account_id, api_token)
        remember_account_id(account_id)
        remember_api_token(api_token)
        backups = get_backups()
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "message": f"Loaded {len(tunnels)} tunnel(s).",
                "error": None,
                "success_target": "tunnels",
                "backups": backups,
                "tunnels": tunnels,
                "prefill_status": build_prefill_status_from_sources("database", "browser"),
                "prefill_account_id": account_id,
                "prefill_api_token": api_token,
            },
        )
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        return await index(request, error=exc.detail)


@app.post("/backup", response_class=HTMLResponse)
async def backup_action(
    request: Request,
    account_id: str = Form(default=""),
    tunnel_id: str = Form(...),
    api_token: str = Form(default=""),
    notes: str | None = Form(default=None),
) -> HTMLResponse:
    try:
        account_id = resolve_account_id(account_id)
        api_token = resolve_api_token(request, api_token)
        backup = await create_backup(account_id, tunnel_id, api_token, notes)
        remember_account_id(account_id)
        remember_api_token(api_token)
        response = await index(
            request,
            message=f"Backup created: #{backup.id} for tunnel {backup.tunnel_name}.",
            success_target="backup",
            prefill_status=build_prefill_status_from_sources("database", "browser"),
            prefill_account_id=account_id,
            prefill_api_token=api_token,
        )
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        return await index(request, error=exc.detail)


@app.post("/clear-saved-auth", response_class=HTMLResponse)
async def clear_saved_auth(request: Request) -> HTMLResponse:
    delete_setting("account_id")
    delete_setting("api_token")
    response = await index(
        request,
        message="Saved authentication data removed from the database. Backups were left untouched.",
        success_target="tunnels",
        prefill_status=build_prefill_status_from_sources(
            "environment" if DEFAULT_ACCOUNT_ID else None,
            "environment" if DEFAULT_API_TOKEN else None,
        ),
        prefill_account_id=DEFAULT_ACCOUNT_ID,
        prefill_api_token=DEFAULT_API_TOKEN,
    )
    clear_api_token_cookie(response)
    return response


@app.get("/backup/{backup_id}", response_class=HTMLResponse)
async def backup_details(request: Request, backup_id: int) -> HTMLResponse:
    return render_backup_page(request, backup_id)


@app.get("/backup/{backup_id}/download")
async def download_backup(backup_id: int) -> FileResponse:
    backup = get_backup(backup_id)
    file_path = BACKUP_DIR / backup.filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")
    return FileResponse(path=file_path, filename=backup.filename, media_type="application/json")


@app.post("/backup/{backup_id}/restore", response_class=HTMLResponse)
async def restore_backup(
    request: Request,
    backup_id: int,
    account_id: str = Form(default=""),
    tunnel_id: str = Form(...),
    api_token: str = Form(default=""),
) -> HTMLResponse:
    try:
        account_id = resolve_account_id(account_id)
        api_token = resolve_api_token(request, api_token)
        payload = load_backup_json(backup_id)
        configuration = payload.get("configuration", {})
        config_body = configuration.get("config")
        if not isinstance(config_body, dict):
            raise HTTPException(status_code=400, detail="Backup file does not contain a restorable tunnel configuration")
        await cloudflare_put(account_id, f"cfd_tunnel/{tunnel_id}/configurations", api_token, {"config": config_body})
        remember_account_id(account_id)
        remember_api_token(api_token)
        record_restore(backup_id, account_id, tunnel_id)
        response = render_backup_page(request, backup_id, message="Backup restored successfully.")
        set_api_token_cookie(response, api_token)
        return response
    except HTTPException as exc:
        return render_backup_page(request, backup_id, error=exc.detail)


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
