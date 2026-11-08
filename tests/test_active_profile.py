"""Active profile per browser: the ``tm_profile`` cookie and the switch route."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from fastapi import Request

import app.profiles as profiles
import app.routers.profiles as profiles_router
from app.profiles import ACTIVE_PROFILE_COOKIE, DEFAULT_PROFILE_ID, current_profile_id, switch_destination
from tests.conftest import use_profile

CROSS_SITE = {"Sec-Fetch-Site": "cross-site"}


def _request(cookie: str | None = None) -> Request:
    headers = [] if cookie is None else [(b"cookie", f"{ACTIVE_PROFILE_COOKIE}={cookie}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def _deleted_cookie_headers(response) -> list[str]:
    return [
        value
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{ACTIVE_PROFILE_COOKIE}=") and ("Max-Age=0" in value or "expires=Thu, 01 Jan 1970" in value)
    ]


def test_no_cookie_is_default():
    request = _request()
    assert current_profile_id(request) == DEFAULT_PROFILE_ID
    assert request.state.active_profile_id == DEFAULT_PROFILE_ID
    assert not getattr(request.state, "stale_profile_cookie", False)


def test_cookie_selects_profile(second_profile):
    request = _request(str(second_profile.id))
    assert current_profile_id(request) == second_profile.id
    assert request.state.active_profile_id == second_profile.id
    assert not getattr(request.state, "stale_profile_cookie", False)


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "1e3", "01", "99999999999", ""])
def test_garbage_cookie_falls_back_and_is_deleted(client, raw):
    request = _request(raw)
    assert current_profile_id(request) == DEFAULT_PROFILE_ID
    use_profile(client, raw)
    response = client.get("/settings")
    assert response.status_code == 200
    if raw:
        assert _deleted_cookie_headers(response)


def test_cookie_for_deleted_profile_falls_back(client, second_profile, data_dir):
    use_profile(client, second_profile.id)
    with closing(sqlite3.connect(data_dir / "app.db")) as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (second_profile.id,))
        conn.commit()
    request = _request(str(second_profile.id))
    assert current_profile_id(request) == DEFAULT_PROFILE_ID
    assert request.state.stale_profile_cookie is True
    response = client.get("/")
    assert response.status_code == 200
    assert _deleted_cookie_headers(response)


def test_valid_cookie_is_kept(client, second_profile):
    use_profile(client, second_profile.id)
    response = client.get("/settings")
    assert response.status_code == 200
    assert not _deleted_cookie_headers(response)


def test_demo_ignores_cookie(second_profile, monkeypatch):
    monkeypatch.setattr(profiles, "DEMO_MODE", True)
    assert current_profile_id(_request(str(second_profile.id))) == DEFAULT_PROFILE_ID


def test_switch_sets_cookie_and_redirects(client, second_profile):
    response = client.post(
        "/profiles/active",
        data={"profile_id": str(second_profile.id), "next": "/automation/runs?page=2"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/automation/runs"
    cookie = response.headers["set-cookie"]
    assert f"tm_profile={second_profile.id}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=31536000" in cookie
    assert "Secure" not in cookie


@pytest.mark.parametrize("profile_id", ["999", "x", "", "0"])
def test_switch_unknown_profile_404(client, profile_id):
    response = client.post(
        "/profiles/active", data={"profile_id": profile_id, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 404
    assert "tm_profile=" not in response.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    "next_url,expected",
    [
        ("/backup/12", "/backups"),
        ("/backup/12/restore", "/backups"),
        ("/backups?scope=other&tunnel=x", "/backups"),
        ("/backups/new", "/backups/new"),
        ("/automation/destinations/3/edit", "/automation"),
        ("/automation/runs?page=2", "/automation/runs"),
        ("/settings/profiles/2/remove", "/settings"),
        ("/setup", "/"),
        ("//evil.example/x", "/"),
        ("/\\evil", "/"),
        ("https://evil.example/", "/"),
        ("", "/"),
        ("/ingress?source=live", "/ingress"),
    ],
)
def test_switch_destination_rules(next_url, expected):
    assert switch_destination(next_url) == expected


def test_switch_cross_site_rejected(client, second_profile):
    response = client.post(
        "/profiles/active",
        data={"profile_id": str(second_profile.id), "next": "/"},
        headers=CROSS_SITE,
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "tm_profile=" not in response.headers.get("set-cookie", "")


def test_switch_demo_redirects_home(client, second_profile, monkeypatch):
    monkeypatch.setattr(profiles_router, "DEMO_MODE", True)
    response = client.post(
        "/profiles/active",
        data={"profile_id": str(second_profile.id), "next": "/backups"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "tm_profile=" not in response.headers.get("set-cookie", "")


def test_switch_with_stale_cookie_keeps_new_cookie(client, second_profile):
    use_profile(client, "abc")
    response = client.post(
        "/profiles/active",
        data={"profile_id": str(second_profile.id), "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookies = [value for value in response.headers.get_list("set-cookie") if value.startswith("tm_profile=")]
    assert cookies == [cookies[0]]
    assert cookies[0].startswith(f"tm_profile={second_profile.id};")
