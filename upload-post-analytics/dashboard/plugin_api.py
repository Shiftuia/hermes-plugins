"""Upload-Post analytics dashboard panel — read-only routes over the collector's SQLite.

Mounted at /api/plugins/upload-post-analytics/. The collector
(~/dev/services/_scripts/upload-post-analytics) is the only writer; this module opens the DB
with ``mode=ro`` and never calls the Upload-Post API.

Series kinds, so the UI can toggle cumulative vs per-interval honestly:
  - ``daily``  per-day values (channel_daily): cumulative = sum within selected range, delta = as-is.
                 Recent days are NULL until the reporting window closes (YouTube: 3 days).
  - ``total``  lifetime counters (post snapshots, channel followers): delta = consecutive diffs,
                 with the last snapshot before the selected range as the first baseline.
  - ``window`` rolling 30-day aggregates (channel snapshot likes/comments/shares): shown as-is
               in both modes, because neither a running sum nor a diff of a rolling window means anything.
Values with different ``metric_type`` (reach / views / impressions) are never summed together.

Time: snapshot points are ``[epoch_ms, value]`` (stored as UTC epoch seconds). Daily points are
``["YYYY-MM-DD", value]`` -- calendar dates in the platform's own day, passed through untouched
so no timezone can shift them. ``/series`` range is half-open ``[from_ts, to_ts)`` in epoch
seconds; daily rows are kept when their date falls in ``[date(from_ts), date(to_ts - 1)]`` with
both bounds converted to calendar dates in the caller's IANA ``tz``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter

router = APIRouter()

DB_PATH = Path("~/dev/services/_scripts/upload-post-analytics/data/upload_post_analytics.db").expanduser()

METRICS = ("primary", "likes", "comments", "shares", "followers")
PRIMARY_COLS = ("views", "reach", "impressions")
PRIMARY_SERIES = {
    "facebook": "reach", "instagram": "reach", "linkedin": "reach",
    "youtube": "views", "tiktok": "views", "threads": "views",
    "x": "impressions", "pinterest": "impressions", "reddit": "score", "bluesky": "engagement",
}
HEALTH_WINDOW_SECONDS = 24 * 3600
RECENT_JOBS_SECONDS = 7 * 86400
UPLOAD_POST_LINKS = {"upcoming": "https://app.upload-post.com/calendar",
                     "recent": "https://app.upload-post.com/upload-history"}
MAX_TS = 4102444800  # 2100-01-01Z: "unbounded" upper edge, still convertible to a date in any zone


def _connect() -> Optional[sqlite3.Connection]:
    if not DB_PATH.exists():
        return None
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _not_ready() -> dict[str, Any]:
    return {"status": "not_configured", "error": f"collector DB not found at {DB_PATH}"}


def _not_duplicate(db) -> str:
    """SQL filter hiding rows the collector marked as another id of the same publication; a DB from
    before the collector added ``duplicate_of`` has none to hide."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(posts)")}
    return "p.duplicate_of IS NULL" if "duplicate_of" in cols else "1"


def _group_posts(rows) -> list[list]:
    """One group per logical post: shared vault content_id, else shared Upload-Post request_id
    (same multi-platform upload), else a lone post. No title or publish-time heuristics -- see
    suggest_joins.py for a weekly SUGGESTIONS report on everything that doesn't join here."""
    by_content: dict[tuple[str, str], list] = {}
    by_request: dict[tuple[str, str], list] = {}
    lone: list[list] = []
    request_content: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        if r["request_id"] and r["content_id"]:
            request_content.setdefault((r["profile"], r["request_id"]), set()).add(r["content_id"])
    for r in rows:
        content = r["content_id"]
        if not content and r["request_id"]:
            matched = request_content.get((r["profile"], r["request_id"]), set())
            if len(matched) == 1:
                content = next(iter(matched))
        if content:
            by_content.setdefault((r["profile"], content), []).append(r)
        elif r["request_id"]:
            by_request.setdefault((r["profile"], r["request_id"]), []).append(r)
        else:
            lone.append([r])
    groups = list(by_content.values()) + list(by_request.values()) + lone
    groups.sort(key=lambda g: max(x["published_at"] or 0 for x in g), reverse=True)
    return groups


