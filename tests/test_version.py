"""Display form of APP_VERSION: release tags as is, dev builds with their base release and commit."""

from __future__ import annotations

import pytest

from app.version import badge_version, display_version


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.3.10-42-g8cce54d", "dev · 1.3.10+42 · 8cce54d"),
        ("1.3-7-gabcdef0", "dev · 1.3+7 · abcdef0"),
        ("1.3.10-0-g8cce54d", "dev · 1.3.10 · 8cce54d"),  # main exactly on a release tag
        ("1.3.10-42-g8cce54d0a1b", "dev · 1.3.10+42 · 8cce54d0a1b"),
        ("dev-8cce54d", "dev · 8cce54d"),  # no release tag reachable
        ("dev", "dev"),
        ("", "dev"),
        ("  ", "dev"),
        ("1.3.11", "1.3.11"),
        ("1.4", "1.4"),
        ("something-else", "something-else"),
    ],
)
def test_display_version(raw, expected):
    assert display_version(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.3.11", "v1.3.11"),
        ("1.4", "v1.4"),
        ("dev", "dev"),
        ("1.3.10-42-g8cce54d", "dev · 1.3.10+42 · 8cce54d"),
        ("something-else", "something-else"),
    ],
)
def test_badge_version(raw, expected):
    assert badge_version(raw) == expected


def test_templates_and_notifications_use_display_version():
    import app.config as config
    import app.notifications as notifications
    from app.web import templates

    assert config.APP_VERSION == display_version(config.APP_VERSION_RAW)
    assert notifications.APP_VERSION == config.APP_VERSION
    assert templates.env.globals["app_version"] == config.APP_VERSION
    assert templates.env.globals["app_version_badge"] == badge_version(config.APP_VERSION_RAW)


def test_pages_show_display_version(client, monkeypatch):
    from app.web import templates

    monkeypatch.setitem(templates.env.globals, "app_version", "dev · 1.3.10+42 · 8cce54d")
    monkeypatch.setitem(templates.env.globals, "app_version_badge", "dev · 1.3.10+42 · 8cce54d")
    text = client.get("/settings").text
    assert '<sub>dev · 1.3.10+42 · 8cce54d</sub>' in text
    assert "Version dev · 1.3.10+42 · 8cce54d" in text  # footer
    assert '<dd class="meta-value">dev · 1.3.10+42 · 8cce54d</dd>' in text  # Settings > About
    assert "vdev" not in text


def test_release_badge_keeps_v_prefix(client, monkeypatch):
    from app.web import templates

    monkeypatch.setitem(templates.env.globals, "app_version", "1.3.11")
    monkeypatch.setitem(templates.env.globals, "app_version_badge", "v1.3.11")
    text = client.get("/settings").text
    assert "<sub>v1.3.11</sub>" in text
    assert "Version 1.3.11" in text


def test_workflow_passes_described_version_on_main():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "Docker-BuildPublish.yml"
    ).read_text(encoding="utf-8")
    assert "fetch-depth: 0" in workflow
    assert "git describe --tags --long --match '[0-9]*'" in workflow
    assert "APP_VERSION=${{ steps.version.outputs.app_version }}" in workflow
