# hermes-plugins

> **Archived 2026-10-16.** The dashboard plugins provider-usage and upload-post-analytics were removed
> (HQ replaces them). finance-review-bridge is still loaded from the local checkout until it moves to its owning repo.

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

### kanban-delivery-gate

Scaffolding only — a `tests/` directory with no implementation yet. Not a dashboard plugin
(no `manifest.json`), not wired into anything. Intended purpose per its name: gate Kanban task
completion on deterministic delivery checks. Whoever picks this up next should treat it as an
empty slate, not a partially-built feature.

### finance-review-bridge

Gateway plugin for synthetic finance review in one restricted Discord guild text
channel. Reuses the gateway's Discord client and exposes an owner-only Unix socket
under the active Hermes profile's `plugin-data/finance-review-bridge/`. Notices and
replies bind to a case ID, version and digest; this plugin never writes a ledger.

Enable `finance-review-bridge` in `plugins.enabled`. Configure all four exact
Discord IDs (`bot_id`, `actor_id`, `guild_id`, `channel_id`) under
`plugins.entries.finance-review-bridge.settings` in the profile's `config.yaml`,
or use the environment variables in `config/finance-review-bridge.env.example`.
Plugin settings take precedence over environment variables. No IDs or credentials
are shipped; missing or malformed IDs reject registration before any connection.
When migrating an existing installation, copy its four IDs into profile settings
before restarting the gateway with this version. The plugin uses the gateway's
existing credentials and does not need another bot token.

Both `DISCORD_ALLOWED_CHANNELS` and `DISCORD_FREE_RESPONSE_CHANNELS` must explicitly
include the finance channel without a wildcard. The bridge audits gateway identity,
channel permissions and routing before handling requests. Restart the gateway after
configuration changes. Smoke tests (no Discord connection or live state):
`~/.hermes/hermes-agent/venv/bin/pytest tests/test_finance_review_bridge.py`.

## License

MIT — see `LICENSE`.
