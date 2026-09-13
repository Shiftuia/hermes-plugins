(function () {
  "use strict";
  // provider-usage dashboard plugin: Claude / Codex / OpenRouter usage at a glance.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const hooks = SDK.hooks;
  const C = SDK.components;

  function api(path, options) {
    // fetchJSON handles auth for both loopback (session-token header) and gated
    // (cookie) dashboard modes — never hand-roll fetch + read the token directly.
    return SDK.fetchJSON("/api/plugins/provider-usage" + path, options);
  }

  function barColor(pct) {
    if (pct == null) return "pu-bar-unknown";
    if (pct >= 90) return "pu-bar-critical";
    if (pct >= 70) return "pu-bar-warn";
    return "pu-bar-ok";
  }

  function UsageBar({ label, pct, detail, resetHuman }) {
    const known = pct != null;
    return React.createElement(
      "div",
      { className: "pu-window" },
      React.createElement(
        "div",
        { className: "pu-window-head" },
        React.createElement("span", { className: "pu-window-label" }, label),
        React.createElement(
          "span",
          { className: "pu-window-pct" },
          known ? Math.round(pct) + "% used" : "unknown"
        )
      ),
      React.createElement(
        "div",
        { className: "pu-bar-track" },
        React.createElement("div", {
          className: "pu-bar-fill " + barColor(pct),
          style: { width: (known ? Math.min(100, Math.max(0, pct)) : 0) + "%" },
        })
      ),
      (resetHuman || detail) &&
        React.createElement(
          "div",
          { className: "pu-window-foot" },
          resetHuman ? "resets " + resetHuman : detail
        )
    );
  }

  function ErrorState({ status, error }) {
    const label =
      status === "not_configured"
        ? "Not configured"
        : status === "unavailable"
        ? "Unavailable"
        : "Error";
    return React.createElement(
      "div",
      { className: "pu-error" },
      React.createElement("strong", null, label + ": "),
      error || "no details"
    );
  }

  function CodexResetButton({ section, onRedeemed }) {
    const [busy, setBusy] = hooks.useState(false);
    const [result, setResult] = hooks.useState(null); // last non-force response (for the force gate)
    const [confirming, setConfirming] = hooks.useState(false);
    const [error, setError] = hooks.useState(null);

    const banked =
      section && section.status === "ok" && typeof section.banked_reset_credits === "number"
        ? section.banked_reset_credits
        : 0;

    function redeem(force) {
      setBusy(true);
      setError(null);
      api("/codex-reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: !!force }),
      })
        .then(function (res) {
          setResult(res);
          setConfirming(false);
          if (res.redeemed && onRedeemed) onRedeemed();
        })
        .catch(function (e) {
          setError(String((e && e.message) || e));
          setConfirming(false);
        })
        .finally(function () {
          setBusy(false);
        });
    }

    function handleClick() {
      if (confirming) {
        // Second, explicit confirmation — this is the ONLY path that may send force=true.
        redeem(true);
        return;
      }
      redeem(false);
    }

    // A `not_exhausted` first-call response is what unlocks the force-confirm gate; any other
    // status (reset / no_credits_banked / already_redeemed / unavailable / …) never does.
    hooks.useEffect(
      function () {
        if (result && result.status === "not_exhausted" && !result.redeemed) {
          setConfirming(true);
        } else {
          setConfirming(false);
        }
      },
      [result]
    );

    return React.createElement(
      "div",
      { className: "pu-codex-reset" },
      React.createElement(
        C.Button,
        {
          onClick: handleClick,
          disabled: busy || banked <= 0,
          className: confirming ? "pu-codex-reset-force" : "",
        },
        busy
          ? "Working…"
          : confirming
          ? "Redeem anyway (force)"
          : "Redeem reset credit"
      ),
      banked <= 0 &&
        React.createElement(
          "div",
          { className: "pu-muted pu-codex-reset-note" },
          "No banked reset credits — nothing to redeem."
        ),
      error &&
        React.createElement("div", { className: "pu-error pu-codex-reset-note" }, error),
      result &&
        React.createElement(
          "div",
          {
            className:
              "pu-codex-reset-note " + (result.redeemed ? "pu-codex-reset-ok" : "pu-codex-reset-warn"),
          },
          result.message
        ),
      confirming &&
        React.createElement(
          "div",
          { className: "pu-codex-reset-confirm" },
          "Click \"Redeem anyway (force)\" again to spend a banked credit at the usage shown above. This cannot be undone."
        )
    );
  }

  function ProviderCard({ title, section, extra }) {
    if (!section || section.status !== "ok") {
      return React.createElement(
        C.Card,
        { className: "pu-card" },
        React.createElement(
          C.CardHeader,
          null,
          React.createElement(C.CardTitle, null, title)
        ),
        React.createElement(
          C.CardContent,
          null,
          React.createElement(ErrorState, {
            status: section ? section.status : "error",
            error: section ? section.error : "no data",
          })
        )
      );
    }
    return React.createElement(
      C.Card,
      { className: "pu-card" },
      React.createElement(
        C.CardHeader,
        null,
        React.createElement(
          C.CardTitle,
          null,
          title + (section.plan ? " (" + section.plan + ")" : "")
        )
      ),
      React.createElement(
        C.CardContent,
        { className: "pu-card-content" },
        section.windows.length === 0 &&
          React.createElement(
            "div",
            { className: "pu-muted" },
            "No window data reported."
          ),
        section.windows.map((w, i) =>
          React.createElement(UsageBar, {
            key: i,
            label: w.label,
            pct: w.used_percent,
            detail: w.detail,
            resetHuman: w.reset_in_human,
          })
        ),
        section.details.length > 0 &&
          React.createElement(
            "ul",
            { className: "pu-details" },
            section.details.map((d, i) => React.createElement("li", { key: i }, d))
          ),
        extra
      )
    );
  }

  function fmtUsd(v) {
    if (v == null || typeof v !== "number") return "—";
    return "$" + v.toFixed(2);
  }

  function KeysTable({ section }) {
    if (!section || section.status !== "ok") {
      return React.createElement(
        C.Card,
        { className: "pu-card pu-card-wide" },
        React.createElement(
          C.CardHeader,
          null,
          React.createElement(C.CardTitle, null, "OpenRouter — all keys")
        ),
        React.createElement(
          C.CardContent,
          null,
          React.createElement(ErrorState, {
            status: section ? section.status : "error",
            error: section ? section.error : "no data",
          })
        )
      );
    }
    return React.createElement(
      C.Card,
      { className: "pu-card pu-card-wide" },
      React.createElement(
        C.CardHeader,
        null,
        React.createElement(
          C.CardTitle,
          null,
          "OpenRouter — all keys (" + section.keys.length + ")"
        )
      ),
      React.createElement(
        C.CardContent,
        null,
        section.keys.length === 0
          ? React.createElement("div", { className: "pu-muted" }, "No keys returned.")
          : React.createElement(
              "table",
              { className: "pu-key-table" },
              React.createElement(
                "thead",
                null,
                React.createElement(
                  "tr",
                  null,
                  ["Key", "Usage", "Limit", "Remaining", "Resets", "Today", "Week", "Month"].map(
                    (h) => React.createElement("th", { key: h }, h)
                  )
                )
              ),
              React.createElement(
                "tbody",
                null,
                section.keys.map((k, i) => {
                  const hasLimit = typeof k.limit === "number" && k.limit > 0;
                  const pct =
                    hasLimit && typeof k.limit_remaining === "number"
                      ? ((k.limit - k.limit_remaining) / k.limit) * 100
                      : null;
                  return React.createElement(
                    "tr",
                    { key: i },
                    React.createElement(
                      "td",
                      { className: "pu-key-name" },
                      React.createElement("div", null, k.name || "(unnamed)"),
                      React.createElement(
                        "div",
                        { className: "pu-key-label" },
                        k.label || ""
                      )
                    ),
                    React.createElement("td", null, fmtUsd(k.usage)),
                    React.createElement(
                      "td",
                      null,
                      hasLimit ? fmtUsd(k.limit) : "unlimited"
                    ),
                    React.createElement(
                      "td",
                      { className: pct != null ? barColor(pct).replace("pu-bar-", "pu-text-") : "" },
                      hasLimit ? fmtUsd(k.limit_remaining) : "—"
                    ),
                    React.createElement("td", null, k.limit_reset || "—"),
                    React.createElement("td", null, fmtUsd(k.usage_daily)),
                    React.createElement("td", null, fmtUsd(k.usage_weekly)),
                    React.createElement("td", null, fmtUsd(k.usage_monthly))
                  );
                })
              )
            )
      )
    );
  }

  function ProviderUsagePage() {
    const [data, setData] = hooks.useState(null);
    const [loading, setLoading] = hooks.useState(true);
    const [error, setError] = hooks.useState(null);
    const [lastFetched, setLastFetched] = hooks.useState(null);

    const load = hooks.useCallback(function () {
      setLoading(true);
      setError(null);
      api("/usage")
        .then(function (res) {
          setData(res);
          setLastFetched(new Date());
        })
        .catch(function (e) {
          setError(String((e && e.message) || e));
        })
        .finally(function () {
          setLoading(false);
        });
    }, []);

    hooks.useEffect(
      function () {
        load();
      },
      [load]
    );

    return React.createElement(
      "div",
      { className: "pu-page" },
      React.createElement(
        "div",
        { className: "pu-page-head" },
        React.createElement("h2", null, "Provider usage"),
        React.createElement(
          "div",
          { className: "pu-page-head-right" },
          lastFetched &&
            React.createElement(
              "span",
              { className: "pu-muted pu-last-fetched" },
              "Updated " + lastFetched.toLocaleTimeString()
            ),
          React.createElement(
            C.Button,
            { onClick: load, disabled: loading },
            loading ? "Refreshing…" : "Refresh"
          )
        )
      ),
      error &&
        React.createElement(
          "div",
          { className: "pu-error pu-page-error" },
          "Failed to load usage: " + error
        ),
      !data && loading && React.createElement("div", { className: "pu-muted" }, "Loading…"),
      data &&
        React.createElement(
          "div",
          { className: "pu-grid" },
          React.createElement(ProviderCard, { title: "Claude (Anthropic)", section: data.anthropic }),
          React.createElement(ProviderCard, {
            title: "OpenAI Codex",
            section: data.openai_codex,
            extra:
              data.openai_codex && data.openai_codex.status === "ok"
                ? React.createElement(CodexResetButton, {
                    section: data.openai_codex,
                    onRedeemed: load,
                  })
                : null,
          }),
          React.createElement(ProviderCard, {
            title: "OpenRouter (Hermes key)",
            section: data.openrouter_key,
          })
        ),
      data && React.createElement(KeysTable, { section: data.openrouter_keys })
    );
  }

  window.__HERMES_PLUGINS__.register("provider-usage", ProviderUsagePage);
})();
