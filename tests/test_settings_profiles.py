"""Settings → Profiles: add, rename and remove profiles; credential writes reconfigure the job."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from contextlib import closing

import pytest

import app.cloudflare as cloudflare
import app.credentials as credentials
import app.routers.settings as settings_router
from app.backups import get_backup_retention_status
from app.cloudflare import CredentialsInvalid
from app.profile_admin import REMOVE_BUSY_MESSAGE
from app.profiles import DEFAULT_NOT_REMOVABLE, create_profile, get_profile, list_profiles, profile_exists
from tests.conftest import use_profile

GOOD = "0123456789abcdef0123456789abcdef"
CLIENT = "fedcba9876543210fedcba9876543210"
CLIENT_TOKEN = "client-token-0000-abcd"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def clean_state(client, monkeypatch, data_dir):
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    yield
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM profiles WHERE id != 1")
        conn.execute("UPDATE profiles SET name = 'Default' WHERE id = 1")
        conn.commit()


class _Recorder:
    def __init__(self, exc: BaseException | None = None):
        self.calls: list[tuple] = []
        self.exc = exc

    async def __call__(self, *args):
        self.calls.append(args)
        if self.exc is not None:
            raise self.exc
        return 0


@pytest.fixture
def fake_check(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(cloudflare, "check_credentials", recorder)
    return recorder


def _profile_ids() -> list[int]:
    return [p.id for p in list_profiles()]


def test_add_profile_valid(client, configured_default, fake_check):
    response = client.post(
        "/settings/profiles",
        data={"name": "Client", "account_id": CLIENT, "api_token": CLIENT_TOKEN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/settings?added=")
    new_id = int(re.match(r"/settings\?added=(\d+)#profile-\d+", location).group(1))
    assert location.endswith(f"#profile-{new_id}")
    assert get_profile(new_id).name == "Client"
    text = client.get(location.split("#")[0]).text
    assert "Profile &#39;Client&#39; added." in text or "Profile 'Client' added." in text
    assert "Switch to Client" in text
    assert '<form method="post" action="/profiles/active"' in text
    assert f'name="profile_id" value="{new_id}"' in text
    assert 'name="next" value="/automation"' in text
    assert "configured, ending in abcd" in text
    assert CLIENT_TOKEN not in text


def test_add_profile_invalid_echoes_name_and_account_not_token(client, configured_default, monkeypatch):
    monkeypatch.setattr(cloudflare, "check_credentials", _Recorder(CredentialsInvalid("Cloudflare said no.")))
    response = client.post(
        "/settings/profiles", data={"name": "Client", "account_id": CLIENT, "api_token": CLIENT_TOKEN}
    )
    assert response.status_code == 400
    text = response.text
    assert "Cloudflare said no." in text
    assert 'value="Client"' in text
    assert f'value="{CLIENT}"' in text
    assert CLIENT_TOKEN not in text
    assert 'id="add-profile" open' in text
    assert _profile_ids() == [1]


def test_add_profile_duplicate_name_and_account(client, configured_default, fake_check):
    response = client.post("/settings/profiles", data={"name": "default", "account_id": CLIENT, "api_token": "t" * 20})
    assert response.status_code == 400
    assert "already exists" in response.text
    response = client.post("/settings/profiles", data={"name": "New", "account_id": GOOD, "api_token": "t" * 20})
    assert response.status_code == 400
    assert "already used by profile &#39;Default&#39;" in response.text or "already used by profile 'Default'" in response.text
    assert _profile_ids() == [1]
    assert fake_check.calls == []


def test_add_profile_demo_disabled(client, monkeypatch, fake_check):
    monkeypatch.setattr(settings_router, "DEMO_MODE", True)
    monkeypatch.setattr(credentials, "DEMO_MODE", True)
    response = client.post(
        "/settings/profiles",
        data={"name": "Client", "account_id": CLIENT, "api_token": CLIENT_TOKEN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert _profile_ids() == [1]
    text = client.get("/settings").text
    assert "Adding profiles is disabled in demo mode." in text
    assert 'action="/settings/profiles"' not in text


def test_rename_and_remove_disabled_in_demo(client, second_profile, monkeypatch):
    monkeypatch.setattr(settings_router, "DEMO_MODE", True)
    response = client.post(f"/settings/profiles/{second_profile.id}/name", data={"name": "Acme"}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/settings")
    assert get_profile(second_profile.id).name == "Client"
    response = client.post(f"/settings/profiles/{second_profile.id}/remove", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/settings")
    response = client.get(f"/settings/profiles/{second_profile.id}/remove", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/settings")
    assert profile_exists(second_profile.id)


def test_rename_profile(client, second_profile):
    pid = second_profile.id
    response = client.post(f"/settings/profiles/{pid}/name", data={"name": "Acme"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/settings?renamed=1#profile-{pid}"
    assert get_profile(pid).name == "Acme"
    text = client.get("/settings?renamed=1").text
    assert "Profile renamed." in text
    assert "<h3>Acme</h3>" in text

    response = client.post(f"/settings/profiles/{pid}/name", data={"name": "DEFAULT"})
    assert response.status_code == 400
    assert "already exists" in response.text
    assert re.search(r'<details class="rename-profile[^"]*" open>', response.text)
    assert get_profile(pid).name == "Acme"


def test_rename_unknown_profile_404(client):
    assert client.post("/settings/profiles/9999/name", data={"name": "Ghost"}).status_code == 404


def test_credentials_labels(client, configured_default, second_profile):
    empty = create_profile("Empty")
    text = client.get("/settings").text
    blocks = {
        int(m.group(1)): m.group(2)
        for m in re.finditer(r'<section class="profile-block" id="profile-(\d+)">(.*?)</section>\s*(?=<section class="profile-block"|<details id="add-profile"|<p class="hint">Adding)', text, re.S)
    }
    assert "Replace credentials" in blocks[1]
    assert "Set credentials" in blocks[empty.id]
    assert "Replace credentials" not in blocks[empty.id]
    assert "Environment variables apply to this profile only." in blocks[1]
    assert "Environment variables apply to this profile only." not in blocks[second_profile.id]


def test_remove_link_only_for_non_default(client, second_profile):
    text = client.get("/settings").text
    assert f'href="/settings/profiles/{second_profile.id}/remove"' in text
    assert "Remove profile…" in text
    assert 'href="/settings/profiles/1/remove"' not in text
    assert f'action="/settings/profiles/{second_profile.id}/name"' in text
    assert 'maxlength="40"' in text


def test_add_profile_form_markup(client):
    text = client.get("/settings").text
    assert '<details id="add-profile">' in text
    assert "<summary>Add profile</summary>" in text
    assert '<form method="post" action="/settings/profiles"' in text
    assert 'name="api_token"' in text
    assert "Checked against Cloudflare before it is saved." in text
    assert "Verify and add" in text


def test_remove_page_shows_count(client, second_profile, make_backup):
    make_backup(T1, account_id=CLIENT)
    make_backup(T2, account_id=CLIENT)
    response = client.get(f"/settings/profiles/{second_profile.id}/remove")
    assert response.status_code == 200
    text = response.text
    assert "Remove profile Client" in text
    assert "This cannot be undone." in text
    assert 'name="delete_backups" value="2"' in text
    assert "Also delete its 2 backup(s)" in text
    assert f"<code>{CLIENT}</code>" in text
    assert '<button type="submit" class="danger">Remove profile</button>' in text
    assert '<a href="/settings">Cancel</a>' in text


def test_remove_page_without_backups_has_no_checkbox(client, second_profile):
    text = client.get(f"/settings/profiles/{second_profile.id}/remove").text
    assert 'name="delete_backups"' not in text


def test_remove_page_unknown_404(client):
    assert client.get("/settings/profiles/9999/remove").status_code == 404
    assert client.post("/settings/profiles/9999/remove").status_code == 404


def test_remove_page_names_other_retention(client, second_profile, make_backup):
    make_backup(T1, account_id=CLIENT)
    text = client.get(f"/settings/profiles/{second_profile.id}/remove").text
    assert "follow its retention:" in text
    assert get_backup_retention_status(None)["summary"] in text


def test_remove_page_shared_backups(client, second_profile, make_backup):
    twin = create_profile("Twin")
    from app.profiles import update_profile

    update_profile(twin.id, account_id=CLIENT.upper())
    make_backup(T1, account_id=CLIENT)
    text = client.get(f"/settings/profiles/{second_profile.id}/remove").text
    assert 'name="delete_backups"' not in text
    assert "another profile still uses" in text


def test_remove_keeps_backups(client, second_profile, make_backup):
    backup_id = make_backup(T1, account_id=CLIENT)
    response = client.post(f"/settings/profiles/{second_profile.id}/remove", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?removed=1&deleted=0"
    assert not profile_exists(second_profile.id)
    assert client.get(f"/backup/{backup_id}").status_code == 200
    text = client.get(response.headers["location"]).text
    assert "Profile removed. Its backups were kept under Backups → Other accounts." in text


def test_remove_with_backups(client, second_profile, make_backup, data_dir):
    ids = [make_backup(T1, account_id=CLIENT), make_backup(T2, account_id=CLIENT)]
    response = client.post(
        f"/settings/profiles/{second_profile.id}/remove", data={"delete_backups": "2"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?removed=1&deleted=2"
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM backups WHERE id IN ({ids[0]}, {ids[1]})").fetchone()[0] == 0
    text = client.get(response.headers["location"]).text
    assert "Profile removed together with 2 backup(s)." in text


@pytest.mark.parametrize("query", ["added=abc", "added=9999", "removed=1&deleted=x", "removed=1&deleted=-1"])
def test_bad_notice_params_show_nothing(client, query):
    text = client.get(f"/settings?{query}").text
    assert 'class="notice success"' not in text


def test_remove_stale_count_409(client, second_profile, make_backup):
    make_backup(T1, account_id=CLIENT)
    make_backup(T2, account_id=CLIENT)
    response = client.post(f"/settings/profiles/{second_profile.id}/remove", data={"delete_backups": "1"})
    assert response.status_code == 409
    assert "changed since this page was shown" in response.text
    assert 'value="2"' in response.text
    assert profile_exists(second_profile.id)


def test_remove_non_integer_count_400(client, second_profile):
    response = client.post(f"/settings/profiles/{second_profile.id}/remove", data={"delete_backups": "all"})
    assert response.status_code == 400
    assert profile_exists(second_profile.id)


def test_remove_busy_409(client, second_profile, monkeypatch):
    import app.profile_admin

    class _Locked:
        def locked(self) -> bool:
            return True

    monkeypatch.setattr(app.profile_admin, "profile_run_lock", lambda profile_id: _Locked())
    response = client.post(f"/settings/profiles/{second_profile.id}/remove")
    assert response.status_code == 409
    assert REMOVE_BUSY_MESSAGE in response.text
    assert profile_exists(second_profile.id)


def test_remove_route_runs_on_event_loop_and_passes_count(client, monkeypatch):
    """The removal runs in the event loop (no threadpool), so the lock check and the delete cannot interleave."""
    empty = create_profile("Empty")
    calls: list[tuple] = []

    def recorder(profile_id, *, delete_backups, expected_backup_count=None):
        asyncio.get_running_loop()  # raises RuntimeError in a worker thread
        calls.append((profile_id, delete_backups, expected_backup_count, threading.current_thread().name))
        return 0

    monkeypatch.setattr(settings_router, "remove_profile", recorder)
    response = client.post(
        f"/settings/profiles/{empty.id}/remove", data={"delete_backups": "0"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert calls and calls[0][:3] == (empty.id, True, 0)


def test_remove_default_refused(client):
    response = client.post("/settings/profiles/1/remove")
    assert response.status_code == 400
    assert DEFAULT_NOT_REMOVABLE in response.text
    response = client.get("/settings/profiles/1/remove", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert profile_exists(1)


def test_remove_active_profile_clears_cookie(client, second_profile):
    use_profile(client, second_profile.id)
    response = client.post(f"/settings/profiles/{second_profile.id}/remove", follow_redirects=False)
    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith("tm_profile=") and ("Max-Age=0" in c or "expires=" in c.lower()) for c in cookies)


def test_remove_other_profile_keeps_cookie(client, second_profile):
    other = create_profile("Other one")
    use_profile(client, second_profile.id)
    response = client.post(f"/settings/profiles/{other.id}/remove", follow_redirects=False)
    assert response.status_code == 303
    assert not any(c.startswith("tm_profile=") for c in response.headers.get_list("set-cookie"))


def test_credentials_save_reconfigures_job(client, configured_default, fake_check, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(settings_router, "configure_auto_backup_job", lambda profile_id: calls.append(profile_id))
    response = client.post(
        "/settings/profiles/1/credentials", data={"account_id": GOOD, "api_token": "new-token-0000-qqqq"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == [1]
    response = client.post("/settings/profiles/1/clear-credentials", follow_redirects=False)
    assert response.status_code == 303
    assert calls == [1, 1]


def test_remove_cross_site_rejected(client, second_profile):
    response = client.post(
        f"/settings/profiles/{second_profile.id}/remove", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    assert profile_exists(second_profile.id)
