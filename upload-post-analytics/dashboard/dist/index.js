(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const hooks = SDK.hooks;
  const C = SDK.components;
  const h = React.createElement;

  const COLORS = {
    youtube: "#ff4d4d", instagram: "#f59e0b", facebook: "#4f8cff", threads: "#b0b0b0",
    x: "#f5f5f5", tiktok: "#25f4ee", linkedin: "#0a8fd6", pinterest: "#e60023",
  };
  const METRIC_LABELS = {
    primary: "Views / reach / impressions", likes: "Likes", comments: "Comments",
    shares: "Shares", followers: "Followers",
  };

  function api(path) {
    return SDK.fetchJSON("/api/plugins/upload-post-analytics" + path);
  }

  function useQuery(path) {
    const [state, setState] = hooks.useState({ data: undefined, loading: true, error: null });
    const reqRef = hooks.useRef(0);
    const load = hooks.useCallback(function () {
      if (!path) return;
      const id = ++reqRef.current;
      setState(function (s) { return { data: s.data, loading: true, error: null }; });
      api(path)
        .then(function (res) { if (id === reqRef.current) setState({ data: res, loading: false, error: null }); })
        .catch(function (e) {
          if (id === reqRef.current) setState({ data: undefined, loading: false, error: String((e && e.message) || e) });
        });
    }, [path]);
    hooks.useEffect(function () { load(); }, [load]);
    return Object.assign({ reload: load }, state);
  }

  // Selects fire onValueChange (SDK popup) or onChange (native); wire both.
  function onSelect(setter) {
    return {
      onValueChange: function (v) { setter(v == null ? "" : v); },
      onChange: function (e) { setter(e && e.target ? e.target.value : e); },
    };
  }

  // daily → running sum / as-is; total → as-is / consecutive diff; window → as-is either way.
  function transform(line, mode) {
    const pts = line.points;
    if (line.kind === "window") return pts;
    if (line.kind === "daily") {
      if (mode === "delta") return pts;
      let acc = 0;
      return pts.map(function (p) { acc += p[1]; return [p[0], acc]; });
    }
    if (mode === "cumulative") return pts;
    const out = [];
    for (let i = 1; i < pts.length; i++) out.push([pts[i][0], pts[i][1] - pts[i - 1][1]]);
    return out;
  }

  function fmtNum(v) {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(0) + "k";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
    return String(Math.round(v * 10) / 10);
  }

  function fmtDate(ms, spanMs) {
    const d = new Date(ms);
    const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    return spanMs < 3 * 86400000 ? day + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : day;
  }

  function Chart({ lines }) {
    const W = 900, H = 340, L = 56, R = 16, T = 12, B = 34;
    const all = [];
    lines.forEach(function (l) { l.pts.forEach(function (p) { all.push(p); }); });
    if (all.length === 0) {
      return h("div", { className: "upa-empty" }, "No data points for this selection yet.");
    }
    let x0 = Math.min.apply(null, all.map(function (p) { return p[0]; }));
    let x1 = Math.max.apply(null, all.map(function (p) { return p[0]; }));
    let y0 = Math.min(0, Math.min.apply(null, all.map(function (p) { return p[1]; })));
    let y1 = Math.max.apply(null, all.map(function (p) { return p[1]; }));
    if (x1 === x0) { x0 -= 3600000; x1 += 3600000; }
    if (y1 === y0) y1 = y0 + 1;
    const raw = (y1 - y0) / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; }).find(function (s) { return s >= raw; });
    y0 = Math.floor(y0 / step) * step;
    y1 = Math.ceil(y1 / step) * step;
    const sx = function (x) { return L + ((x - x0) / (x1 - x0)) * (W - L - R); };
    const sy = function (y) { return T + (1 - (y - y0) / (y1 - y0)) * (H - T - B); };
    const yTicks = [];
    for (let v = y0; v <= y1 + step / 2; v += step) yTicks.push(v);
    const xTicks = [0, 1, 2, 3, 4].map(function (i) { return x0 + ((x1 - x0) * i) / 4; });
    return h("svg", { className: "upa-chart", viewBox: "0 0 " + W + " " + H, role: "img" },
      yTicks.map(function (v, i) {
        return h("g", { key: "y" + i },
          h("line", { x1: L, x2: W - R, y1: sy(v), y2: sy(v), className: "upa-grid" }),
          h("text", { x: L - 6, y: sy(v) + 4, textAnchor: "end", className: "upa-axis" }, fmtNum(v)));
      }),
      xTicks.map(function (v, i) {
        return h("text", { key: "x" + i, x: sx(v), y: H - 10, textAnchor: i === 0 ? "start" : i === 4 ? "end" : "middle",
          className: "upa-axis" }, fmtDate(v, x1 - x0));
      }),
      lines.map(function (l) {
        const d = l.pts.map(function (p, i) { return (i ? "L" : "M") + sx(p[0]).toFixed(1) + "," + sy(p[1]).toFixed(1); }).join("");
        return h("g", { key: l.id },
          l.pts.length > 1 && h("path", { d: d, stroke: l.color, className: "upa-line" }),
          l.pts.map(function (p, i) {
            return h("circle", { key: i, cx: sx(p[0]), cy: sy(p[1]), r: l.pts.length > 40 ? 1.5 : 3, fill: l.color },
              h("title", null, l.label + " — " + new Date(p[0]).toLocaleString() + ": " + fmtNum(p[1])));
          }));
      }));
  }

  function HealthStrip({ q }) {
    const d = q.data;
    if (!d) return h("div", { className: "upa-health" }, q.error ? "Health unavailable: " + q.error : "Loading health…");
    if (d.status !== "ok") return h("div", { className: "upa-health upa-bad" }, d.error);
    const rl = d.rate_limit;
    const lowRl = rl.limit && rl.min_remaining != null && rl.min_remaining < rl.limit * 0.2;
    const last = d.last_run;
    const stale = !last || Date.now() / 1000 - (last.finished_at || last.started_at) > 3600;
    const chip = function (bad, text) { return h("span", { className: "upa-chip " + (bad ? "upa-bad" : "upa-ok") }, text); };
    return h("div", { className: "upa-health-wrap" },
      h("div", { className: "upa-health" },
        h("span", { className: "upa-muted" }, "Last " + d.window_hours + " h:"),
        chip(d.runs.failed > 0, d.runs.count + " ticks, " + d.runs.failed + " failed"),
        chip(stale, last ? "last tick " + SDK.utils.timeAgo((last.finished_at || last.started_at) * 1000) + " (" + last.status + ")" : "no ticks yet"),
        chip(false, d.runs.requests + " requests"),
        chip(lowRl, "min remaining " + (rl.min_remaining == null ? "—" : rl.min_remaining + "/" + rl.limit)),
        chip(rl.count_429 > 0, rl.count_429 + "× 429"),
        chip(d.error_count > 0, d.error_count + " errors")),
      d.errors.length > 0 && h("table", { className: "upa-errors" },
        h("tbody", null, d.errors.map(function (e, i) {
          return h("tr", { key: i },
            h("td", null, new Date(e.ts * 1000).toLocaleString()),
            h("td", null, [e.profile, e.platform].filter(Boolean).join("/")),
            h("td", null, e.status + (e.error_code ? " " + e.error_code : "")),
            h("td", { className: "upa-err-msg", title: e.endpoint }, e.message));
        }))));
  }

  function postLabel(p) {
    const date = p.published_at ? new Date(p.published_at * 1000).toLocaleDateString() : "?";
    const plats = p.platforms.map(function (x) { return x.platform; }).join(p.matched_by_time ? " ≈ " : "+");
    const title = (p.title || "(untitled)").slice(0, 60);
    return date + " · " + plats + " · " + title + (p.snapshots ? "" : " (no snapshots)");
  }

  function UploadPostAnalyticsPage() {
    const opts = useQuery("/options");
    const [profile, setProfile] = hooks.useState("");
    const [target, setTarget] = hooks.useState("channel");
    const [metric, setMetric] = hooks.useState("primary");
    const [mode, setMode] = hooks.useState("cumulative");

    const profiles = (opts.data && opts.data.profiles) || [];
    hooks.useEffect(function () {
      if (!profile && profiles.length) {
        const withData = profiles.find(function (p) { return p.posts.length; }) || profiles[0];
        setProfile(withData.profile);
      }
    }, [profiles.length]);
    const current = profiles.find(function (p) { return p.profile === profile; });

    const seriesPath = profile
      ? "/series?profile=" + encodeURIComponent(profile) + "&target=" + encodeURIComponent(target) + "&metric=" + metric
      : null;
    const series = useQuery(seriesPath);
    const health = useQuery("/health" + (profile ? "?profile=" + encodeURIComponent(profile) : ""));

    const lines = hooks.useMemo(function () {
      const d = series.data;
      if (!d || d.status !== "ok") return [];
      return d.lines.map(function (l) {
        const pts = transform(l, mode);
        return {
          id: l.platform + ":" + l.metric_type,
          label: l.platform + " · " + l.metric_type + (l.kind === "window" ? " (30-day window)" : ""),
          color: COLORS[l.platform] || "#c9a227",
          pts: pts,
          last: pts.length ? pts[pts.length - 1][1] : null,
          kind: l.kind,
        };
      });
    }, [series.data, mode]);

    if (opts.data && opts.data.status !== "ok") {
      return h("div", { className: "upa-page" }, h("div", { className: "upa-health upa-bad" }, opts.data.error));
    }

    const field = function (label, value, setter, options) {
      return h("label", { className: "upa-field" },
        h("span", { className: "upa-muted" }, label),
        h(C.Select, Object.assign({ value: value, className: "h-8" }, onSelect(setter)),
          options.map(function (o) { return h(C.SelectOption, { key: o[0], value: o[0] }, o[1]); })));
    };

    const unsupported = current ? current.accounts.filter(function (a) { return a.unsupported_note; }) : [];
    const disconnected = current ? current.accounts.filter(function (a) { return !a.connected || a.reauth_required; }) : [];
    const windowOnly = lines.length > 0 && lines.every(function (l) { return l.kind === "window"; });

    return h("div", { className: "upa-page" },
      h("div", { className: "upa-head" },
        h("h2", null, "Upload-Post analytics"),
        h(C.Button, { onClick: function () { opts.reload(); series.reload(); health.reload(); }, disabled: series.loading },
          series.loading ? "Loading…" : "Refresh")),
      h(HealthStrip, { q: health }),
      h(C.Card, null,
        h(C.CardContent, { className: "upa-card" },
          h("div", { className: "upa-controls" },
            field("Profile", profile, function (v) { setProfile(v); setTarget("channel"); },
              profiles.map(function (p) { return [p.profile, p.profile]; })),
            field("Show", target, setTarget,
              [["channel", "Channel overall"]].concat(current ? current.posts.map(function (p) { return [p.key, postLabel(p)]; }) : [])),
            field("Metric", metric, setMetric, Object.keys(METRIC_LABELS).map(function (k) { return [k, METRIC_LABELS[k]]; })),
            h("div", { className: "upa-field" },
              h("span", { className: "upa-muted" }, "Mode"),
              h("div", { className: "upa-toggle" },
                ["cumulative", "delta"].map(function (m) {
                  return h("button", { key: m, type: "button", disabled: windowOnly,
                    className: "upa-toggle-btn" + (mode === m ? " upa-active" : ""), onClick: function () { setMode(m); } },
                    m === "cumulative" ? "Cumulative" : "Per interval");
                })))),
          series.error && h("div", { className: "upa-health upa-bad" }, series.error),
          series.data && series.data.status !== "ok" && h("div", { className: "upa-health upa-bad" }, series.data.error),
          h(Chart, { lines: lines }),
          h("div", { className: "upa-legend" }, lines.map(function (l) {
            return h("span", { key: l.id, className: "upa-legend-item" },
              h("span", { className: "upa-swatch", style: { background: l.color } }),
              l.label + " — latest " + fmtNum(l.last));
          })),
          h("div", { className: "upa-muted upa-note" },
            target === "channel" && metric === "primary"
              ? "Daily series from Upload-Post (last 30 days, kept by the collector beyond that). Each platform reports its own metric type; they are never summed."
              : target === "channel" && metric !== "followers"
              ? "Channel likes/comments/shares are Upload-Post's rolling 30-day totals per snapshot, shown as-is."
              : "Snapshots taken by the collector; per-interval = change between consecutive snapshots."),
          (unsupported.length > 0 || disconnected.length > 0) && h("div", { className: "upa-muted upa-note" },
            disconnected.map(function (a) { return a.platform + ": " + (a.reauth_required ? "needs re-auth" : "not connected"); })
              .concat(unsupported.map(function (a) { return a.platform + ": " + a.unsupported_note; })).join(" · ")))));
  }

  window.__HERMES_PLUGINS__.register("upload-post-analytics", UploadPostAnalyticsPage);
})();
