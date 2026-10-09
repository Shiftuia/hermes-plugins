"""Tests for the upload-post-analytics panel backend.

Run with the Hermes venv: `~/.hermes/hermes-agent/venv/bin/pytest upload-post-analytics/dashboard/test_upload_post_api.py`.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

# Loaded under a unique name: every plugin's backend is called plugin_api.py.
_spec = importlib.util.spec_from_file_location("upload_post_plugin_api", Path(__file__).parent / "plugin_api.py")
plugin_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin_api)

SCHEMA = """
CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, finished_at INTEGER, status TEXT,
  requests INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, snapshots INTEGER DEFAULT 0, note TEXT);
CREATE TABLE accounts (profile TEXT, platform TEXT, connected INTEGER, reauth_required INTEGER, handle TEXT,
  page_id TEXT, unsupported_note TEXT, updated_at INTEGER, PRIMARY KEY (profile, platform));
CREATE TABLE posts (id INTEGER PRIMARY KEY, profile TEXT NOT NULL, platform TEXT NOT NULL,
  platform_post_id TEXT NOT NULL, request_id TEXT, url TEXT, title TEXT, published_at INTEGER, source TEXT,
  first_seen INTEGER, last_attempt_at INTEGER, last_ok_at INTEGER, fail_streak INTEGER DEFAULT 0, last_error TEXT);
CREATE TABLE post_snapshots (id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, ts INTEGER NOT NULL,
  metric_type TEXT, views REAL, reach REAL, impressions REAL, likes REAL, comments REAL,
  shares REAL, saves REAL, followers_gained REAL, extra TEXT);
CREATE TABLE channel_snapshots (id INTEGER PRIMARY KEY, profile TEXT NOT NULL, platform TEXT NOT NULL,
  ts INTEGER NOT NULL, metric_type TEXT, followers REAL, reach REAL, views REAL, impressions REAL, likes REAL,
  comments REAL, shares REAL, saves REAL, profile_views REAL, extra TEXT);
CREATE TABLE channel_daily (profile TEXT, platform TEXT, series TEXT, date TEXT, metric_type TEXT, value REAL,
  updated_at INTEGER, PRIMARY KEY (profile, platform, series, date));
CREATE TABLE errors (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, run_id INTEGER, endpoint TEXT, profile TEXT,
  platform TEXT, post_id INTEGER, status INTEGER, error_code TEXT, message TEXT);
CREATE TABLE rate_limits (run_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, requests INTEGER, rl_limit INTEGER,
  min_remaining INTEGER, last_remaining INTEGER, reset_at INTEGER, count_429 INTEGER);
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "upa.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    now = int(time.time())
    con.executemany("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)", [
        ("brand", "youtube", 1, 0, "@b", None, None, now),
        ("brand", "instagram", 1, 0, "b", None, None, now),
        ("brand", "linkedin", 1, 0, "B", None, "LinkedIn only provides analytics for company pages", now),
    ])
    con.executemany("INSERT INTO posts (id, profile, platform, platform_post_id, request_id, title, published_at)"
                    " VALUES (?,?,?,?,?,?,?)", [
        (1, "brand", "youtube", "yt1", "req-1", "Same video", now - 7200),
        (2, "brand", "instagram", "ig1", "req-1", "", now - 7200),
        (3, "brand", "instagram", "ig2", None, "Manual reel", now - 3600),
        (4, "brand", "youtube", "yt2", None, "Manual short", now - 3300),
        (5, "brand", "youtube", "yt3", None, "Unrelated", now - 86400),
    ])
    con.executemany("INSERT INTO post_snapshots (post_id, ts, metric_type, views, reach, likes) VALUES (?,?,?,?,?,?)", [
        (1, now - 3600, "views", 100, None, 5),
        (1, now - 1800, "views", 160, None, 9),
        (2, now - 3600, "reach", 40, 30, 2),
        (2, now - 1800, "reach", 70, 55, None),
    ])
    con.executemany("INSERT INTO channel_daily VALUES (?,?,?,?,?,?,?)", [
        ("brand", "youtube", "views", "2026-10-01", "views", 10, now),
        ("brand", "youtube", "views", "2026-10-02", "15", 15, now),
        ("brand", "youtube", "subscribers_gained", "2026-10-01", "views", 2, now),
        ("brand", "instagram", "reach", "2026-10-01", "reach", 7, now),
    ])
    con.execute("INSERT INTO channel_snapshots (profile, platform, ts, metric_type, followers, likes)"
                " VALUES ('brand','youtube',?, 'views', 13, 20)", (now,))
    con.execute("INSERT INTO runs (started_at, finished_at, status, requests) VALUES (?,?, 'ok', 30)", (now - 60, now - 30))
    con.execute("INSERT INTO rate_limits VALUES (1, ?, 30, 64, 41, 58, ?, 0)", (now - 30, now + 60))
    con.execute("INSERT INTO errors (ts, endpoint, profile, platform, status, error_code, message)"
                " VALUES (?, '/x', 'brand', 'youtube', 500, 'boom', 'server error')", (now - 100,))
    con.execute("INSERT INTO errors (ts, endpoint, profile, status, message) VALUES (?, '/old', 'brand', 500, 'old')",
                (now - 3 * 86400,))
    con.commit()
    con.close()
    monkeypatch.setattr(plugin_api, "DB_PATH", path)
    return path


