"""Regression tests for provider-usage's Antigravity (agy) usage card.

Run with the Hermes venv's pytest (this module needs the same interpreter that has the
dashboard's dependencies importable): `.hermes/hermes-agent/venv/bin/pytest dashboard/test_plugin_api.py`.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import plugin_api


def test_find_agy_binary_falls_back_to_local_bin_when_path_is_minimal(tmp_path, monkeypatch):
    """Reproduces the live regression: a hosting process (e.g. a systemd unit with no PATH=)
    whose PATH lacks ~/.local/bin must still find agy there, instead of reporting the
    Antigravity card as unavailable/"no data" while every other process on the box sees it."""
    fake_home = tmp_path
    fake_agy = fake_home / ".local" / "bin" / "agy"
    fake_agy.parent.mkdir(parents=True)
    fake_agy.write_text("#!/bin/sh\necho fake agy\n")
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(plugin_api.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(plugin_api.shutil, "which", lambda name: None)  # minimal PATH: not found

    found = plugin_api._find_agy_binary()
    assert found == str(fake_agy)


def test_find_agy_binary_prefers_path_when_present(monkeypatch):
    monkeypatch.setattr(plugin_api.shutil, "which", lambda name: "/usr/bin/agy")
    assert plugin_api._find_agy_binary() == "/usr/bin/agy"


def test_find_agy_binary_returns_none_when_nowhere_found(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(plugin_api.shutil, "which", lambda name: None)
    assert plugin_api._find_agy_binary() is None


def test_fetch_antigravity_usage_sync_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr(plugin_api, "_find_agy_binary", lambda: None)
    result = plugin_api._fetch_antigravity_usage_sync()
    assert result["status"] == "unavailable"
    assert "agy" in result["error"]


def test_usage_split_into_independent_per_provider_routes():
    """New UX requirement: the dashboard must not await one slow provider before showing
    others. Verifies the /usage monolith route is gone and each provider has its own route,
    so the frontend can fetch/render each card independently."""
    paths = {getattr(route, "path", None) for route in plugin_api.router.routes}
    assert "/usage" not in paths
    assert {
        "/usage/anthropic",
        "/usage/openai-codex",
        "/usage/openrouter-key",
        "/usage/openrouter-keys",
        "/usage/antigravity",
    } <= paths


def test_antigravity_route_isolated_from_a_slow_codex_fetch(monkeypatch):
    """A hung/slow Codex (or any other provider) fetch must never block the independent
    Antigravity route — each is its own coroutine reached via its own HTTP call, not a
    shared asyncio.gather() on one request."""
    import asyncio

    async def _slow_codex():
        raise AssertionError("the Antigravity route must never invoke the Codex fetch")

    monkeypatch.setattr(plugin_api, "_fetch_codex_section", _slow_codex)
    monkeypatch.setattr(
        plugin_api, "_fetch_antigravity_section",
        lambda: asyncio.sleep(0, result={"status": "ok", "windows": [], "details": [], "cache_age_seconds": 0}),
    )
    monkeypatch.setattr(plugin_api, "_config_profile_scope", lambda profile: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: False))

    result = asyncio.run(plugin_api.get_usage_antigravity(profile=None))
    assert result["antigravity"]["status"] == "ok"


def test_anthropic_route_fetches_account_and_model_scoped_together():
    """The Anthropic route intentionally bundles account windows + Fable model-scoped quota
    in one response (both need the same OAuth token and render as one card) — this must stay
    a single request, not one route per section, to avoid doubling the OAuth call."""
    import asyncio

    async def _fake_provider_section(provider):
        assert provider == "anthropic"
        return {"status": "ok", "windows": [], "details": []}

    def _fake_model_scoped():
        return {"status": "ok", "windows": [], "details": [], "model_scoped": False}

    with mock.patch.object(plugin_api, "_fetch_provider_section", _fake_provider_section), \
         mock.patch.object(plugin_api, "_fetch_anthropic_model_scoped_usage", _fake_model_scoped), \
         mock.patch.object(plugin_api, "_config_profile_scope", lambda profile: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: False)):
        result = asyncio.run(plugin_api.get_usage_anthropic(profile=None))
    assert set(result.keys()) == {"anthropic", "anthropic_model_scoped"}
    assert result["anthropic"]["status"] == "ok"
    assert result["anthropic_model_scoped"]["status"] == "ok"
