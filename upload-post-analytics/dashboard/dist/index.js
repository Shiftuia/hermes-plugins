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

  const DAY = 86400;
  const PRESETS = [["1d", "1 day", DAY], ["3d", "3 days", 3 * DAY], ["1w", "1 week", 7 * DAY],
    ["1m", "1 month", 30 * DAY], ["3m", "3 months", 91 * DAY], ["6m", "6 months", 182 * DAY], ["1y", "1 year", 365 * DAY]];
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

  // en "GMT+3" says nothing; derive "IDT"/"IST" from the long name. Per date, so a DST-crossing range shows both.
  function zoneLabel(ms) {
    const part = function (style) {
      const p = new Intl.DateTimeFormat("en-US", { timeZoneName: style }).formatToParts(new Date(ms))
        .find(function (x) { return x.type === "timeZoneName"; });
      return p ? p.value : "";
    };
    const short = part("short");
    if (/^[A-Z]{2,5}$/.test(short)) return short;
    const long = part("long");
    return /^[A-Za-z ]+$/.test(long) ? long.split(" ").map(function (w) { return w[0]; }).join("").toUpperCase() : short;
  }

  // <input type="datetime-local"> value <-> epoch ms; both sides are the browser's local zone.
  function toLocalInput(ms) {
    const d = new Date(ms);
    const p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function fromLocalInput(v) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(v || "");
    return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]).getTime() : NaN;
  }

  // A daily point is a platform calendar date, not an instant: plot it at that date's local midnight, label it as the date.
  function dateX(date) {
    const m = date.split("-");
    return new Date(+m[0], m[1] - 1, +m[2]).getTime();
  }

  // daily → running sum / as-is; total → as-is / consecutive diff; window → as-is either way.
  function transform(line, mode) {
    const pts = line.points;
    if (line.kind === "window") return pts;
    if (line.kind === "daily") {
      if (mode === "delta") return pts;
      let acc = 0;
      return pts.map(function (p) { acc += p[1]; return [p[0], acc, p[2]]; });
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

  function fmtTipTime(ms) {
    return new Date(ms).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit" }) + " " + zoneLabel(ms);
  }

  function fmtPlainDate(date) {
    return new Date(dateX(date)).toLocaleDateString(undefined, { weekday: "short", year: "numeric", month: "short", day: "numeric" });
  }

  function Swatch({ line }) {
    return h("svg", { className: "upa-swatch", viewBox: "0 0 24 10", "aria-hidden": true },
      h("line", { x1: 1, x2: 23, y1: 5, y2: 5, stroke: line.color, strokeWidth: 2.5, strokeDasharray: line.dash || null }),
      h("circle", { cx: 12, cy: 5, r: 2.5, fill: line.color }));
  }

  function Legend({ lines, hidden, toggle, setHover }) {
    if (lines.length === 0) return h("div", { className: "upa-legend upa-muted" }, "No series for this selection.");
    return h("div", { className: "upa-legend", role: "group", "aria-label": "Chart legend: click an item to show or hide its line" },
      lines.map(function (l) {
        const off = hidden.has(l.id);
        return h("button", {
          key: l.id, type: "button", className: "upa-legend-item" + (off ? " upa-off" : ""), "aria-pressed": !off,
          title: (off ? "Show " : "Hide ") + l.platform + " " + l.metric,
          onClick: function () { toggle(l.id); },
          onMouseEnter: function () { if (!off) setHover(l.id); }, onMouseLeave: function () { setHover(null); },
          onFocus: function () { if (!off) setHover(l.id); }, onBlur: function () { setHover(null); },
        },
          h(Swatch, { line: l }),
          h("span", { className: "upa-legend-name" }, l.platform),
          h("span", { className: "upa-legend-metric" }, l.metric + (l.kind === "window" ? " · 30-day window" : "")),
          h("span", { className: "upa-legend-value" }, fmtNum(l.last)));
      }));
  }

  function Chart({ lines, anyLines, hover, range }) {
    const W = 900, H = 340, L = 56, R = 16, T = 12, B = 34;
    const [cursor, setCursor] = hooks.useState(null);
    const all = [];
    lines.forEach(function (l) { l.pts.forEach(function (p) { all.push(p); }); });
    if (all.length === 0) {
      return h("div", { className: "upa-empty" },
        anyLines && lines.length === 0 ? "All lines are hidden — click a legend item to show it." : "No data points in this range.");
    }
    // Axis spans the selected range (absolute ms), widened only if a daily point's local midnight sits just before it.
    let x0 = Math.min(range[0], Math.min.apply(null, all.map(function (p) { return p[0]; })));
    let x1 = Math.max(range[1], Math.max.apply(null, all.map(function (p) { return p[0]; })));
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

    // Snap to the nearest timestamp; platforms in one collector tick land seconds apart, so match within 1% of the span.
    let tip = null;
    if (cursor != null) {
      const t = x0 + ((cursor - L) / (W - L - R)) * (x1 - x0);
      let ts = all[0][0];
      all.forEach(function (p) { if (Math.abs(p[0] - t) < Math.abs(ts - t)) ts = p[0]; });
      const tol = (x1 - x0) / 100;
      tip = { ts: ts, rows: lines.map(function (l) {
        let best = null;
        l.pts.forEach(function (p) {
          if (Math.abs(p[0] - ts) <= tol && (!best || Math.abs(p[0] - ts) < Math.abs(best[0] - ts))) best = p;
        });
        return { line: l, p: best };
      }) };
    }
    const onMove = function (e) {
      const r = e.currentTarget.getBoundingClientRect();
      const x = ((e.clientX - r.left) / r.width) * W;
      setCursor(x < L || x > W - R ? null : x);
    };
    const tipLeft = tip ? (sx(tip.ts) / W) * 100 : 0;
    const tipDaily = tip && tip.rows.find(function (r) { return r.p && r.p[2]; });

    return h("div", { className: "upa-plot" },
      h("svg", { className: "upa-chart", viewBox: "0 0 " + W + " " + H, role: "img",
        onPointerMove: onMove, onPointerLeave: function () { setCursor(null); } },
        yTicks.map(function (v, i) {
          return h("g", { key: "y" + i },
            h("line", { x1: L, x2: W - R, y1: sy(v), y2: sy(v), className: "upa-grid" }),
            h("text", { x: L - 6, y: sy(v) + 4, textAnchor: "end", className: "upa-axis" }, fmtNum(v)));
        }),
        xTicks.map(function (v, i) {
          return h("text", { key: "x" + i, x: sx(v), y: H - 10, textAnchor: i === 0 ? "start" : i === 4 ? "end" : "middle",
            className: "upa-axis" }, fmtDate(v, x1 - x0));
        }),
        tip && h("line", { x1: sx(tip.ts), x2: sx(tip.ts), y1: T, y2: H - B, className: "upa-cursor" }),
        lines.map(function (l) {
          const d = l.pts.map(function (p, i) { return (i ? "L" : "M") + sx(p[0]).toFixed(1) + "," + sy(p[1]).toFixed(1); }).join("");
          const cls = "upa-series" + (hover ? (hover === l.id ? " upa-focus" : " upa-dim") : "");
          return h("g", { key: l.id, className: cls },
            l.pts.length > 1 && h("path", { d: d, stroke: l.color, strokeDasharray: l.dash || null, className: "upa-line" }),
            l.pts.map(function (p, i) {
              return h("circle", { key: i, cx: sx(p[0]), cy: sy(p[1]), r: l.pts.length > 40 ? 1.5 : 3, fill: l.color });
            }));
        }),
        tip && tip.rows.map(function (r) {
          return r.p && h("circle", { key: "c" + r.line.id, cx: sx(r.p[0]), cy: sy(r.p[1]), r: 5, fill: r.line.color,
            className: "upa-cursor-dot" });
        })),
      tip && h("div", { className: "upa-tip" + (tipLeft > 60 ? " upa-tip-left" : ""), style: { left: tipLeft + "%" } },
        h("div", { className: "upa-tip-date" }, tipDaily ? fmtPlainDate(tipDaily.p[2]) : fmtTipTime(tip.ts)),
        tip.rows.map(function (r) {
          return h("div", { key: r.line.id, className: "upa-tip-row" },
            h(Swatch, { line: r.line }),
            h("span", null, h("span", { className: "upa-cap" }, r.line.platform), " " + r.line.metric),
            h("b", null, r.p ? fmtNum(r.p[1]) : "—"));
        })));
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
    return date + " · " + p.title + (p.snapshots ? "" : " (no snapshots)");
  }

  function defaultRange(post, nowMs) {
    if (!post) return "1m";
    return post.published_at && post.published_at * 1000 > nowMs - 7 * DAY * 1000 ? "pub" : "1w";
  }

  // [fromMs, toMs) in absolute time; presets are "now minus N", not calendar-day boundaries.
  function resolveRange(key, nowMs, post, custom) {
    if (key === "custom") return [fromLocalInput(custom[0]), fromLocalInput(custom[1])];
    if (key === "pub") return [post && post.published_at ? post.published_at * 1000 : nowMs - 7 * DAY * 1000, nowMs];
    const preset = PRESETS.find(function (p) { return p[0] === key; }) || PRESETS[3];
    return [nowMs - preset[2] * 1000, nowMs];
  }

  function RangePicker({ rangeKey, setRangeKey, post, custom, setCustom, range, valid }) {
    const options = (post && post.published_at ? [["pub", "Since publish"]] : []).concat(
      PRESETS.map(function (p) { return [p[0], p[1]]; }), [["custom", "Custom…"]]);
    const z0 = zoneLabel(range[0] || Date.now()), z1 = zoneLabel(range[1] || Date.now());
    return h("div", { className: "upa-field upa-range" },
      h("span", { className: "upa-muted" }, "Range"),
      h("div", { className: "upa-range-row" },
        h("div", { className: "upa-toggle", role: "group", "aria-label": "Time range" },
          options.map(function (o) {
            return h("button", { key: o[0], type: "button", "aria-pressed": rangeKey === o[0],
              className: "upa-toggle-btn" + (rangeKey === o[0] ? " upa-active" : ""),
              onClick: function () { setRangeKey(o[0]); } }, o[1]);
          })),
        rangeKey === "custom" && h("div", { className: "upa-custom" },
          h("input", { type: "datetime-local", className: "upa-dt", "aria-label": "From (local time)", value: custom[0],
            max: custom[1] || undefined, onChange: function (e) { setCustom([e.target.value, custom[1]]); } }),
          h("span", { className: "upa-muted" }, "–"),
          h("input", { type: "datetime-local", className: "upa-dt", "aria-label": "To (local time)", value: custom[1],
            min: custom[0] || undefined, onChange: function (e) { setCustom([custom[0], e.target.value]); } })),
        h("span", { className: "upa-zone", title: TZ + " — times shown and entered in this zone" },
          z0 === z1 ? z0 : z0 + " → " + z1)),
      !valid && h("span", { className: "upa-bad upa-muted" }, "“To” must be after “From”."));
  }

  function UploadPostAnalyticsPage() {
    const opts = useQuery("/options");
    const [profile, setProfile] = hooks.useState("");
    const [target, setTarget] = hooks.useState("channel");
    const [metric, setMetric] = hooks.useState("primary");
    const [mode, setMode] = hooks.useState("cumulative");
    const [hidden, setHidden] = hooks.useState(function () { return new Set(); });
    const [hover, setHover] = hooks.useState(null);
    const toggle = function (id) {
      setHidden(function (prev) { const next = new Set(prev); if (!next.delete(id)) next.add(id); return next; });
      setHover(null);
    };

    const profiles = (opts.data && opts.data.profiles) || [];
    hooks.useEffect(function () {
      if (!profile && profiles.length) {
        const withData = profiles.find(function (p) { return p.posts.length; }) || profiles[0];
        setProfile(withData.profile);
      }
    }, [profiles.length]);
    const current = profiles.find(function (p) { return p.profile === profile; });
    const post = current && target !== "channel" ? current.posts.find(function (p) { return p.key === target; }) : null;

    const [nowMs, setNowMs] = hooks.useState(function () { return Date.now(); });
    const [rangeKey, setRangeKeyRaw] = hooks.useState("1m");
    const [custom, setCustom] = hooks.useState(["", ""]);
    const range = resolveRange(rangeKey, nowMs, post, custom);
    const rangeValid = isFinite(range[0]) && isFinite(range[1]) && range[1] > range[0];
    const setRangeKey = function (k) {
      const t = Date.now();
      if (k === "custom") {
        const r = rangeKey === "custom" ? range : resolveRange(rangeKey, t, post, custom);
        setCustom([toLocalInput(r[0]), toLocalInput(r[1])]);
      }
      setNowMs(t);
      setRangeKeyRaw(k);
    };
    hooks.useEffect(function () {
      const t = Date.now();
      setNowMs(t);
      setRangeKeyRaw(defaultRange(post, t));
    }, [profile, target, !!post]);

    const seriesPath = profile && rangeValid
      ? "/series?profile=" + encodeURIComponent(profile) + "&target=" + encodeURIComponent(target) + "&metric=" + metric
        + "&from_ts=" + Math.floor(range[0] / 1000) + "&to_ts=" + Math.ceil(range[1] / 1000) + "&tz=" + encodeURIComponent(TZ)
      : null;
    const series = useQuery(seriesPath);
    const health = useQuery("/health" + (profile ? "?profile=" + encodeURIComponent(profile) : ""));

    const lines = hooks.useMemo(function () {
      const d = series.data;
      if (!d || d.status !== "ok") return [];
      return d.lines.map(function (l) {
        const src = l.kind === "daily" ? Object.assign({}, l, {
          points: l.points.map(function (p) { return [dateX(p[0]), p[1], p[0]]; }) }) : l;
        const pts = transform(src, mode);
        return {
          id: l.platform + ":" + l.metric_type,
          platform: l.platform,
          metric: l.metric_type,
          color: COLORS[l.platform] || "#c9a227",
          dash: l.kind === "window" ? "6 4" : null,
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
        h(C.Button, { onClick: function () {
          opts.reload(); health.reload();
          if (rangeKey === "custom") series.reload(); else setNowMs(Date.now());
        }, disabled: series.loading },
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
          h(RangePicker, { rangeKey: rangeKey, setRangeKey: setRangeKey, post: post, custom: custom, setCustom: setCustom,
            range: range, valid: rangeValid }),
          series.error && h("div", { className: "upa-health upa-bad" }, series.error),
          series.data && series.data.status !== "ok" && h("div", { className: "upa-health upa-bad" }, series.data.error),
          h("div", { className: "upa-chart-box" },
            h(Legend, { lines: lines, hidden: hidden, toggle: toggle, setHover: setHover }),
            h(Chart, { lines: rangeValid ? lines.filter(function (l) { return !hidden.has(l.id); }) : [],
              anyLines: rangeValid && lines.length > 0, hover: hover, range: range })),
          h("div", { className: "upa-muted upa-note" },
            target === "channel" && metric === "primary"
              ? "Daily series from Upload-Post (last 30 days, kept by the collector beyond that), plotted at each platform's own calendar date — never shifted by time zone. Each platform reports its own metric type; they are never summed."
              : target === "channel" && metric !== "followers"
              ? "Channel likes/comments/shares are Upload-Post's rolling 30-day totals per snapshot (dashed), shown as-is."
              : "Snapshots taken by the collector; per-interval = change between consecutive snapshots."),
          (unsupported.length > 0 || disconnected.length > 0) && h("div", { className: "upa-muted upa-note" },
            disconnected.map(function (a) { return a.platform + ": " + (a.reauth_required ? "needs re-auth" : "not connected"); })
              .concat(unsupported.map(function (a) { return a.platform + ": " + a.unsupported_note; })).join(" · ")))));
  }

  window.__HERMES_PLUGINS__.register("upload-post-analytics", UploadPostAnalyticsPage);
})();
