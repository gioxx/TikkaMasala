"""The profile switcher in the top bar: shown on every page once more than one profile exists."""

from __future__ import annotations

import pytest

import app.credentials as credentials
import app.encryption as encryption
import app.setup as setup
import app.web as web
from app.profiles import DEFAULT_PROFILE_ID, update_profile
from tests.conftest import use_profile
from tests.test_layout import PAGES


def test_switcher_hidden_with_one_profile(client):
    assert "data-profile-switcher" not in client.get("/").text


def test_switcher_lists_profiles_and_marks_active(client, second_profile):
    use_profile(client, second_profile.id)
    text = client.get("/").text
    assert f'<option value="{second_profile.id}" selected>Client</option>' in text
    assert f'<option value="{DEFAULT_PROFILE_ID}">Default</option>' in text
    assert 'name="profile_id"' in text
    assert 'action="/profiles/active"' in text
    assert '<span class="sr-only profile-switcher-label">Profile</span>' in text


@pytest.mark.parametrize("path,section", PAGES)
def test_switcher_on_every_page(client, second_profile, path, section):
    response = client.get(path)
    assert response.status_code == 200, path
    assert "data-profile-switcher" in response.text, path


def test_switcher_next_is_current_path(client, second_profile):
    text = client.get("/automation/runs?page=1").text
    assert 'name="next" value="/automation/runs"' in text


def test_switcher_escapes_names(client, second_profile):
    update_profile(second_profile.id, name="<b>x</b>")
    text = client.get("/").text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "<b>x</b>" not in text


def test_switcher_hidden_in_demo(client, second_profile, monkeypatch):
    monkeypatch.setattr(web, "DEMO_MODE", True)
    assert "data-profile-switcher" not in client.get("/").text


def test_switcher_outside_collapsible_menu(client, second_profile):
    text = client.get("/").text
    start = text.index('<nav id="topnav-menu"')
    menu = text[start:text.index("</nav>", start)]
    assert "data-profile-switcher" not in menu
    assert "topnav-identity" not in menu
    assert "data-profile-switcher" in text[text.index("</nav>", start):]


@pytest.mark.setup_gate
def test_setup_page_has_no_switcher(client, second_profile, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEMO_MODE", False)
    monkeypatch.setattr(credentials, "DEFAULT_ACCOUNT_ID", "")
    monkeypatch.setattr(credentials, "DEFAULT_API_TOKEN", "")
    monkeypatch.setattr(encryption, "TOKEN_ENCRYPTION_KEY", "")
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_FILE", tmp_path / "encryption.key")
    update_profile(DEFAULT_PROFILE_ID, account_id=None, api_token=None)
    client.cookies.clear()
    response = client.get("/setup")
    assert response.status_code == 200
    assert "data-profile-switcher" not in response.text


def test_switcher_on_backup_detail(client, second_profile, make_backup):
    backup_id = make_backup()
    response = client.get(f"/backup/{backup_id}")
    assert response.status_code == 200
    assert "data-profile-switcher" in response.text
    assert f'name="next" value="/backup/{backup_id}"' in response.text


def test_switch_button_always_visible_no_autosubmit(client, second_profile):
    text = client.get("/").text
    start = text.index("<form class=\"profile-switcher\"")
    form = text[start:text.index("</form>", start)]
    assert '<button type="submit" class="secondary-button profile-switcher-submit">Switch</button>' in form
    button = form[form.index("<button"):form.index("</button>")]
    assert "hidden" not in button
    js = client.get("/static/app.js").text
    assert "requestSubmit" not in js
    assert "data-profile-switcher" not in js


def test_topbar_flags_switcher_only_when_shown(client, second_profile):
    assert '<header class="topbar has-profile-switcher" data-topbar>' in client.get("/").text


def test_topbar_plain_with_one_profile(client):
    assert '<header class="topbar" data-topbar>' in client.get("/").text


def _switch_next(text: str) -> str:
    import re

    return re.search(r'name="next" value="([^"]*)"', text).group(1)


@pytest.mark.parametrize("path,data,section_root", [
    ("/automation/schedule", {"cron_expression": "0 4 * * *"}, "/automation"),
    ("/automation/retention", {"retention_days": "30"}, "/automation"),
    ("/automation/notifications/test", {}, "/automation"),
    ("/automation/run", {}, "/automation"),
])
def test_switcher_after_post_page_uses_section_root(client, second_profile, monkeypatch, path, data, section_root):
    import app.routers.automation as automation

    async def fake_run(profile_id, trigger="scheduled"):
        return {"status": "success", "tunnel_count": 0, "backup_count": 0, "error_count": 0}

    monkeypatch.setattr(automation, "run_auto_backup_job", fake_run)
    response = client.post(path, data=data)
    assert "data-profile-switcher" in response.text, path
    next_path = _switch_next(response.text)
    assert next_path == section_root
    switched = client.post(
        "/profiles/active", data={"profile_id": str(DEFAULT_PROFILE_ID), "next": next_path}, follow_redirects=True
    )
    assert switched.status_code == 200
