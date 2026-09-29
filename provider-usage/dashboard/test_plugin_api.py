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
