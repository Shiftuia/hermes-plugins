"""Provider-usage dashboard plugin — backend API routes, mounted at /api/plugins/provider-usage/.

Aggregates three cheap, request-time reads into one payload so the dashboard tab can render
"how much have I got left right now" without round-tripping three separate calls:

  1. Claude (anthropic) 5h-session + weekly window usage      — agent.account_usage.fetch_account_usage
  2. OpenAI Codex session + weekly window usage                — agent.account_usage.fetch_account_usage
  3. OpenRouter: Hermes's own key quota + credits balance       — agent.account_usage.fetch_account_usage
  4. OpenRouter: ALL keys on the account (management API)       — GET https://openrouter.ai/api/v1/keys

Security notes (read before touching this file):
  - The OpenRouter management key lives in ~/services/_scripts/openrouter-spend/.env. It is read
    fresh on every request, NEVER cached on the module/process, NEVER logged, and NEVER placed in
    an HTTP response. Only the per-key masked ``label`` (already masked by OpenRouter, e.g.
    "sk-or-v1-xxx...yyy") is forwarded to the browser.
  - fetch_account_usage() swallows its own exceptions and returns None on failure. This module
    must not conflate that with "nothing to show" — every provider section carries an explicit
    ``status`` field (ok / unavailable / error) so the UI can render a real error state.
  - All three provider fetches + the OpenRouter keys fetch run concurrently (asyncio.gather) with
    a per-call timeout, so one slow/hung upstream can't stall the dashboard request.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()

# Per-upstream-call budget. The dashboard route must stay cheap; a hung provider degrades to an
# error card rather than hanging the whole tab.
_FETCH_TIMEOUT_SECONDS = 12.0

_OPENROUTER_ENV_PATH = Path("~/services/_scripts/openrouter-spend/.env").expanduser()
_OPENROUTER_MGMT_ENV_VAR = "OPENROUTER_MANAGEMENT_API_KEY"
_OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/keys"

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _unquote_env_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _read_openrouter_management_key() -> Optional[str]:
    """Read OPENROUTER_MANAGEMENT_API_KEY straight from the secrets .env at request time.

    Never cached, never logged. Falls back to the process environment (in case it's exported
    there instead) before giving up. Returns None (not an exception) on any failure so callers
    degrade to a clear "not configured" state instead of a 500.
    """
    env_value = os.environ.get(_OPENROUTER_MGMT_ENV_VAR, "").strip()
    if env_value:
        return env_value
    try:
        text = _OPENROUTER_ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if name == _OPENROUTER_MGMT_ENV_VAR:
            value = _unquote_env_value(value)
            return value or None
    return None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _window_dict(window) -> dict[str, Any]:
    """AccountUsageWindow -> wire dict. Reuses agent.account_usage's own reset-time formatting
    (``_format_reset``) instead of reimplementing the "resets in 56m" math."""
    from agent.account_usage import _format_reset

    used = window.used_percent
    return {
        "label": window.label,
        "used_percent": round(float(used), 1) if _is_num(used) else None,
        "remaining_percent": round(max(0.0, 100.0 - float(used)), 1) if _is_num(used) else None,
        "reset_at": window.reset_at.isoformat() if window.reset_at else None,
        "reset_in_human": _format_reset(window.reset_at) if window.reset_at else None,
        "detail": window.detail,
    }


_CODEX_BANKED_DETAIL_RE = re.compile(r"You have (\d+) reset")


def _codex_banked_count_from_details(details: list[str]) -> int:
    """Pull the banked-reset-credit count back out of the human-readable detail line that
    ``agent.account_usage._fetch_codex_account_usage`` already generates (``"You have N resets
    banked - ..."``). This is display-layer parsing of a string produced by the same module —
    not a reimplementation of the guard/consume logic — done so the button can be disabled at
    0 banked without a second round-trip."""
    for line in details:
        match = _CODEX_BANKED_DETAIL_RE.search(line)
        if match:
            return int(match.group(1))
    return 0


def _snapshot_dict(snapshot) -> dict[str, Any]:
    details = list(snapshot.details)
    result: dict[str, Any] = {
        "status": "ok",
        "source": snapshot.source,
        "title": snapshot.title,
        "plan": snapshot.plan,
        "windows": [_window_dict(w) for w in snapshot.windows],
        "details": details,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "unavailable_reason": snapshot.unavailable_reason,
        "error": None,
    }
    if snapshot.provider == "openai-codex":
        result["banked_reset_credits"] = _codex_banked_count_from_details(details)
    return result


async def _fetch_provider_section(provider: str) -> dict[str, Any]:
    """One provider's card payload. fetch_account_usage() is synchronous (uses httpx.Client) and
    swallows its own exceptions -> None. Run it off the event loop and distinguish None
    ("unavailable" — e.g. no OAuth creds configured for this box) from a raised exception here
    ("error" — something actually broke the request path) so the UI never renders a blank card
    for a genuine failure.
    """
    from agent.account_usage import fetch_account_usage

    try:
        snapshot = await asyncio.wait_for(
            asyncio.to_thread(fetch_account_usage, provider), timeout=_FETCH_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return {"status": "error", "error": f"timed out after {_FETCH_TIMEOUT_SECONDS:.0f}s", "windows": [], "details": []}
    except Exception as exc:  # defence in depth; fetch_account_usage already fails open
        log.warning("provider-usage: %s fetch raised unexpectedly: %s", provider, exc)
        return {"status": "error", "error": str(exc), "windows": [], "details": []}
    if snapshot is None:
        return {
            "status": "unavailable",
            "error": "no live credentials/session found for this provider on this box",
            "windows": [],
            "details": [],
        }
    return _snapshot_dict(snapshot)


def _key_row(raw: dict[str, Any]) -> dict[str, Any]:
    """One OpenRouter management-API key row -> wire dict. Values are rendered exactly as the
    API returns them (a key can report limit_remaining inconsistent with usage/limit — that's
    the API's business, not ours to "correct" with our own arithmetic). ``updated_at``/``created_at``
    can be null; nothing here slices/formats them beyond a plain pass-through.
    """
    return {
        "name": raw.get("name"),
        "label": raw.get("label"),
        "usage": raw.get("usage"),
        "usage_daily": raw.get("usage_daily"),
        "usage_weekly": raw.get("usage_weekly"),
        "usage_monthly": raw.get("usage_monthly"),
        "limit": raw.get("limit"),
        "limit_remaining": raw.get("limit_remaining"),
        "limit_reset": raw.get("limit_reset"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "disabled": bool(raw.get("disabled")) if raw.get("disabled") is not None else None,
    }


async def _fetch_openrouter_keys_section() -> dict[str, Any]:
    """All OpenRouter keys on the account via the management API. Degrades to a clear
    "management key not configured" status (not an exception, not a blank section) when the
    secrets .env / env var is missing — the Claude/Codex cards must still render regardless.
    """
    mgmt_key = _read_openrouter_management_key()
    if not mgmt_key:
        return {
            "status": "not_configured",
            "error": f"{_OPENROUTER_MGMT_ENV_VAR} not found in {_OPENROUTER_ENV_PATH} or the environment",
            "keys": [],
        }
    headers = {"Authorization": f"Bearer {mgmt_key}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(_OPENROUTER_KEYS_URL, headers=headers)
        resp.raise_for_status()
        payload = resp.json() or {}
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        # Never echo response body back to the client: an auth failure's body can echo the
        # Authorization header contents on some gateways.
        return {"status": "error", "error": f"OpenRouter management API returned HTTP {status_code}", "keys": []}
    except asyncio.TimeoutError:
        return {"status": "error", "error": f"timed out after {_FETCH_TIMEOUT_SECONDS:.0f}s", "keys": []}
    except Exception as exc:
        log.warning("provider-usage: openrouter keys fetch failed: %s", exc)
        return {"status": "error", "error": "could not reach the OpenRouter management API", "keys": []}
    finally:
        mgmt_key = None  # belt-and-braces: drop the local reference promptly
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        rows = []
    return {"status": "ok", "keys": [_key_row(r) for r in rows if isinstance(r, dict)]}


@router.get("/usage")
async def get_usage() -> dict[str, Any]:
    """Everything the tab needs in one call, fetched concurrently."""
    anthropic, codex, openrouter_key, openrouter_keys = await asyncio.gather(
        _fetch_provider_section("anthropic"),
        _fetch_provider_section("openai-codex"),
        _fetch_provider_section("openrouter"),
        _fetch_openrouter_keys_section(),
    )
    return {
        "anthropic": anthropic,
        "openai_codex": codex,
        "openrouter_key": openrouter_key,
        "openrouter_keys": openrouter_keys,
    }


class CodexResetRedeemBody(BaseModel):
    """Empty-by-default; ``force`` must be explicit and only ever sent as the second, confirmed
    call — the UI's first click always POSTs with force omitted/false."""

    force: bool = False


@router.post("/codex-reset")
async def post_codex_reset(payload: CodexResetRedeemBody | None = None) -> dict[str, Any]:
    """Redeem one banked Codex reset credit. Thin wrapper around
    ``agent.account_usage.redeem_codex_reset_credit`` — all guard/consume logic lives there.

    This route is deliberately POST-only (a GET on this path 405s via FastAPI's default
    method-not-allowed handling) so a prefetch/crawler/browser-extension can never trigger an
    irreversible spend. ``force`` defaults to False; the frontend only ever sends
    ``force=true`` from its second, explicit confirmation step.
    """
    from agent.account_usage import redeem_codex_reset_credit

    force = bool(payload.force) if payload is not None else False
    try:
        result = await asyncio.to_thread(redeem_codex_reset_credit, force=force)
    except Exception as exc:  # redeem_codex_reset_credit is documented to never raise; defence in depth
        log.warning("provider-usage: codex reset redemption raised unexpectedly: %s", exc)
        return {
            "status": "unavailable",
            "message": f"Unexpected error while redeeming: {exc}",
            "available_count": 0,
            "windows_reset": 0,
            "redeemed": False,
        }
    log.info(
        "provider-usage: codex reset redemption attempt — status=%s force=%s banked_after=%d",
        result.status, force, result.available_count,
    )
    return {
        "status": result.status,
        "message": result.message,
        "available_count": result.available_count,
        "windows_reset": result.windows_reset,
        "redeemed": result.redeemed,
    }