def test_missing_db_reports_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "DB_PATH", tmp_path / "nope.db")
    for result in (plugin_api.get_options(), plugin_api.get_series(profile="x"), plugin_api.get_health()):
        assert result["status"] == "not_configured"


def test_db_is_opened_read_only(db):
    con = plugin_api._connect()
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM runs")
    con.close()


def test_options_group_cross_posts_by_request_id(db):
    res = plugin_api.get_options()
    brand = res["profiles"][0]
    keys = {p["key"]: p for p in brand["posts"]}
    assert set(keys) == {"r:req-1", "p:3,4", "p:5"}
    assert sorted(x["platform"] for x in keys["r:req-1"]["platforms"]) == ["instagram", "youtube"]
    assert keys["r:req-1"]["title"] == "Same video"
    assert keys["p:3,4"]["matched_by_time"] and not keys["r:req-1"]["matched_by_time"]
    assert [p["key"] for p in brand["posts"]][-1] == "p:5"  # newest first
    assert any(a["unsupported_note"] for a in brand["accounts"] if a["platform"] == "linkedin")


def test_time_matched_group_returns_one_line_per_platform(db):
    lines = plugin_api.get_series(profile="brand", target="p:3,4", metric="primary")["lines"]
    assert [l["platform"] for l in lines] == ["instagram", "youtube"]


def test_post_primary_series_keeps_each_platforms_metric_type(db):
    res = plugin_api.get_series(profile="brand", target="r:req-1", metric="primary")
    by_platform = {l["platform"]: l for l in res["lines"]}
    assert by_platform["youtube"]["metric_type"] == "views"
    assert [p[1] for p in by_platform["youtube"]["points"]] == [100, 160]
    assert by_platform["instagram"]["metric_type"] == "reach"
    assert [p[1] for p in by_platform["instagram"]["points"]] == [30, 55]
    assert all(l["kind"] == "total" for l in res["lines"])


def test_post_metric_skips_null_snapshots(db):
    res = plugin_api.get_series(profile="brand", target="r:req-1", metric="likes")
    ig = next(l for l in res["lines"] if l["platform"] == "instagram")
    assert [p[1] for p in ig["points"]] == [2]


def test_channel_primary_uses_only_the_platform_metric_series(db):
    res = plugin_api.get_series(profile="brand", target="channel", metric="primary")
    by_platform = {l["platform"]: l for l in res["lines"]}
    assert set(by_platform) == {"youtube", "instagram"}
    assert by_platform["youtube"]["kind"] == "daily"
    assert len(by_platform["youtube"]["points"]) == 1  # subscribers_gained and mislabeled rows excluded


def test_channel_followers_and_window_metrics(db):
    followers = plugin_api.get_series(profile="brand", target="channel", metric="followers")["lines"]
    assert followers[0]["kind"] == "total" and followers[0]["points"][0][1] == 13
    likes = plugin_api.get_series(profile="brand", target="channel", metric="likes")["lines"]
    assert likes[0]["kind"] == "window"


def test_bad_inputs(db):
    assert plugin_api.get_series(profile="brand", metric="saves; DROP TABLE runs")["status"] == "error"
    assert plugin_api.get_series(profile="brand", target="p:abc")["lines"] == []
    assert plugin_api.get_series(profile="brand", target="p:1) OR 1=1 --")["lines"] == []
    assert plugin_api.get_series(profile="brand", target="p:3", metric="primary")["lines"][0]["points"] == []


def test_health_counts_last_24h_only(db):
    res = plugin_api.get_health(profile="brand")
    assert res["runs"] == {"count": 1, "ok": 1, "failed": 0, "requests": 30}
    assert res["rate_limit"] == {"limit": 64, "min_remaining": 41, "count_429": 0}
    assert res["error_count"] == 1
    assert res["errors"][0]["error_code"] == "boom"