def _group_key(group) -> str:
    content_id = next((x["content_id"] for x in group if x["content_id"]), None)
    if content_id:
        return f"c:{content_id}"
    if group[0]["request_id"]:
        return f"r:{group[0]['request_id']}"
    return "p:" + ",".join(str(x["id"]) for x in sorted(group, key=lambda x: x["id"]))


def _media_kind(platform: str, media_type: Optional[str]) -> str:
    if (media_type or "").upper() in ("VIDEO", "REEL", "REELS"):
        return "Reel" if platform == "instagram" else "Video"
    return "Photo"


def _untitled_label(group) -> str:
    """Several untitled posts must stay distinguishable in the picker: '<kind> · <platform> · <day>'."""
    first = group[0]
    day = (datetime.fromtimestamp(first["published_at"], tz=timezone.utc).strftime("%-d %b")
           if first["published_at"] else "undated")
    platforms = ",".join(sorted({x["platform"] for x in group}))
    kind = _media_kind(first["platform"], first["media_type"])
    return f"{kind} · {platforms} · {day}"


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
            "SELECT p.id, p.profile, p.platform, p.request_id, p.url, p.title, p.media_type,"
            " p.content_id, p.content_title, p.source,"
            " COALESCE(p.published_at, p.first_seen) AS published_at,"
            " (SELECT COUNT(*) FROM post_snapshots s WHERE s.post_id = p.id) AS snapshots"
            f" FROM posts p WHERE {_not_duplicate(db)}").fetchall()
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
        content_id = next((x["content_id"] for x in group if x["content_id"]), None)
        title = (next((x["content_title"] for x in group if x["content_id"] and x["content_title"]), "")
                 if content_id else next((x["title"] for x in group if x["title"]), ""))
        # A vault row's date is the note's calendar day (midnight), coarser than a collected timestamp.
        timed = [x for x in group if x["source"] != "vault"] or group
        prof["posts"].append({
            "key": _group_key(group),
            "title": f"{content_id} \u2014 {title}" if content_id else (title or _untitled_label(group)),
            "published_at": min(x["published_at"] or 0 for x in timed) or None,
            # stats=False: hand-published url from the vault note, collected with no metrics.
            "platforms": [{"platform": x["platform"], "url": x["url"], "stats": x["source"] != "vault"}
                          for x in group],
            "snapshots": sum(x["snapshots"] for x in group),
            "joined_by": "content_id" if content_id else ("request_id" if group[0]["request_id"] else None),
        })
    return {"status": "ok", "profiles": sorted(profiles.values(), key=lambda x: x["profile"]),
            "metrics": list(METRICS)}


def _primary_value(row) -> tuple[Optional[float], Optional[str]]:
    mtype = row["metric_type"]
    if mtype in PRIMARY_COLS and row[mtype] is not None:
        return row[mtype], mtype
    for col in PRIMARY_COLS:
        if row[col] is not None:
            return row[col], col
    return None, mtype


def _date_bounds(from_ts: int, to_ts: int, tz: ZoneInfo) -> tuple[str, str]:
    """Inclusive calendar-date bounds of the epoch range [from_ts, to_ts) as seen in ``tz``."""
    first = datetime.fromtimestamp(from_ts, tz).date()
    last = datetime.fromtimestamp(to_ts - 1, tz).date()
    return first.isoformat(), last.isoformat()


