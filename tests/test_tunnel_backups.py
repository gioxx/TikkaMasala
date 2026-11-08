"""Per-row "Back up" and "Back up all now" on the Tunnels home."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.credentials as credentials
import app.routers.dashboard as dashboard
import app.tunnels as tunnels
import app.web as web
from app.profiles import DEFAULT_PROFILE_ID
from app.scheduler import get_scheduled_runs_stats, set_auto_backup_tunnel_schedule

GOOD = "0123456789abcdef0123456789abcdef"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"
T3 = "33333333-3333-4333-8333-333333333333"
T4 = "44444444-4444-4444-8444-444444444444"
TOKEN = "test-token-0000-wxyz"
HX = {"HX-Request": "true"}

NAMES = {T1: "Home", T2: "Lab", T3: "Office", T4: "Old"}


def _listing(*ids: str) -> list[dict]:
    return [{"id": tunnel_id, "name": NAMES[tunnel_id], "status": "healthy"} for tunnel_id in ids]


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")


class _ListFake:
    def __init__(self, result=None, error: BaseException | None = None):
        self.calls: list[tuple] = []
        self.result = result if result is not None else []
        self.error = error

    async def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_list(monkeypatch):
    def _install(result=None, error: BaseException | None = None) -> _ListFake:
        fake = _ListFake(result, error)
        monkeypatch.setattr(tunnels, "list_tunnels", fake)
        return fake

    return _install


class _CreateFake:
    def __init__(self, errors: dict[str, BaseException] | None = None, result=None):
        self.calls: list[tuple] = []
        self.errors = errors or {}
        self.result = result

    async def __call__(self, account_id, tunnel_id, api_token, notes=None, *rest):
        self.calls.append((account_id, tunnel_id, api_token, notes))
        if tunnel_id in self.errors:
            raise self.errors[tunnel_id]
        if self.result is not None:
            return self.result
        return SimpleNamespace(id=42, tunnel_name=NAMES.get(tunnel_id, "Unknown"), route_count=1, filename="x.json")


@pytest.fixture
def fake_create(monkeypatch):
    def _install(errors: dict[str, BaseException] | None = None, result=None) -> _CreateFake:
        fake = _CreateFake(errors, result)
        monkeypatch.setattr(tunnels, "create_backup", fake)
        return fake

    return _install


@pytest.fixture
def events(monkeypatch):
    recorded: list[tuple[str, dict, str]] = []

    def _queue(event, message, details=None, level="info"):
        recorded.append((event, details or {}, level))

    monkeypatch.setattr(tunnels, "queue_notification", _queue)
    return recorded


def _demo(monkeypatch):
    for module in (credentials, tunnels, web, dashboard):
        monkeypatch.setattr(module, "DEMO_MODE", True)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", GOOD)
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "env-token-0000-abcd")


def _row(text: str, tunnel_id: str) -> str:
    start = text.index(f'id="tunnel-row-{tunnel_id}"')
    return text[start:text.index("</tr>", start)]


# --- per-row backup ---------------------------------------------------------


def test_row_backup_htmx_returns_region(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1, T2))
    create = fake_create()
    response = client.post(f"/tunnels/{T1}/backup", headers=HX)
    assert response.status_code == 200
    text = response.text
    assert text.lstrip().startswith('<div id="tunnels-home"')
    assert "<html" not in text
    assert "Backup #42 saved." in text
    assert 'href="/backup/42"' in _row(text, T1)
    assert create.calls == [(GOOD, T1, TOKEN, None)]
    assert "cf_api_token" not in response.headers.get("set-cookie", "")


def test_row_backup_updates_live_region_out_of_band(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    fake_create()
    text = client.post(f"/tunnels/{T1}/backup", headers=HX).text
    region_end = text.index('id="tunnels-live"')
    assert region_end > text.index('id="tunnels-home"')
    live = text[region_end:text.index("</div>", region_end)]
    assert 'hx-swap-oob="innerHTML"' in live
    assert "Home: Backup #42 saved." in live


def test_full_page_has_one_empty_live_region_outside_region(client, configured_default, fake_list):
    fake_list(_listing(T1))
    text = client.get("/").text
    assert text.count('id="tunnels-live"') == 1
    live_start = text.index('id="tunnels-live"')
    live = text[text.rindex("<", 0, live_start):text.index("</div>", live_start)]
    assert 'role="status"' in live and 'aria-live="polite"' in live
    assert "hx-swap-oob" not in live
    # Outside the swapped region: it comes before it, so an outerHTML swap never replaces it.
    assert live_start < text.index('id="tunnels-home"')


def test_row_backup_queues_success_notification(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    fake_create()
    client.post(f"/tunnels/{T1}/backup", headers=HX)
    assert [event for event, _, _ in events] == ["manual_backup_success"]
    _, details, level = events[0]
    assert set(details) == {"backup_id", "account_id", "tunnel_id", "tunnel_name", "route_count"}
    assert details["backup_id"] == 42
    assert details["tunnel_id"] == T1
    assert level == "info"


def test_row_backup_failure_inline(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1, T2))
    fake_create(errors={T1: HTTPException(status_code=502, detail="Cloudflare said no")})
    response = client.post(f"/tunnels/{T1}/backup", headers=HX)
    assert response.status_code == 200
    assert "Cloudflare said no" in _row(response.text, T1)
    assert "Cloudflare said no" not in _row(response.text, T2)
    assert [event for event, _, _ in events] == ["manual_backup_failed"]
    _, details, level = events[0]
    assert details["error"] == "Cloudflare said no"
    assert details["tunnel_id"] == T1
    assert level == "warning"


@pytest.mark.parametrize("tunnel_id", ["not-a-uuid", "..%2F..%2Fsecrets", f"{T1}%0A", f"{T1}x"])
def test_row_backup_rejects_bad_tunnel_id(client, configured_default, fake_list, fake_create, events, tunnel_id):
    fake_list(_listing(T1))
    create = fake_create()
    response = client.post(f"/tunnels/{tunnel_id}/backup", headers=HX)
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert "Unknown tunnel ID." in response.text
    assert create.calls == []
    assert events == []


def test_row_backup_rejects_trailing_newline(client, configured_default, fake_list, fake_create):
    fake_list(_listing(T1))
    create = fake_create()
    response = client.post(f"/tunnels/{T1}%0A/backup", headers=HX)
    assert response.status_code == 200
    assert "Unknown tunnel ID." in response.text
    assert create.calls == []


def test_tunnel_id_pattern_rejects_trailing_newline():
    assert tunnels.is_valid_tunnel_id(T1)
    assert not tunnels.is_valid_tunnel_id(f"{T1}\n")
    assert not tunnels.is_valid_tunnel_id("not-a-uuid")
    assert not tunnels.TUNNEL_ID_PATTERN.match(f"{T1}\n")


def test_row_backup_without_htmx_renders_full_page(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    fake_create()
    response = client.post(f"/tunnels/{T1}/backup")
    assert response.status_code == 200
    assert "<title>" in response.text
    assert "Backup #42 saved." in response.text


def test_row_backup_without_credentials(client, fake_list, fake_create, events):
    create = fake_create()
    response = client.post(f"/tunnels/{T1}/backup", headers=HX)
    assert response.status_code == 200
    assert "Add them in Settings" in response.text
    assert create.calls == []
    assert events == []


def _backup_state(data_dir) -> tuple[int, int]:
    from contextlib import closing

    from app.db import db

    with closing(db()) as conn:
        rows = int(conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0])
    files = sum(1 for item in (data_dir / "backups").iterdir() if item.is_file())
    return rows, files


def test_row_backup_demo_downloads_without_saving(client, monkeypatch, data_dir, fake_list, fake_create, events):
    from app.backups import Snapshot

    _demo(monkeypatch)
    fake_list(_listing(T1))
    create = fake_create()
    snapshot_calls: list[tuple] = []

    async def _take(account_id, tunnel_id, api_token, notes=None):
        snapshot_calls.append((account_id, tunnel_id, api_token, notes))
        return Snapshot(
            payload={"tunnel_id": T1, "account_id": account_id, "notes": ""},
            created_at="2026-11-08T00:00:00+00:00",
            tunnel_name="Home",
            route_count=1,
            filename="x.json",
        )

    monkeypatch.setattr(tunnels, "take_snapshot", _take)
    before = _backup_state(data_dir)
    response = client.post(f"/tunnels/{T1}/backup")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert 'attachment; filename="x.json"' in response.headers["content-disposition"]
    assert response.json()["tunnel_id"] == T1
    assert snapshot_calls == [(GOOD, T1, "env-token-0000-abcd", None)]
    assert create.calls == []
    assert events == []
    assert _backup_state(data_dir) == before


def test_row_backup_demo_failure_renders_inline(client, monkeypatch, fake_list, fake_create, events):
    _demo(monkeypatch)
    fake_list(_listing(T1))
    create = fake_create()

    async def _take(*args, **kwargs):
        raise HTTPException(status_code=502, detail="Cloudflare said no")

    monkeypatch.setattr(tunnels, "take_snapshot", _take)
    response = client.post(f"/tunnels/{T1}/backup")
    assert response.status_code == 200
    assert "Cloudflare said no" in _row(response.text, T1)
    assert create.calls == []
    assert events == []


# --- back up all ------------------------------------------------------------


@pytest.fixture
def selected_schedule():
    set_auto_backup_tunnel_schedule(
        DEFAULT_PROFILE_ID, {"mode": "selected", "tunnels": {T1: {"name": "Home", "frequency": "monthly"}}}
    )
    yield
    set_auto_backup_tunnel_schedule(DEFAULT_PROFILE_ID, {"mode": "all", "tunnels": {}})


def test_backup_all_ignores_schedule_filters(client, configured_default, selected_schedule, fake_list, fake_create, events):
    fake_list(_listing(T1, T2))
    create = fake_create()
    before = get_scheduled_runs_stats(DEFAULT_PROFILE_ID)
    response = client.post("/tunnels/backup-all", headers=HX)
    assert response.status_code == 200
    assert sorted(call[1] for call in create.calls) == [T1, T2]
    assert "Backed up all 2 tunnels." in response.text
    assert [event for event, _, _ in events] == ["manual_backup_success", "manual_backup_success"]
    assert get_scheduled_runs_stats(DEFAULT_PROFILE_ID) == before
    assert "cf_api_token" not in response.headers.get("set-cookie", "")
    assert "Backed up all 2 tunnels." in text_of_live(response.text)


def text_of_live(text: str) -> str:
    start = text.index('id="tunnels-live"')
    return text[start:text.index("</div>", start)]


def test_backup_all_single_tunnel_wording(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    fake_create()
    assert "Backed up 1 tunnel." in client.post("/tunnels/backup-all", headers=HX).text


def test_backup_all_partial(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1, T2))
    fake_create(errors={T2: HTTPException(status_code=502, detail="boom")})
    response = client.post("/tunnels/backup-all", headers=HX)
    assert response.status_code == 200
    assert "Backed up 1 of 2 tunnels. Failed: Lab (boom)." in response.text
    assert "boom" in _row(response.text, T2)
    assert "Backup #42 saved." in _row(response.text, T1)
    assert [event for event, _, _ in events] == ["manual_backup_success", "manual_backup_failed"]


def test_backup_all_fetches_fresh_list(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    assert client.get("/").status_code == 200
    fake_list(_listing(T1, T2))
    create = fake_create()
    client.post("/tunnels/backup-all", headers=HX)
    assert sorted(call[1] for call in create.calls) == [T1, T2]


def test_backup_all_list_failure(client, configured_default, fake_list, fake_create, events):
    fake_list(error=HTTPException(status_code=502, detail="boom"))
    create = fake_create()
    response = client.post("/tunnels/backup-all", headers=HX)
    assert response.status_code == 200
    assert "Could not list tunnels: boom" in response.text
    assert create.calls == []


def test_backup_all_zero_tunnels(client, configured_default, fake_list, fake_create, events):
    fake_list([])
    create = fake_create()
    response = client.post("/tunnels/backup-all", headers=HX)
    assert "No tunnels to back up." in response.text
    assert create.calls == []


def test_backup_all_without_credentials(client, fake_list, fake_create, events):
    create = fake_create()
    response = client.post("/tunnels/backup-all", headers=HX)
    assert response.status_code == 200
    assert "Add them in Settings" in response.text
    assert create.calls == []


def test_backup_all_refuses_concurrent_run(client, configured_default, monkeypatch, fake_list, fake_create, events):
    class _Busy:
        def locked(self) -> bool:
            return True

    monkeypatch.setattr(tunnels, "_bulk_lock", lambda profile_id: _Busy())
    fake_list(_listing(T1))
    create = fake_create()
    response = client.post("/tunnels/backup-all", headers=HX)
    assert "Back up all is already running." in response.text
    assert create.calls == []


def test_bulk_lock_is_per_profile():
    assert tunnels._bulk_lock(1) is tunnels._bulk_lock(1)
    assert tunnels._bulk_lock(1) is not tunnels._bulk_lock(2)


def test_backup_all_does_not_mutate_cached_list(client, configured_default, fake_list, fake_create, events):
    listing = _listing(T1, T2)
    snapshot = [dict(item) for item in listing]
    fake_list(listing)
    fake_create()
    client.post("/tunnels/backup-all", headers=HX)
    assert listing == snapshot


def test_backup_all_demo_redirects(client, monkeypatch, fake_create, events):
    _demo(monkeypatch)
    create = fake_create()
    response = client.post("/tunnels/backup-all", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert create.calls == []


def test_backup_all_rejects_cross_site(client, configured_default, fake_create):
    create = fake_create()
    response = client.post("/tunnels/backup-all", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert create.calls == []


# --- unexpected errors and id case ------------------------------------------


def _connect_error() -> Exception:
    import httpx

    return httpx.ConnectError("connection refused", request=httpx.Request("GET", "https://api.cloudflare.com/x"))


def test_row_backup_unexpected_error_inline(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    fake_create(errors={T1: _connect_error()})
    response = client.post(f"/tunnels/{T1}/backup", headers=HX)
    assert response.status_code == 200
    assert "Unexpected error (ConnectError)." in _row(response.text, T1)
    assert "connection refused" not in response.text
    assert TOKEN not in response.text
    assert [event for event, _, _ in events] == ["manual_backup_failed"]
    assert events[0][1]["error"] == "Unexpected error (ConnectError)."


def test_backup_all_continues_after_unexpected_error(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1, T2))
    create = fake_create(errors={T1: _connect_error()})
    response = client.post("/tunnels/backup-all", headers=HX)
    assert response.status_code == 200
    assert [call[1] for call in create.calls] == [T1, T2]
    assert "Backed up 1 of 2 tunnels. Failed: Home (Unexpected error (ConnectError))." in response.text
    assert "Backup #42 saved." in _row(response.text, T2)
    assert [event for event, _, _ in events] == ["manual_backup_failed", "manual_backup_success"]


def test_row_feedback_matches_upper_case_stored_id(client, configured_default, make_backup, fake_list, fake_create, events):
    upper = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
    make_backup(upper, "Home", account_id=GOOD)
    fake_list(error=HTTPException(status_code=502, detail="down"))  # fallback rows from local backups
    fake_create()
    response = client.post(f"/tunnels/{upper}/backup", headers=HX)
    assert response.status_code == 200
    assert "Backup #42 saved." in _row(response.text, upper)
    assert "Home: Backup #42 saved." in text_of_live(response.text)


def test_row_backup_bad_tunnel_id_without_htmx_is_400(client, configured_default, fake_list, fake_create, events):
    fake_list(_listing(T1))
    create = fake_create()
    response = client.post("/tunnels/not-a-uuid/backup")
    assert response.status_code == 400
    assert create.calls == []
