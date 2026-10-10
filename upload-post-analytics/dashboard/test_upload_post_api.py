"""Tests for the upload-post-analytics panel backend.

Run with the Hermes venv: `~/.hermes/hermes-agent/venv/bin/pytest upload-post-analytics/dashboard/test_upload_post_api.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
  first_seen INTEGER, last_attempt_at INTEGER, last_ok_at INTEGER, fail_streak INTEGER DEFAULT 0, last_error TEXT,
  media_type TEXT, content_id TEXT, content_title TEXT);
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
    con.executemany("INSERT INTO posts (id, profile, platform, platform_post_id, request_id, title, published_at,"
                    " content_id, content_title, media_type) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        (1, "brand", "youtube", "yt1", "req-1", "Same video", now - 7200, "HS-001", "Same video", "VIDEO"),
        (2, "brand", "instagram", "ig1", "req-1", "", now - 7200, "HS-001", "Same video", "REEL"),
        (3, "brand", "instagram", "ig2", None, "", now - 3600, None, None, "REEL"),
        (4, "brand", "youtube", "yt2", None, "", now - 3300, None, None, "VIDEO"),
        (5, "brand", "youtube", "yt3", None, "Unrelated", now - 86400, None, None, "VIDEO"),
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
    for result in (plugin_api.get_options(), plugin_api.get_series(profile="x"), plugin_api.get_health(),
                   plugin_api.get_scheduled()):
        assert result["status"] == "not_configured"


JOB_SCHEMA = """
CREATE TABLE scheduled_jobs (
  job_id TEXT PRIMARY KEY, profile TEXT, scheduled_at INTEGER, post_type TEXT, title TEXT, external_id TEXT,
  source_filename TEXT, platforms TEXT, platform_content TEXT, has_cover INTEGER, cover_preview_url TEXT,
  thumbnail_url TEXT, original_timezone TEXT, first_seen INTEGER, last_seen INTEGER, gone_at INTEGER,
  status TEXT, status_final INTEGER DEFAULT 0, status_checked_at INTEGER, status_message TEXT);
CREATE TABLE scheduled_results (
  job_id TEXT, platform TEXT, status TEXT, post_url TEXT, error TEXT, upload_ts INTEGER, updated_at INTEGER,
  PRIMARY KEY (job_id, platform));
