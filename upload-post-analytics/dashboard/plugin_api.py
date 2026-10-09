"""Upload-Post analytics dashboard panel — read-only routes over the collector's SQLite.

Mounted at /api/plugins/upload-post-analytics/. The collector
(~/dev/services/_scripts/upload-post-analytics) is the only writer; this module opens the DB
with ``mode=ro`` and never calls the Upload-Post API.

Series kinds, so the UI can toggle cumulative vs per-interval honestly:
  - ``daily``  per-day values (channel_daily): cumulative = running sum, delta = as-is.
  - ``total``  lifetime counters (post snapshots, channel followers): delta = consecutive diffs.
  - ``window`` rolling 30-day aggregates (channel snapshot likes/comments/shares): shown as-is
               in both modes, because neither a running sum nor a diff of a rolling window means anything.
Values with different ``metric_type`` (reach / views / impressions) are never summed together.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter

router = APIRouter()

DB_PATH = Path("~/dev/services/_scripts/upload-post-analytics/data/upload_post_analytics.db").expanduser()

METRICS = ("primary", "likes", "comments", "shares", "followers")
PRIMARY_COLS = ("views", "reach", "impressions")
HEALTH_WINDOW_SECONDS = 24 * 3600
CROSSPOST_WINDOW_SECONDS = 15 * 60


def _connect() -> Optional[sqlite3.Connection]:
    if not DB_PATH.exists():
        return None
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _not_ready() -> dict[str, Any]:
    return {"status": "not_configured", "error": f"collector DB not found at {DB_PATH}"}


def _group_posts(rows) -> list[list]:
    """One group per logical post: shared request_id, else manual posts on distinct platforms
    published within CROSSPOST_WINDOW_SECONDS of each other (Upload-Post gives them no link)."""
    by_request: dict[tuple[str, str], list] = {}
    manual: list[list] = []
    for r in sorted(rows, key=lambda r: r["published_at"] or 0):
        if r["request_id"]:
            by_request.setdefault((r["profile"], r["request_id"]), []).append(r)
            continue
        t = r["published_at"]
        match = next((g for g in manual if t is not None and g[0]["published_at"] is not None
                      and g[0]["profile"] == r["profile"]
                      and t - g[0]["published_at"] <= CROSSPOST_WINDOW_SECONDS
                      and r["platform"] not in {x["platform"] for x in g}), None)
        if match is not None:
            match.append(r)
        else:
            manual.append([r])
    groups = list(by_request.values()) + manual
    groups.sort(key=lambda g: max(x["published_at"] or 0 for x in g), reverse=True)
    return groups


def _group_key(group) -> str:
    if group[0]["request_id"]:
        return f"r:{group[0]['request_id']}"
    return "p:" + ",".join(str(x["id"]) for x in sorted(group, key=lambda x: x["id"]))


@router.get("/options")
def get_options() -> dict[str, Any]:
    db = _connect()
    if db is None:
        return _not_ready()
    with closing(db):
        accounts = db.execute(
            "SELECT profile, platform, connected, reauth_required, handle, unsupported_note"
            " FROM accounts ORDER BY profile, platform").fetchall()
        posts = db.execute(
            "SELECT p.id, p.profile, p.platform, p.request_id, p.url, p.title,"
            " COALESCE(p.published_at, p.first_seen) AS published_at,"
            " (SELECT COUNT(*) FROM post_snapshots s WHERE s.post_id = p.id) AS snapshots"
            " FROM posts p").fetchall()
    profiles: dict[str, dict[str, Any]] = {}
    for a in accounts:
        prof = profiles.setdefault(a["profile"], {"profile": a["profile"], "accounts": [], "posts": []})
        prof["accounts"].append({
            "platform": a["platform"], "connected": bool(a["connected"]),
            "reauth_required": bool(a["reauth_required"]), "handle": a["handle"],
            "unsupported_note": a["unsupported_note"],
        })
    for group in _group_posts(posts):
        prof = profiles.setdefault(group[0]["profile"], {"profile": group[0]["profile"], "accounts": [], "posts": []})
        prof["posts"].append({
            "key": _group_key(group),
            "title": next((x["title"] for x in group if x["title"]), ""),
            "published_at": min(x["published_at"] or 0 for x in group) or None,
            "platforms": [{"platform": x["platform"], "url": x["url"]} for x in group],
            "snapshots": sum(x["snapshots"] for x in group),
            "matched_by_time": len(group) > 1 and not group[0]["request_id"],
        })
    return {"status": "ok", "profiles": sorted(profiles.values(), key=lambda x: x["profile"]),
            "metrics": list(METRICS)}


def _date_ms(date: str) -> int:
    return int(time.mktime(time.strptime(date, "%Y-%m-%d"))) * 1000


def _primary_value(row) -> tuple[Optional[float], Optional[str]]:
    mtype = row["metric_type"]
    if mtype in PRIMARY_COLS and row[mtype] is not None:
        return row[mtype], mtype
    for col in PRIMARY_COLS:
        if row[col] is not None:
            return row[col], col
    return None, mtype


def _channel_series(db, profile: str, metric: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    if metric == "primary":
        rows = db.execute(
            "SELECT platform, series, metric_type, date, value FROM channel_daily"
            " WHERE profile=? AND series=metric_type ORDER BY platform, date", (profile,)).fetchall()
        by_platform: dict[str, dict[str, Any]] = {}
        for r in rows:
            line = by_platform.setdefault(r["platform"], {
                "platform": r["platform"], "metric_type": r["metric_type"], "kind": "daily", "points": []})
            line["points"].append([_date_ms(r["date"]), r["value"]])
        return list(by_platform.values())
    col = metric
    kind = "total" if metric == "followers" else "window"
    rows = db.execute(
        f"SELECT platform, ts, {col} AS v FROM channel_snapshots"
        f" WHERE profile=? AND {col} IS NOT NULL ORDER BY platform, ts", (profile,)).fetchall()
    by_platform = {}
    for r in rows:
        line = by_platform.setdefault(r["platform"], {
            "platform": r["platform"], "metric_type": metric, "kind": kind, "points": []})
        line["points"].append([r["ts"] * 1000, r["v"]])
    lines.extend(by_platform.values())
    return lines


def _post_series(db, profile: str, key: str, metric: str) -> list[dict[str, Any]]:
    kind, _, ident = key.partition(":")
    if kind == "r":
        posts = db.execute("SELECT id, platform FROM posts WHERE profile=? AND request_id=?", (profile, ident)).fetchall()
    elif kind == "p" and ident and all(x.isdigit() for x in ident.split(",")):
        ids = [int(x) for x in ident.split(",")]
        posts = db.execute(f"SELECT id, platform FROM posts WHERE profile=? AND id IN ({','.join('?' * len(ids))})"
                           " ORDER BY platform", (profile, *ids)).fetchall()
    else:
        return []
    col = "followers_gained" if metric == "followers" else metric
    lines = []
    for p in posts:
        snaps = db.execute("SELECT * FROM post_snapshots WHERE post_id=? ORDER BY ts", (p["id"],)).fetchall()
        points, mtype = [], None
        for s in snaps:
            if metric == "primary":
                value, mtype = _primary_value(s)
            else:
                value, mtype = s[col], metric
            if value is not None:
                points.append([s["ts"] * 1000, value])
        lines.append({"platform": p["platform"], "metric_type": mtype or metric, "kind": "total", "points": points})
    return lines


@router.get("/series")
def get_series(profile: str, target: str = "channel", metric: str = "primary") -> dict[str, Any]:
    if metric not in METRICS:
        return {"status": "error", "error": f"unknown metric {metric!r}; one of {', '.join(METRICS)}"}
    db = _connect()
    if db is None:
        return _not_ready()
    with closing(db):
        if target == "channel":
            lines = _channel_series(db, profile, metric)
        else:
            lines = _post_series(db, profile, target, metric)
    return {"status": "ok", "profile": profile, "target": target, "metric": metric, "lines": lines}


@router.get("/health")
def get_health(profile: Optional[str] = None) -> dict[str, Any]:
    db = _connect()
    if db is None:
        return _not_ready()
    since = int(time.time()) - HEALTH_WINDOW_SECONDS
    with closing(db):
        runs = db.execute(
            "SELECT COUNT(*) AS n, SUM(status='ok') AS ok, SUM(COALESCE(status,'')!='ok') AS bad,"
            " SUM(requests) AS requests FROM runs WHERE started_at >= ?", (since,)).fetchone()
        last_run = db.execute(
            "SELECT started_at, finished_at, status, note FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        rl = db.execute(
            "SELECT MAX(rl_limit) AS rl_limit, MIN(min_remaining) AS min_remaining,"
            " SUM(count_429) AS count_429 FROM rate_limits WHERE ts >= ?", (since,)).fetchone()
        err_filter, err_args = ("AND (profile=? OR profile IS NULL)", (since, profile)) if profile else ("", (since,))
        error_count = db.execute(f"SELECT COUNT(*) FROM errors WHERE ts >= ? {err_filter}", err_args).fetchone()[0]
        errors = db.execute(
            f"SELECT ts, endpoint, profile, platform, status, error_code, message FROM errors"
            f" WHERE ts >= ? {err_filter} ORDER BY ts DESC LIMIT 10", err_args).fetchall()
    return {
        "status": "ok",
        "window_hours": HEALTH_WINDOW_SECONDS // 3600,
        "runs": {"count": runs["n"] or 0, "ok": runs["ok"] or 0, "failed": runs["bad"] or 0,
                 "requests": runs["requests"] or 0},
        "last_run": dict(last_run) if last_run else None,
        "rate_limit": {"limit": rl["rl_limit"], "min_remaining": rl["min_remaining"],
                       "count_429": rl["count_429"] or 0},
        "error_count": error_count,
        "errors": [dict(e) for e in errors],
    }
