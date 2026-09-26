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
active), Codex session/weekly usage, and OpenRouter credits + all-key limits (via the
OpenRouter management API). Fetched fresh on every page load — no caching, no time series.

- `dashboard/plugin_api.py` — backend routes (`GET /usage`, `GET /capabilities`,
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

### kanban-delivery-gate

Scaffolding only — a `tests/` directory with no implementation yet. Not a dashboard plugin
(no `manifest.json`), not wired into anything. Intended purpose per its name: gate Kanban task
completion on deterministic delivery checks. Whoever picks this up next should treat it as an
empty slate, not a partially-built feature.

## License

MIT — see `LICENSE`.
