"""Provider-usage dashboard plugin — backend API routes, mounted at /api/plugins/provider-usage/.

Aggregates cheap, request-time reads into one payload so the dashboard tab can render
"how much have I got left right now" without round-tripping several separate calls:

  1. Claude (anthropic) 5h-session + weekly window usage      — agent.account_usage.fetch_account_usage
  2. Claude per-model scoped quota (e.g. a Fable weekly cap)   — this module's own oauth/usage read (see below)
  3. OpenAI Codex session + weekly window usage                — agent.account_usage.fetch_account_usage
  4. OpenRouter: Hermes's own key quota + credits balance       — agent.account_usage.fetch_account_usage
  5. OpenRouter: ALL keys on the account (management API)       — GET https://openrouter.ai/api/v1/keys

Claude Fable usage (Sep 2026): the Anthropic OAuth usage endpoint
(``https://api.anthropic.com/api/oauth/usage``) that ``agent.account_usage`` already calls for the
account-wide five_hour/seven_day windows ALSO returns a ``limits[]`` array. Most accounts only ever
see ``kind: session`` / ``kind: weekly_all`` entries there (duplicates of the two windows above,
scope=null) — but when Anthropic has scoped a distinct per-model weekly allocation to the account
(observed as a ``weekly_scoped`` entry with ``scope.model.display_name == "Fable"``), that entry is
a genuinely SEPARATE quota, not a relabeling of the account totals. `agent/account_usage.py` doesn't
parse `limits[]` at all (core only exposes five_hour/seven_day/seven_day_opus/seven_day_sonnet), and
this plugin must not modify core (`plugins/AGENTS.md`: "Plugins never touch core"). So this module
does its OWN read of the same oauth/usage endpoint — reusing only the core token
resolver/OAuth-detector (`agent.anthropic_credentials`) — parses `limits[]` for model-scoped entries,
and renders a real Fable card ONLY when Anthropic reports one active for this account. When no
model-scoped limit is present the Fable card explicitly states that (never a fabricated duplicate of
the account bars) — see `_fetch_anthropic_model_scoped_usage` and `/capabilities`.

Security notes (read before touching this file):
  - The OpenRouter management key lives in ~/dev/services/_scripts/openrouter-spend/.env. It is read
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
from fastapi import APIRouter, Query
from pydantic import BaseModel

from hermes_cli.web_deps import late

log = logging.getLogger(__name__)

# Late-bound so this stays a thin caller of the dashboard's own profile-scope seam
# instead of importing hermes_cli.web_server_profiles at module load (import-cycle safe,
# consistent with every other extracted router — see hermes_cli/web_routers/_common.py).
_config_profile_scope = late("_config_profile_scope", "hermes_cli.web_server_profiles")

router = APIRouter()

# Per-upstream-call budget. The dashboard route must stay cheap; a hung provider degrades to an
# error card rather than hanging the whole tab.
_FETCH_TIMEOUT_SECONDS = 12.0

_OPENROUTER_ENV_PATH = Path("~/dev/services/_scripts/openrouter-spend/.env").expanduser()
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


# Kinds/groups from Anthropic's oauth/usage `limits[]` array that duplicate the account-wide
# five_hour/seven_day windows `_fetch_provider_section("anthropic")` already renders (scope=null).
# Only a `scope.model.display_name` entry is a genuinely distinct allocation worth a second card.
_ACCOUNT_WIDE_LIMIT_KINDS = {"session", "weekly_all"}


def _fetch_anthropic_model_scoped_usage() -> dict[str, Any]:
    """Claude per-model scoped quota (e.g. a Fable-specific weekly cap), synchronous.

    Reuses ONLY the core token resolver/OAuth-detector (``agent.anthropic_credentials`` — no
    plugin-owned credential logic) and re-reads the same
    ``https://api.anthropic.com/api/oauth/usage`` endpoint ``agent.account_usage`` already calls,
    because that module's dataclass only surfaces the four top-level utilization fields and does
    not parse the response's ``limits[]`` array. Adding that parsing to core would be a design
    decision (not this plugin's call — plugins never touch core, see plugins/AGENTS.md), so it
    lives here. Returns a wire dict shaped like ``_fetch_provider_section``'s output plus a
    ``model_scoped`` flag the UI uses to render "no distinct Fable quota" instead of a fake bar.
    """
    from agent.anthropic_credentials import _is_oauth_token, resolve_anthropic_token

    try:
        token = (resolve_anthropic_token() or "").strip()
    except Exception as exc:
        log.warning("provider-usage: anthropic model-scoped token resolve failed: %s", exc)
        return {"status": "error", "error": str(exc), "windows": [], "details": [], "model_scoped": False}
    if not token:
        return {"status": "unavailable", "error": "no live credentials/session found for this provider on this box",
                 "windows": [], "details": [], "model_scoped": False}
    if not _is_oauth_token(token):
        return {"status": "unavailable", "error": "model-scoped limits are only available for OAuth-backed Claude accounts",
                 "windows": [], "details": [], "model_scoped": False}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json",
               "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"}
    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = client.get("https://api.anthropic.com/api/oauth/usage", headers=headers)
            resp.raise_for_status()
            payload = resp.json() or {}
    except httpx.HTTPStatusError as exc:
        return {"status": "error", "error": f"Anthropic oauth/usage returned HTTP {exc.response.status_code}",
                 "windows": [], "details": [], "model_scoped": False}
    except Exception as exc:
        log.warning("provider-usage: anthropic model-scoped fetch failed: %s", exc)
        return {"status": "error", "error": "could not reach the Anthropic oauth/usage endpoint",
                 "windows": [], "details": [], "model_scoped": False}
    from agent.account_usage import AccountUsageWindow, _format_reset, _parse_dt

    windows: list[dict[str, Any]] = []
    for item in payload.get("limits") or []:
        if not isinstance(item, dict) or str(item.get("kind") or "").strip() in _ACCOUNT_WIDE_LIMIT_KINDS:
            continue
        model = (item.get("scope") or {}).get("model") or {}
        display_name = str(model.get("display_name") or "").strip()
        percent = item.get("percent")
        if not display_name or not _is_num(percent):
            continue
        cadence = {"weekly": "weekly", "session": "session"}.get(str(item.get("group") or "").strip(), "window")
        reset_at = _parse_dt(item.get("resets_at"))
        window = AccountUsageWindow(label=f"{display_name} {cadence}", used_percent=float(percent), reset_at=reset_at)
        windows.append({
            "label": window.label,
            "used_percent": round(float(percent), 1),
            "remaining_percent": round(max(0.0, 100.0 - float(percent)), 1),
            "reset_at": reset_at.isoformat() if reset_at else None,
            "reset_in_human": _format_reset(reset_at) if reset_at else None,
            "detail": f"active: {item.get('is_active')}" if item.get("is_active") is not None else None,
        })
    if not windows:
        return {
            "status": "ok", "windows": [], "details": [],
            "unavailable_reason": "This Claude account has no distinct Fable/model-scoped quota right now — "
                                   "Fable usage is drawn from the shared Claude session/weekly windows above.",
            "model_scoped": False, "error": None,
        }
    return {"status": "ok", "windows": windows, "details": [], "unavailable_reason": None, "model_scoped": True, "error": None}


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
async def get_usage(profile: Optional[str] = Query(None)) -> dict[str, Any]:
    """Everything the tab needs in one call, fetched concurrently.

    ``profile`` matches every other dashboard route's query param (the Profiles switcher):
    the multiplexed dashboard host serves several profiles from one process, and credential
    reads (``resolve_anthropic_token``, ``resolve_runtime_provider``, ...) go through
    ``agent.secret_scope.get_secret``, which fails closed with no profile scope installed.
    Without this wrapper every fetch below silently raised ``UnscopedSecretError``, caught by
    the broad ``except Exception`` guards and rendered as a fake "no live credentials" status
    even when the profile's credentials are perfectly valid.
    """
    with _config_profile_scope(profile):
        anthropic, anthropic_model_scoped, codex, openrouter_key, openrouter_keys = await asyncio.gather(
            _fetch_provider_section("anthropic"),
            asyncio.wait_for(asyncio.to_thread(_fetch_anthropic_model_scoped_usage), timeout=_FETCH_TIMEOUT_SECONDS),
            _fetch_provider_section("openai-codex"),
            _fetch_provider_section("openrouter"),
            _fetch_openrouter_keys_section(),
            return_exceptions=True,
        )
    if isinstance(anthropic_model_scoped, BaseException):
        log.warning("provider-usage: anthropic model-scoped fetch raised unexpectedly: %s", anthropic_model_scoped)
        anthropic_model_scoped = {"status": "error", "error": str(anthropic_model_scoped), "windows": [], "details": [], "model_scoped": False}
    return {
        "anthropic": anthropic,
        "anthropic_model_scoped": anthropic_model_scoped,
        "openai_codex": codex,
        "openrouter_key": openrouter_key,
        "openrouter_keys": openrouter_keys,
    }


@router.get("/capabilities")
async def get_capabilities() -> dict[str, Any]:
    """Factual description of what this plugin can retrieve per provider — no secrets, no live
    fetch (static metadata only, always cheap). Surfaced in the UI as an expandable details panel
    so "does this plugin see all my Claude/OpenAI/OpenRouter usage?" has a concrete, truthful
    answer instead of being inferred from card behavior.
    """
    return {
        "sources": [
            {
                "provider": "anthropic",
                "label": "Claude (Anthropic)",
                "endpoint": "https://api.anthropic.com/api/oauth/usage (OAuth-only)",
                "retrieves": [
                    "Current 5-hour session window (% used, reset time)",
                    "Current 7-day account-wide window (% used, reset time)",
                    "Opus-specific 7-day window and Sonnet-specific 7-day window, when the account reports them",
                    "Model-scoped weekly quotas (e.g. Fable), when Anthropic has one active for this account — "
                    "rendered as its own card, never fabricated when absent",
                    "Extra-usage credits balance, when pay-as-you-go overage is enabled",
                ],
                "does_not_retrieve": [
                    "Per-conversation or per-session token/cost breakdowns",
                    "Historical usage (this is a live snapshot, not a time series)",
                    "Usage for API-key (non-OAuth) Anthropic accounts — those show 'unavailable' by design",
                ],
                "auth": "OAuth-backed Claude Code / Claude subscription session on this box",
            },
            {
                "provider": "openai-codex",
                "label": "OpenAI Codex",
                "endpoint": "ChatGPT/Codex backend rate-limit + credits usage API",
                "retrieves": [
                    "Session and weekly rate-limit windows (% used, reset time)",
                    "Plan type (e.g. Plus)",
                    "Banked reset-credit count and redemption (via the Redeem button)",
                    "Prepaid credits balance, when the account has any",
                ],
                "does_not_retrieve": [
                    "Historical usage (live snapshot only)",
                    "Per-conversation cost breakdowns",
                ],
                "auth": "ChatGPT-account OAuth session, or a pooled Codex credential, on this box",
            },
            {
                "provider": "openrouter",
                "label": "OpenRouter",
                "endpoint": "https://openrouter.ai/api/v1/credits + /key (Hermes's own key) and /keys (management API, all keys)",
                "retrieves": [
                    "Hermes's own OpenRouter key: quota window, credits balance, today/week/month usage",
                    "Every key on the account (management API): usage, limit, remaining, reset schedule — masked labels only",
                ],
                "does_not_retrieve": [
                    "The raw key value or the management API key itself (never sent to the browser, never logged)",
                    "Per-model spend breakdown within a key",
                ],
                "auth": "Hermes's runtime OpenRouter API key; the account-wide keys table additionally needs "
                        "OPENROUTER_MANAGEMENT_API_KEY configured in ~/dev/services/_scripts/openrouter-spend/.env",
            },
        ],
    }


class CodexResetRedeemBody(BaseModel):
    """Empty-by-default; ``force`` must be explicit and only ever sent as the second, confirmed
    call — the UI's first click always POSTs with force omitted/false."""

    force: bool = False


@router.post("/codex-reset")
async def post_codex_reset(payload: CodexResetRedeemBody | None = None, profile: Optional[str] = Query(None)) -> dict[str, Any]:
    """Redeem one banked Codex reset credit. Thin wrapper around
    ``agent.account_usage.redeem_codex_reset_credit`` — all guard/consume logic lives there.

    This route is deliberately POST-only (a GET on this path 405s via FastAPI's default
    method-not-allowed handling) so a prefetch/crawler/browser-extension can never trigger an
    irreversible spend. ``force`` defaults to False; the frontend only ever sends
    ``force=true`` from its second, explicit confirmation step.

    ``profile``-scoped like ``/usage`` (see its docstring) — the credential read must run under
    the target profile's secret scope on a multiplexed dashboard host.
    """
    from agent.account_usage import redeem_codex_reset_credit

    force = bool(payload.force) if payload is not None else False
    try:
        def _run():
            with _config_profile_scope(profile):
                return redeem_codex_reset_credit(force=force)
        result = await asyncio.to_thread(_run)
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