def _channel_series(db, profile: str, metric: str, from_ts: int, to_ts: int, tz: ZoneInfo) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    if metric == "primary":
        d0, d1 = _date_bounds(from_ts, to_ts, tz)
        rows = db.execute(
            "SELECT platform, series, metric_type, date, value FROM channel_daily"
            " WHERE profile=? AND date >= ? AND date <= ? ORDER BY platform, date",
            (profile, d0, d1)).fetchall()
        by_platform: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["series"] != PRIMARY_SERIES.get(r["platform"]) or r["metric_type"] != r["series"]:
                continue
            line = by_platform.setdefault(r["platform"], {
                "platform": r["platform"], "metric_type": r["metric_type"], "kind": "daily", "points": []})
            line["points"].append([r["date"], r["value"]])
        return list(by_platform.values())
    col = metric
    kind = "total" if metric == "followers" else "window"
    rows = db.execute(
        f"SELECT platform, ts, {col} AS v FROM channel_snapshots"
        f" WHERE profile=? AND {col} IS NOT NULL AND ts >= ? AND ts < ? ORDER BY platform, ts",
        (profile, from_ts, to_ts)).fetchall()
    by_platform = {}
    for r in rows:
        line = by_platform.setdefault(r["platform"], {
            "platform": r["platform"], "metric_type": metric, "kind": kind, "points": []})
        line["points"].append([r["ts"] * 1000, r["v"]])
    if kind == "total":
        for platform, line in by_platform.items():
            baseline = db.execute(
                f"SELECT {col} FROM channel_snapshots WHERE profile=? AND platform=? AND {col} IS NOT NULL"
                " AND ts < ? ORDER BY ts DESC, id DESC LIMIT 1", (profile, platform, from_ts)).fetchone()
            line["baseline"] = baseline[0] if baseline else None
    lines.extend(by_platform.values())
    return lines


def _post_series(db, profile: str, key: str, metric: str, from_ts: int, to_ts: int) -> list[dict[str, Any]]:
    kind, _, ident = key.partition(":")
    if kind not in ("c", "r", "p") or not ident:
        return []
    if kind == "p" and not all(x.isdigit() for x in ident.split(",")):
        return []
    candidates = db.execute("SELECT p.id, p.profile, p.platform, p.content_id, p.request_id, p.published_at, p.source"
                            f" FROM posts p WHERE p.profile=? AND {_not_duplicate(db)}", (profile,)).fetchall()
    posts = next((group for group in _group_posts(candidates) if _group_key(group) == key), [])
    col = "followers_gained" if metric == "followers" else metric
    lines = []
    for p in posts:
        if p["source"] == "vault":  # no metrics exist; a line would read as zero
            continue
        snaps = db.execute("SELECT * FROM post_snapshots WHERE post_id=? AND ts >= ? AND ts < ? ORDER BY ts",
                           (p["id"], from_ts, to_ts)).fetchall()
        points, mtype = [], None
        for s in snaps:
            if metric == "primary":
                value, mtype = _primary_value(s)
            else:
                value, mtype = s[col], metric
            if value is not None:
                points.append([s["ts"] * 1000, value])
        baseline_filter = ("(views IS NOT NULL OR reach IS NOT NULL OR impressions IS NOT NULL)"
                           if metric == "primary" else f"{col} IS NOT NULL")
        baseline = db.execute(f"SELECT * FROM post_snapshots WHERE post_id=? AND {baseline_filter}"
                              " AND ts < ? ORDER BY ts DESC, id DESC LIMIT 1", (p["id"], from_ts)).fetchone()
        prior = (_primary_value(baseline)[0] if metric == "primary" else baseline[col]) if baseline else None
        lines.append({"post_id": p["id"], "platform": p["platform"], "metric_type": mtype or metric,
                      "kind": "total", "baseline": prior, "points": points})
    return lines


@router.get("/series")
def get_series(profile: str, target: str = "channel", metric: str = "primary",
               from_ts: Optional[int] = None, to_ts: Optional[int] = None, tz: str = "UTC") -> dict[str, Any]:
    """``from_ts`` inclusive, ``to_ts`` exclusive, UTC epoch seconds; omitted = unbounded on that side."""
    if metric not in METRICS:
        return {"status": "error", "error": f"unknown metric {metric!r}; one of {', '.join(METRICS)}"}
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return {"status": "error", "error": f"unknown time zone {tz!r}"}
    lo = 0 if from_ts is None else max(0, min(from_ts, MAX_TS))
    hi = MAX_TS if to_ts is None else max(0, min(to_ts, MAX_TS))
    if hi <= lo:
        return {"status": "error", "error": "empty range: to_ts must be greater than from_ts"}
    db = _connect()
    if db is None:
        return _not_ready()
    with closing(db):
        if target == "channel":
            lines = _channel_series(db, profile, metric, lo, hi, zone)
        else:
            lines = _post_series(db, profile, target, metric, lo, hi)
    return {"status": "ok", "profile": profile, "target": target, "metric": metric,
            "from_ts": from_ts, "to_ts": to_ts, "tz": tz, "lines": lines}


