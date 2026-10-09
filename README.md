# hermes-plugins

Dashboard plugins for [Hermes Agent](https://hermes-agent.nousresearch.com/docs). Drop a
subdirectory into `~/.hermes/plugins/` (or clone this whole repo there) and Hermes picks up
any plugin with a `dashboard/manifest.json` automatically — no separate install step.

## Install

```
git clone https://github.com/Shiftuia/hermes-plugins.git ~/.hermes/plugins
```

If `~/.hermes/plugins` already has content, clone elsewhere and copy the plugin subdirectory
you want in. Restart the Hermes dashboard (or call `POST /api/dashboard/plugins/rescan`) to
pick up new/changed plugins without a full restart.

## Plugins

### provider-usage

Dashboard tab: live Claude (Anthropic) 5h/weekly usage, Claude Fable model-scoped quota (when
active), Codex session/weekly usage, Antigravity (agy) pool limits, and OpenRouter credits +
all-key limits (via the OpenRouter management API). Each provider card fetches its own data
independently (`GET /usage/anthropic`, `/usage/openai-codex`, `/usage/openrouter-key`,
`/usage/openrouter-keys`, `/usage/antigravity`) — opening the tab never waits on a slow
provider (e.g. Antigravity's `agy` subprocess) to show the others; each card shows its own
spinner until its own fetch resolves.

- `dashboard/plugin_api.py` — backend routes (`GET /usage/<provider>`, `GET /capabilities`,
  `POST /codex-reset`), mounted at `/api/plugins/provider-usage/`.
- `dashboard/dist/` — prebuilt frontend bundle (`index.js` + `style.css`). This is a checked-in
  build artifact, not source — there is no frontend source tree in this repo. To rebuild from
  scratch you'd need to recreate the React/TS tab against the routes above; the existing `dist/`
  is kept because that's the only copy and it's what Hermes actually loads.
- Credential reads run inside the dashboard's per-request profile secret scope
  (`_config_profile_scope`), same pattern as every other extracted Hermes dashboard router —
  required so a multiplexed dashboard host (several profiles behind one process) resolves the
  right profile's credentials instead of failing closed.
- Optional: set `OPENROUTER_MANAGEMENT_API_KEY` (see `config/openrouter-spend.env.example`) to
  populate the "every key on the account" table. Without it, the Hermes-runtime-key card still
  works; only the account-wide keys table degrades to "not configured".

### upload-post-analytics

Dashboard tab "Post analytics": one chart over the SQLite written by the Upload-Post collector
(`~/dev/services/_scripts/upload-post-analytics`, shared thinkcentre-scripts repo). Read-only
(`mode=ro`), never calls the Upload-Post API.

- Selectors: profile → channel overall or a single post; metric (views/reach/impressions, likes,
  comments, shares, followers); cumulative vs per-interval; time range.
- Range: presets 1 day … 1 year are "now minus N" in absolute time (1 day = last 24 h); "Since
  publish" for a post; Custom… takes local date+time `from–to`. Default 1 month for the channel,
  1 week for a post (since publish if newer). Display/input is in the browser's zone, labelled next to
  the picker (e.g. `IDT`, or `IDT → IST` when the range crosses a DST change). The client converts to
  UTC epoch seconds; the backend filters `from_ts <= ts < to_ts` and never parses naive strings.
- Daily channel series are platform calendar dates, returned as `"YYYY-MM-DD"` and plotted at that date —
  never shifted by a zone. They are kept when the date lies in `[date(from_ts), date(to_ts - 1)]`, both
  dates taken in the browser's `tz`. Tooltip: plain date for daily series, local date-time + zone for snapshots.
- One line per platform, labelled with its own `metric_type` — reach, views and impressions are never
  summed. Legend sits on top of the chart: click an item to hide/show its line, hover to highlight it;
  hovering the plot shows date + every visible line's value. Dashed = rolling 30-day window series.
  Cross-posts are grouped by vault `content_id`, else by Upload-Post `request_id`; anything else is a lone post.
- Channel views/reach come from the daily series (cumulative = running sum); post metrics and channel
  followers are lifetime counters (per-interval = diff between snapshots); channel likes/comments/shares
  are Upload-Post's rolling 30-day totals and are shown as-is in both modes.
- Strip on top: last 24 h ticks, requests, min `X-RateLimit-Remaining`, 429 count, latest errors.
- Routes: `GET /options`, `/series?profile=&target=channel|c:<content_id>|r:<request_id>|p:<ids>&metric=&from_ts=&to_ts=&tz=`, `/health`.
- Needs `upload-post-analytics` in `plugins.enabled` (config.yaml) and a dashboard restart to mount
  the backend. Tests: `PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/pytest
  upload-post-analytics/dashboard provider-usage/dashboard` and
  `node --test upload-post-analytics/dashboard/test_panel_render.cjs` (renders `dist/index.js` in jsdom with
  hermes-agent's React; drives every range for channel and post against both the current and the
  pre-range `/series` shape).
- The page is wrapped in an error boundary: a render exception shows the message + stack instead of a
  blank tab. If the backend predates the range filter (dashboard not restarted after an update), the
  panel still renders and says the range is not applied until a restart.

### kanban-delivery-gate

Scaffolding only — a `tests/` directory with no implementation yet. Not a dashboard plugin
(no `manifest.json`), not wired into anything. Intended purpose per its name: gate Kanban task
completion on deterministic delivery checks. Whoever picks this up next should treat it as an
empty slate, not a partially-built feature.

## License

MIT — see `LICENSE`.