"""


@pytest.fixture
def jobs_db(db):
    con = sqlite3.connect(db)
    con.executescript(JOB_SCHEMA)
    now = int(time.time())
    con.execute("INSERT INTO accounts VALUES ('brand', 'tiktok', 0, 0, NULL, NULL, NULL, 0)")
    jobs = [
        ("later", now + 7200, "HS-2", ["youtube"], None, None),
        ("soon", now + 600, None, ["instagram", "youtube", "threads"], None, None),
        ("done", now - 86400, "HS-1", ["youtube", "instagram"], now - 86000, "completed"),
        ("old", now - 9 * 86400, "HS-0", ["youtube"], now - 9 * 86400, "completed"),
    ]
    for job_id, at, ext, platforms, gone, status in jobs:
        con.execute("INSERT INTO scheduled_jobs (job_id, profile, scheduled_at, title, external_id, platforms,"
                    " platform_content, thumbnail_url, gone_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, "brand", at, f"title {job_id}", ext, json.dumps(platforms),
                     json.dumps({"youtube": {"title": "yt", "caption": "desc"}}), "https://t/1.jpg" if ext else None,
                     gone, status))
    con.executemany("INSERT INTO scheduled_results VALUES (?,?,?,?,?,?,?)", [
        ("done", "youtube", "completed", "https://youtube.com/shorts/a", None, now - 86400, now),
        ("done", "instagram", "failed", None, "Video too long", None, now),
    ])
    con.commit()
    con.close()
    return db


def test_scheduled_splits_upcoming_and_recent(jobs_db):
    res = plugin_api.get_scheduled()
    assert res["status"] == "ok" and res["collecting"]
    assert [j["job_id"] for j in res["upcoming"]] == ["soon", "later"]
    assert [j["job_id"] for j in res["recent"]] == ["done"]  # "old" is past the 7-day window
    assert res["links"]["upcoming"].startswith("https://app.upload-post.com/")


def test_scheduled_platform_states(jobs_db):
    soon = plugin_api.get_scheduled()["upcoming"][0]
    states = [(p["platform"], p["state"]) for p in soon["platforms"]]
    # selected in job order; threads selected but not connected; linkedin connected but not selected; tiktok manual
    assert states == [("instagram", "selected"), ("youtube", "selected"), ("threads", "selected_unconnected"),
                      ("linkedin", "not_selected"), ("tiktok", "manual")]


def test_scheduled_recent_carries_results(jobs_db):
    done = plugin_api.get_scheduled()["recent"][0]
    by = {p["platform"]: p for p in done["platforms"]}
    assert by["youtube"]["result"]["post_url"] == "https://youtube.com/shorts/a"
    assert by["instagram"]["result"] == {"status": "failed", "post_url": None, "error": "Video too long",
                                         "upload_ts": None}
    assert done["platform_content"]["youtube"]["caption"] == "desc"
    assert done["cover_url"] == "https://t/1.jpg"


def test_scheduled_before_collector_upgrade(db):
    res = plugin_api.get_scheduled()
    assert res["status"] == "ok" and res["collecting"] is False and res["upcoming"] == []


def test_db_is_opened_read_only(db):
    con = plugin_api._connect()
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM runs")
    con.close()


def test_options_groups_by_content_id_then_request_id_then_lone(db):
    res = plugin_api.get_options()
    brand = res["profiles"][0]
    keys = {p["key"]: p for p in brand["posts"]}
    assert set(keys) == {"c:HS-001", "p:3", "p:4", "p:5"}
    assert sorted(x["platform"] for x in keys["c:HS-001"]["platforms"]) == ["instagram", "youtube"]
    assert keys["c:HS-001"]["title"] == "HS-001 \u2014 Same video"
    assert keys["c:HS-001"]["joined_by"] == "content_id"
    assert keys["p:3"]["joined_by"] is None
    # untitled lone post gets a distinguishable label, not a blank title
    assert "Reel" in keys["p:3"]["title"] and "instagram" in keys["p:3"]["title"]
    assert [p["key"] for p in brand["posts"]][-1] == "p:5"  # newest first
    assert any(a["unsupported_note"] for a in brand["accounts"] if a["platform"] == "linkedin")


def test_content_id_group_returns_one_line_per_platform(db):
    lines = plugin_api.get_series(profile="brand", target="c:HS-001", metric="primary")["lines"]
    assert sorted(l["platform"] for l in lines) == ["instagram", "youtube"]


def test_post_primary_series_keeps_each_platforms_metric_type(db):
    res = plugin_api.get_series(profile="brand", target="c:HS-001", metric="primary")
    by_platform = {l["platform"]: l for l in res["lines"]}
    assert by_platform["youtube"]["metric_type"] == "views"
    assert [p[1] for p in by_platform["youtube"]["points"]] == [100, 160]
    assert by_platform["instagram"]["metric_type"] == "reach"
    assert [p[1] for p in by_platform["instagram"]["points"]] == [30, 55]
    assert all(l["kind"] == "total" for l in res["lines"])


def test_post_metric_skips_null_snapshots(db):
    res = plugin_api.get_series(profile="brand", target="c:HS-001", metric="likes")
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


JLM = ZoneInfo("Asia/Jerusalem")


def _epoch(y, mo, d, h=0, mi=0, tz=JLM) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())


@pytest.fixture
def range_db(db):
    """Hourly snapshots 2026-10-15..2026-11-05 (crosses the 2026-10-25 IDT->IST change) + daily dates."""
    con = sqlite3.connect(db)
    start, end = _epoch(2026, 10, 15), _epoch(2026, 11, 5)
    con.executemany("INSERT INTO post_snapshots (post_id, ts, metric_type, views) VALUES (5, ?, 'views', ?)",
                    [(ts, i) for i, ts in enumerate(range(start, end, 3600))])
    con.executemany("INSERT INTO channel_snapshots (profile, platform, ts, metric_type, followers)"
                    " VALUES ('brand', 'instagram', ?, 'reach', ?)",
                    [(ts, i) for i, ts in enumerate(range(start, end, 3600))])
    con.executemany("INSERT INTO channel_daily VALUES ('brand', 'instagram', 'reach', ?, 'reach', ?, 0)",
                    [(f"2026-10-{d:02d}", d) for d in range(3, 32)] + [(f"2026-11-{d:02d}", d) for d in range(1, 6)])
    con.commit()
    con.close()
    return db


def _points(res, platform="instagram"):
    return next(l for l in res["lines"] if l["platform"] == platform)["points"]


def test_range_is_from_inclusive_to_exclusive(range_db):
    t0 = _epoch(2026, 10, 16, 12)
    res = plugin_api.get_series(profile="brand", target="p:5", metric="primary", from_ts=t0, to_ts=t0 + 3 * 3600)
    assert [p[0] // 1000 for p in _points(res, "youtube")] == [t0, t0 + 3600, t0 + 7200]
    res = plugin_api.get_series(profile="brand", target="p:5", metric="primary", from_ts=t0 + 1, to_ts=t0 + 3601)
    assert [p[0] // 1000 for p in _points(res, "youtube")] == [t0 + 3600]
    res = plugin_api.get_series(profile="brand", metric="followers", from_ts=t0, to_ts=t0 + 3600)
    assert [p[0] // 1000 for p in _points(res)] == [t0]


def test_dst_crossing_range_counts_absolute_hours(range_db):
    # 2026-10-20 00:00 IDT -> 2026-10-30 00:00 IST is 10 calendar days but 241 real hours (clocks fall back 25 Oct).
    t0, t1 = _epoch(2026, 10, 20), _epoch(2026, 10, 30)
    assert t1 - t0 == 241 * 3600
    snaps = _points(plugin_api.get_series(profile="brand", target="p:5", metric="primary",
                                          from_ts=t0, to_ts=t1, tz="Asia/Jerusalem"), "youtube")
    assert len(snaps) == 241
    assert snaps[0][0] // 1000 == t0 and snaps[-1][0] // 1000 == t1 - 3600
    assert len(_points(plugin_api.get_series(profile="brand", metric="followers", from_ts=t0, to_ts=t1))) == 241
    daily = _points(plugin_api.get_series(profile="brand", metric="primary", from_ts=t0, to_ts=t1, tz="Asia/Jerusalem"))
    assert [p[0] for p in daily] == [f"2026-10-{d}" for d in range(20, 30)]


def test_daily_dates_are_never_shifted(range_db):
    # Local midnight in UTC+3 is still the previous day in UTC; the date bound must follow the caller's zone.
    t0 = _epoch(2026, 10, 5)
    assert datetime.fromtimestamp(t0, timezone.utc).date().isoformat() == "2026-10-04"
    res = plugin_api.get_series(profile="brand", metric="primary", from_ts=t0, to_ts=t0 + 86400, tz="Asia/Jerusalem")
    assert _points(res) == [["2026-10-05", 5]]
    # UTC-3, late evening: already the next day in UTC, still 2026-10-05 locally.
    sp = ZoneInfo("America/Sao_Paulo")
    t = _epoch(2026, 10, 5, 23, 30, tz=sp)
    assert datetime.fromtimestamp(t, timezone.utc).date().isoformat() == "2026-10-06"
    res = plugin_api.get_series(profile="brand", metric="primary", from_ts=t, to_ts=t + 60, tz="America/Sao_Paulo")
    assert _points(res) == [["2026-10-05", 5]]
    # Dates come back as the stored strings, untouched by tz.
    for tz in ("UTC", "Asia/Jerusalem", "America/Sao_Paulo", "Pacific/Kiritimati"):
        pts = _points(plugin_api.get_series(profile="brand", metric="primary", tz=tz))
        assert pts[0] == ["2026-10-01", 7] and pts[-1] == ["2026-11-05", 5]


def test_range_input_validation(range_db):
    assert plugin_api.get_series(profile="brand", from_ts=100, to_ts=100)["status"] == "error"
    assert plugin_api.get_series(profile="brand", tz="Nope/Zone")["status"] == "error"
    assert plugin_api.get_series(profile="brand", tz="../../etc/passwd")["status"] == "error"
    assert plugin_api.get_series(profile="brand", from_ts=-5, to_ts=10**15)["status"] == "ok"