def _job_platforms(selected: list[str], accounts: dict[str, dict], results: dict[str, dict]) -> list[dict[str, Any]]:
    """Every platform worth showing for one job: selected ones first (in job order), then the profile's
    other connected platforms (not selected), then its known-but-unconnected ones (published by hand)."""
    out = []
    for p in selected:
        acct = accounts.get(p)
        state = "selected" if acct and acct["connected"] and not acct["reauth_required"] else "selected_unconnected"
        out.append({"platform": p, "state": state, "result": results.get(p)})
    for p, acct in sorted(accounts.items()):
        if p in selected:
            continue
        out.append({"platform": p, "state": "not_selected" if acct["connected"] else "manual",
                    "result": results.get(p)})
    return out


def _job_out(row, accounts, results) -> dict[str, Any]:
    try:
        selected = [p for p in json.loads(row["platforms"] or "[]") if isinstance(p, str)]
    except ValueError:
        selected = []
    try:
        content = json.loads(row["platform_content"] or "{}")
    except ValueError:
        content = {}
    return {
        "job_id": row["job_id"], "profile": row["profile"], "scheduled_at": row["scheduled_at"],
        "post_type": row["post_type"], "external_id": row["external_id"], "title": row["title"],
        "source_filename": row["source_filename"], "original_timezone": row["original_timezone"],
        "platforms": _job_platforms(selected, accounts.get(row["profile"], {}), results.get(row["job_id"], {})),
        "platform_content": content if isinstance(content, dict) else {},
        "cover_url": row["cover_preview_url"] or row["thumbnail_url"],
        "status": row["status"], "status_message": row["status_message"], "gone_at": row["gone_at"],
    }


@router.get("/scheduled")
def get_scheduled() -> dict[str, Any]:
    """Upcoming Upload-Post jobs (still in its schedule list) and jobs whose slot passed in the last 7 days.
    Upload-Post's web app has no per-job URL (its /calendar and /scheduled-posts views read no query
    parameters), so rows link to the calendar / history pages."""
    db = _connect()
    if db is None:
        return _not_ready()
    now = int(time.time())
    with closing(db):
        try:
            jobs = db.execute("SELECT * FROM scheduled_jobs WHERE gone_at IS NULL OR scheduled_at >= ?"
                              " ORDER BY scheduled_at", (now - RECENT_JOBS_SECONDS,)).fetchall()
            res_rows = db.execute("SELECT r.* FROM scheduled_results r JOIN scheduled_jobs j USING (job_id)"
                                  " WHERE j.gone_at IS NOT NULL AND j.scheduled_at >= ?",
                                  (now - RECENT_JOBS_SECONDS,)).fetchall()
        except sqlite3.OperationalError:
            # Collector not yet upgraded to the version that creates these tables.
            return {"status": "ok", "now": now, "collecting": False, "upcoming": [], "recent": [],
                    "links": UPLOAD_POST_LINKS}
        acct_rows = db.execute("SELECT profile, platform, connected, reauth_required FROM accounts").fetchall()
    accounts: dict[str, dict[str, dict]] = {}
    for a in acct_rows:
        accounts.setdefault(a["profile"], {})[a["platform"]] = {
            "connected": bool(a["connected"]), "reauth_required": bool(a["reauth_required"])}
    results: dict[str, dict[str, dict]] = {}
    for r in res_rows:
        results.setdefault(r["job_id"], {})[r["platform"]] = {
            "status": r["status"], "post_url": r["post_url"], "error": r["error"], "upload_ts": r["upload_ts"]}
    upcoming = [_job_out(j, accounts, results) for j in jobs if j["gone_at"] is None]
    recent = [_job_out(j, accounts, results) for j in jobs if j["gone_at"] is not None]
    recent.sort(key=lambda j: j["scheduled_at"], reverse=True)
    return {"status": "ok", "now": now, "collecting": True, "upcoming": upcoming, "recent": recent,
            "links": UPLOAD_POST_LINKS}


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
