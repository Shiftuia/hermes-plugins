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
  // A backend mounted before aa346fe (dashboard not restarted yet) sends daily points as local-midnight epoch ms.
  function dailyDate(v) {
    if (typeof v === "string") return v;
    const d = new Date(v);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
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

  const CHART = { W: 900, H: 340, L: 56, R: 16, T: 12, B: 34 };
  // Every stored point is drawn in the path; per-point dots only on sparse lines, where they add information.
  const MAX_DOTS = 120;
  const NO_LINES = [];

  function chartGeometry(lines, range) {
    const W = CHART.W, H = CHART.H, L = CHART.L, R = CHART.R, T = CHART.T, B = CHART.B;
    let n = 0, minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    lines.forEach(function (l) {
      l.pts.forEach(function (p) {
        n++;
        if (p[0] < minX) minX = p[0];
        if (p[0] > maxX) maxX = p[0];
        if (p[1] < minY) minY = p[1];
        if (p[1] > maxY) maxY = p[1];
      });
    });
    if (n === 0) return null;
    // Axis spans the selected range (absolute ms), widened only if a daily point's local midnight sits just before it.
    let x0 = Math.min(range[0], minX), x1 = Math.max(range[1], maxX);
    let y0 = Math.min(0, minY), y1 = maxY;
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
    const paths = lines.map(function (l) {
      let d = "";
      for (let i = 0; i < l.pts.length; i++) d += (i ? "L" : "M") + sx(l.pts[i][0]).toFixed(1) + "," + sy(l.pts[i][1]).toFixed(1);
      return d;
    });
    return { x0: x0, x1: x1, sx: sx, sy: sy, yTicks: yTicks, xTicks: xTicks, paths: paths };
  }

  // Nearest point at or around t in a time-sorted series (binary search).
  function nearest(pts, t) {
    let lo = 0, hi = pts.length - 1;
    if (hi < 0) return null;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    return lo > 0 && t - pts[lo - 1][0] <= pts[lo][0] - t ? pts[lo - 1] : pts[lo];
  }

  function Chart({ lines, anyLines, hover, range }) {
    const W = CHART.W, H = CHART.H, L = CHART.L, R = CHART.R, T = CHART.T, B = CHART.B;
    const [cursor, setCursor] = hooks.useState(null);
    const g = hooks.useMemo(function () { return chartGeometry(lines, range); }, [lines, range[0], range[1]]);
    // The same element objects across cursor moves let React skip the whole static layer on hover.
    const staticLayer = hooks.useMemo(function () {
      if (!g) return null;
      const sx = g.sx, sy = g.sy;
      return [
        g.yTicks.map(function (v, i) {
          return h("g", { key: "y" + i },
            h("line", { x1: L, x2: W - R, y1: sy(v), y2: sy(v), className: "upa-grid" }),
            h("text", { x: L - 6, y: sy(v) + 4, textAnchor: "end", className: "upa-axis" }, fmtNum(v)));
        }),
        g.xTicks.map(function (v, i) {
          return h("text", { key: "x" + i, x: sx(v), y: H - 10, textAnchor: i === 0 ? "start" : i === 4 ? "end" : "middle",
            className: "upa-axis" }, fmtDate(v, g.x1 - g.x0));
        }),
      ];
    }, [g]);
    const seriesLayer = hooks.useMemo(function () {
      if (!g) return null;
      return lines.map(function (l, li) {
        const cls = "upa-series" + (hover ? (hover === l.id ? " upa-focus" : " upa-dim") : "");
        return h("g", { key: l.id, className: cls },
          l.pts.length > 1 && h("path", { d: g.paths[li], stroke: l.color, strokeDasharray: l.dash || null, className: "upa-line" }),
          l.pts.length <= MAX_DOTS && l.pts.map(function (p, i) {
            return h("circle", { key: i, cx: g.sx(p[0]), cy: g.sy(p[1]), r: l.pts.length > 40 ? 1.5 : 3, fill: l.color });
          }));
      });
    }, [g, lines, hover]);
    if (!g) {
      return h("div", { className: "upa-empty" },
        anyLines && lines.length === 0 ? "All lines are hidden — click a legend item to show it." : "No data points in this range.");
    }
    const sx = g.sx, sy = g.sy, x0 = g.x0, x1 = g.x1;

    // Snap to the nearest timestamp; platforms in one collector tick land seconds apart, so match within 1% of the span.
    let tip = null;
    if (cursor != null) {
      const t = x0 + ((cursor - L) / (W - L - R)) * (x1 - x0);
      let ts = null;
      lines.forEach(function (l) {
        const p = nearest(l.pts, t);
        if (p && (ts == null || Math.abs(p[0] - t) < Math.abs(ts - t))) ts = p[0];
      });
      const tol = (x1 - x0) / 100;
      tip = { ts: ts, rows: lines.map(function (l) {
        const p = nearest(l.pts, ts);
        return { line: l, p: p && Math.abs(p[0] - ts) <= tol ? p : null };
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
        staticLayer,
        tip && h("line", { x1: sx(tip.ts), x2: sx(tip.ts), y1: T, y2: H - B, className: "upa-cursor" }),
        seriesLayer,
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

  // Simple Icons 16.34.0 (CC0 1.0, simpleicons.org), 24x24 paths. LinkedIn was removed from Simple Icons
  // at LinkedIn's request, so it falls back to a text mark like any other unlisted platform.
  const ICONS = {
    youtube: ["#FF0000", "M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"],
    instagram: ["#FF0069", "M7.0301.084c-1.2768.0602-2.1487.264-2.911.5634-.7888.3075-1.4575.72-2.1228 1.3877-.6652.6677-1.075 1.3368-1.3802 2.127-.2954.7638-.4956 1.6365-.552 2.914-.0564 1.2775-.0689 1.6882-.0626 4.947.0062 3.2586.0206 3.6671.0825 4.9473.061 1.2765.264 2.1482.5635 2.9107.308.7889.72 1.4573 1.388 2.1228.6679.6655 1.3365 1.0743 2.1285 1.38.7632.295 1.6361.4961 2.9134.552 1.2773.056 1.6884.069 4.9462.0627 3.2578-.0062 3.668-.0207 4.9478-.0814 1.28-.0607 2.147-.2652 2.9098-.5633.7889-.3086 1.4578-.72 2.1228-1.3881.665-.6682 1.0745-1.3378 1.3795-2.1284.2957-.7632.4966-1.636.552-2.9124.056-1.2809.0692-1.6898.063-4.948-.0063-3.2583-.021-3.6668-.0817-4.9465-.0607-1.2797-.264-2.1487-.5633-2.9117-.3084-.7889-.72-1.4568-1.3876-2.1228C21.2982 1.33 20.628.9208 19.8378.6165 19.074.321 18.2017.1197 16.9244.0645 15.6471.0093 15.236-.005 11.977.0014 8.718.0076 8.31.0215 7.0301.0839m.1402 21.6932c-1.17-.0509-1.8053-.2453-2.2287-.408-.5606-.216-.96-.4771-1.3819-.895-.422-.4178-.6811-.8186-.9-1.378-.1644-.4234-.3624-1.058-.4171-2.228-.0595-1.2645-.072-1.6442-.079-4.848-.007-3.2037.0053-3.583.0607-4.848.05-1.169.2456-1.805.408-2.2282.216-.5613.4762-.96.895-1.3816.4188-.4217.8184-.6814 1.3783-.9003.423-.1651 1.0575-.3614 2.227-.4171 1.2655-.06 1.6447-.072 4.848-.079 3.2033-.007 3.5835.005 4.8495.0608 1.169.0508 1.8053.2445 2.228.408.5608.216.96.4754 1.3816.895.4217.4194.6816.8176.9005 1.3787.1653.4217.3617 1.056.4169 2.2263.0602 1.2655.0739 1.645.0796 4.848.0058 3.203-.0055 3.5834-.061 4.848-.051 1.17-.245 1.8055-.408 2.2294-.216.5604-.4763.96-.8954 1.3814-.419.4215-.8181.6811-1.3783.9-.4224.1649-1.0577.3617-2.2262.4174-1.2656.0595-1.6448.072-4.8493.079-3.2045.007-3.5825-.006-4.848-.0608M16.953 5.5864A1.44 1.44 0 1 0 18.39 4.144a1.44 1.44 0 0 0-1.437 1.4424M5.8385 12.012c.0067 3.4032 2.7706 6.1557 6.173 6.1493 3.4026-.0065 6.157-2.7701 6.1506-6.1733-.0065-3.4032-2.771-6.1565-6.174-6.1498-3.403.0067-6.156 2.771-6.1496 6.1738M8 12.0077a4 4 0 1 1 4.008 3.9921A3.9996 3.9996 0 0 1 8 12.0077"],
    facebook: ["#0866FF", "M9.101 23.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085 1.848-5.978 5.858-5.978.401 0 .955.042 1.468.103a8.68 8.68 0 0 1 1.141.195v3.325a8.623 8.623 0 0 0-.653-.036 26.805 26.805 0 0 0-.733-.009c-.707 0-1.259.096-1.675.309a1.686 1.686 0 0 0-.679.622c-.258.42-.374.995-.374 1.752v1.297h3.919l-.386 2.103-.287 1.564h-3.246v8.245C19.396 23.238 24 18.179 24 12.044c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.628 3.874 10.35 9.101 11.647Z"],
    threads: ["currentColor", "M18.263 11.097c-.03-3.486-1.92-5.586-5.111-5.586-2.13 0-3.922.963-4.863 2.499l2.062 1.438c.535-.843 1.272-1.543 2.628-1.543 1.528 0 2.318.85 2.544 2.431a15 15 0 0 0-2.236-.173c-4.125 0-6.068 1.867-6.068 4.336s1.943 3.99 4.804 3.99c3.139 0 5.013-2.115 5.781-4.735.798.361 1.348 1.204 1.348 2.47 0 3.387-3.907 5.232-7.22 5.232-4.885 0-8.077-3.207-8.077-8.424 0-6.392 4.223-10.487 9.9-10.487 3.808 0 5.69 1.671 6.97 3.914l2.108-1.475C21.44 2.078 18.331 0 13.663 0 6.227 0 1.168 5.277 1.168 12.934c0 7 4.953 11.066 10.856 11.066 4.878 0 9.809-2.846 9.809-7.716 0-2.545-1.46-4.231-3.569-5.187m-6.33 4.855c-1.077 0-2.026-.512-2.026-1.453 0-1.483 1.822-1.934 3.606-1.934.678 0 1.34.045 1.927.173-.422 1.927-1.671 3.215-3.508 3.214Z"],
    x: ["currentColor", "M14.234 10.162 22.977 0h-2.072l-7.591 8.824L7.251 0H.258l9.168 13.343L.258 24H2.33l8.016-9.318L16.749 24h6.993zm-2.837 3.299-.929-1.329L3.076 1.56h3.182l5.965 8.532.929 1.329 7.754 11.09h-3.182z"],
    tiktok: ["currentColor", "M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"],
    pinterest: ["#BD081C", "M12.017 0C5.396 0 .029 5.367.029 11.987c0 5.079 3.158 9.417 7.618 11.162-.105-.949-.199-2.403.041-3.439.219-.937 1.406-5.957 1.406-5.957s-.359-.72-.359-1.781c0-1.663.967-2.911 2.168-2.911 1.024 0 1.518.769 1.518 1.688 0 1.029-.653 2.567-.992 3.992-.285 1.193.6 2.165 1.775 2.165 2.128 0 3.768-2.245 3.768-5.487 0-2.861-2.063-4.869-5.008-4.869-3.41 0-5.409 2.562-5.409 5.199 0 1.033.394 2.143.889 2.741.099.12.112.225.085.345-.09.375-.293 1.199-.334 1.363-.053.225-.172.271-.401.165-1.495-.69-2.433-2.878-2.433-4.646 0-3.776 2.748-7.252 7.92-7.252 4.158 0 7.392 2.967 7.392 6.923 0 4.135-2.607 7.462-6.233 7.462-1.214 0-2.354-.629-2.758-1.379l-.749 2.848c-.269 1.045-1.004 2.352-1.498 3.146 1.123.345 2.306.535 3.55.535 6.607 0 11.985-5.365 11.985-11.987C23.97 5.39 18.592.026 11.985.026L12.017 0z"],
    bluesky: ["#1185FE", "M5.202 2.857C7.954 4.922 10.913 9.11 12 11.358c1.087-2.247 4.046-6.436 6.798-8.501C20.783 1.366 24 .213 24 3.883c0 .732-.42 6.156-.667 7.037-.856 3.061-3.978 3.842-6.755 3.37 4.854.826 6.089 3.562 3.422 6.299-5.065 5.196-7.28-1.304-7.847-2.97-.104-.305-.152-.448-.153-.327 0-.121-.05.022-.153.327-.568 1.666-2.782 8.166-7.847 2.97-2.667-2.737-1.432-5.473 3.422-6.3-2.777.473-5.899-.308-6.755-3.369C.42 10.04 0 4.615 0 3.883c0-3.67 3.217-2.517 5.202-1.026"],
    telegram: ["#26A5E4", "M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"],
    reddit: ["#FF4500", "M12 0C5.373 0 0 5.373 0 12c0 3.314 1.343 6.314 3.515 8.485l-2.286 2.286C.775 23.225 1.097 24 1.738 24H12c6.627 0 12-5.373 12-12S18.627 0 12 0Zm4.388 3.199c1.104 0 1.999.895 1.999 1.999 0 1.105-.895 2-1.999 2-.946 0-1.739-.657-1.947-1.539v.002c-1.147.162-2.032 1.15-2.032 2.341v.007c1.776.067 3.4.567 4.686 1.363.473-.363 1.064-.58 1.707-.58 1.547 0 2.802 1.254 2.802 2.802 0 1.117-.655 2.081-1.601 2.531-.088 3.256-3.637 5.876-7.997 5.876-4.361 0-7.905-2.617-7.998-5.87-.954-.447-1.614-1.415-1.614-2.538 0-1.548 1.255-2.802 2.803-2.802.645 0 1.239.218 1.712.585 1.275-.79 2.881-1.291 4.64-1.365v-.01c0-1.663 1.263-3.034 2.88-3.207.188-.911.993-1.595 1.959-1.595Zm-8.085 8.376c-.784 0-1.459.78-1.506 1.797-.047 1.016.64 1.429 1.426 1.429.786 0 1.371-.369 1.418-1.385.047-1.017-.553-1.841-1.338-1.841Zm7.406 0c-.786 0-1.385.824-1.338 1.841.047 1.017.634 1.385 1.418 1.385.785 0 1.473-.413 1.426-1.429-.046-1.017-.721-1.797-1.506-1.797Zm-3.703 4.013c-.974 0-1.907.048-2.77.135-.147.015-.241.168-.183.305.483 1.154 1.622 1.964 2.953 1.964 1.33 0 2.47-.81 2.953-1.964.057-.137-.037-.29-.184-.305-.863-.087-1.795-.135-2.769-.135Z"],
  };

  const LOCAL_TZ = "Asia/Jerusalem";
  const STATE_TIP = {
    selected: "selected", not_selected: "not selected", manual: "manual — not connected to Upload-Post",
    selected_unconnected: "selected, but not connected in Upload-Post — it will be skipped",
  };
  const RESULT_TIP = { completed: "published", failed: "failed", skipped: "skipped (not connected)",
    retryable: "failed, retry pending", queued: "queued", processing: "processing", pending: "pending" };

  function zoneAbbr(ms, tz) {
    const part = function (style) {
      const p = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: style }).formatToParts(new Date(ms))
        .find(function (x) { return x.type === "timeZoneName"; });
      return p ? p.value : "";
    };
    const short = part("short");
    if (/^[A-Z]{2,5}$/.test(short)) return short;
    const long = part("long");
    return /^[A-Za-z ]+$/.test(long) ? long.split(" ").map(function (w) { return w[0]; }).join("").toUpperCase() : short;
  }

  function fmtSlot(sec) {
    const ms = sec * 1000;
    return new Intl.DateTimeFormat("en-GB", { timeZone: LOCAL_TZ, weekday: "short", day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(ms)) + " " + zoneAbbr(ms, LOCAL_TZ);
  }

  function fmtRelative(sec, nowSec) {
    const d = sec - nowSec, a = Math.abs(d);
    const days = Math.floor(a / 86400), hrs = Math.floor((a % 86400) / 3600), mins = Math.floor((a % 3600) / 60);
    let s;
    if (a < 60) return d >= 0 ? "now" : "just now";
    if (days) s = days + " d" + (hrs ? " " + hrs + " h" : "");
    else if (hrs) s = hrs + " h" + (mins ? " " + mins + " min" : "");
    else s = mins + " min";
    return d >= 0 ? "in " + s : s + " ago";
  }

  function truncate(s, n) {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function Glyph({ platform }) {
    const icon = ICONS[platform];
    if (!icon) {
      return h("span", { className: "upa-glyph upa-glyph-text", "aria-hidden": true },
        platform === "linkedin" ? "in" : platform.slice(0, 2));
    }
    return h("svg", { className: "upa-glyph", viewBox: "0 0 24 24", "aria-hidden": true },
      h("path", { d: icon[1] }));
  }

  function PlatformIcon({ p, showResult }) {
    const r = showResult ? p.result : null;
    const tip = p.platform + ": " + STATE_TIP[p.state]
      + (r ? " · " + (RESULT_TIP[r.status] || r.status) + (r.error ? " — " + r.error : "") : "");
    const color = p.state === "selected" || p.state === "selected_unconnected" ? (ICONS[p.platform] || [""])[0] : null;
    const cls = "upa-pi upa-pi-" + p.state + (r ? " upa-res-" + r.status : "");
    const body = [h(Glyph, { key: "g", platform: p.platform }),
      r && h("span", { key: "b", className: "upa-res-badge", "aria-hidden": true },
        r.status === "completed" ? "✓" : r.status === "failed" || r.status === "retryable" ? "!" : "·")];
    const props = { className: cls, title: tip, "aria-label": tip, style: color ? { color: color } : null };
    if (r && r.post_url) {
      return h("a", Object.assign(props, { href: r.post_url, target: "_blank", rel: "noopener noreferrer",
        onClick: function (e) { e.stopPropagation(); } }), body);
    }
    return h("span", Object.assign(props, { role: "img" }), body);
  }

  function JobDetails({ job }) {
    const selected = job.platforms.filter(function (p) { return p.state === "selected" || p.state === "selected_unconnected"; });
    return h("div", { className: "upa-job-details" },
      job.cover_url && h("img", { className: "upa-job-cover", src: job.cover_url, alt: "Cover", loading: "lazy",
        referrerPolicy: "no-referrer" }),
      h("div", { className: "upa-job-captions" },
        selected.map(function (p) {
          const c = job.platform_content[p.platform] || {};
          const r = p.result;
          return h("div", { key: p.platform, className: "upa-job-caption" },
            h("div", { className: "upa-job-caption-head" },
              h(PlatformIcon, { p: p, showResult: false }),
              h("span", { className: "upa-cap" }, p.platform),
              r && r.error && h("span", { className: "upa-bad" }, r.error),
              r && r.post_url && h("a", { href: r.post_url, target: "_blank", rel: "noopener noreferrer" }, "open post ↗")),
            c.title && h("div", { className: "upa-job-title" }, c.title),
            h("div", { className: "upa-job-text" }, c.caption || (c.title ? "" : h("span", { className: "upa-muted" },
              "No per-platform text; Upload-Post uses the job title: " + (job.title || "—")))));
        }),
        h("div", { className: "upa-muted" },
          ["job " + job.job_id, job.post_type, job.source_filename, job.original_timezone && "scheduled in " + job.original_timezone,
            job.status && "status " + job.status + (job.status_message ? " — " + job.status_message : "")]
            .filter(Boolean).join(" · "))));
  }

  function JobRow({ job, nowSec, recent, link }) {
    const [open, setOpen] = hooks.useState(false);
    const label = job.external_id || truncate(job.title, 70) || job.job_id;
    const toggle = function () { setOpen(!open); };
    return h("div", { className: "upa-job" + (open ? " upa-open" : "") },
      h("div", { className: "upa-job-row", role: "button", tabIndex: 0, "aria-expanded": open, onClick: toggle,
        onKeyDown: function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } } },
        h("span", { className: "upa-job-caret", "aria-hidden": true }, open ? "▾" : "▸"),
        h("span", { className: "upa-job-time" },
          h("b", null, fmtSlot(job.scheduled_at)),
          h("span", { className: "upa-muted" }, fmtRelative(job.scheduled_at, nowSec))),
        h("span", { className: "upa-job-profile" }, job.profile),
        h("span", { className: "upa-job-label", title: job.title || "" },
          label, job.external_id && job.title && h("span", { className: "upa-muted" }, " " + truncate(job.title, 60))),
        h("span", { className: "upa-job-icons" },
          job.platforms.map(function (p) { return h(PlatformIcon, { key: p.platform, p: p, showResult: recent }); })),
        h("a", { className: "upa-job-link", href: link, target: "_blank", rel: "noopener noreferrer",
          title: recent ? "Open Upload-Post history" : "Open Upload-Post calendar (no per-job page exists)",
          onClick: function (e) { e.stopPropagation(); } }, "↗")),
      open && h(JobDetails, { job: job }));
  }

  function ScheduledSection({ q }) {
    const [nowSec, setNowSec] = hooks.useState(function () { return Math.floor(Date.now() / 1000); });
    hooks.useEffect(function () {
      const t = setInterval(function () { setNowSec(Math.floor(Date.now() / 1000)); }, 30000);
      return function () { clearInterval(t); };
    }, []);
    const d = q.data;
    let body;
    if (!d) {
      body = h("div", { className: "upa-muted" }, q.error
        ? (/404|not found/i.test(q.error) ? "The Scheduled list needs a dashboard restart to mount its backend route."
          : "Scheduled list unavailable: " + q.error)
        : "Loading…");
    } else if (d.status !== "ok") {
      body = h("div", { className: "upa-bad upa-muted" }, d.error);
    } else {
      const list = function (jobs, recent, empty) {
        return jobs.length === 0 ? h("div", { className: "upa-muted upa-job-empty" }, empty)
          : jobs.map(function (j) { return h(JobRow, { key: j.job_id, job: j, nowSec: nowSec, recent: recent,
            link: recent ? d.links.recent : d.links.upcoming }); });
      };
      body = [
        !d.collecting && h("div", { key: "c", className: "upa-muted" }, "The collector has not created the schedule tables yet; they appear after its next tick."),
        h("div", { key: "u", className: "upa-jobs" }, list(d.upcoming, false, "Nothing scheduled in Upload-Post.")),
        h("h3", { key: "rh", className: "upa-subhead" }, "Recently published", h("span", { className: "upa-muted" }, " · last 7 days")),
        h("div", { key: "r", className: "upa-jobs" }, list(d.recent, true, "No scheduled post went out in the last 7 days.")),
      ];
    }
    return h(C.Card, null,
      h(C.CardContent, { className: "upa-card" },
        h("div", { className: "upa-sched-head" },
          h("h3", { className: "upa-subhead" }, "Scheduled"),
          h("span", { className: "upa-muted" }, "Times in " + LOCAL_TZ + ". Colour = selected · grey = connected, not selected · dashed = manual (not in Upload-Post). Click a row for captions.")),
        body));
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
    const scheduled = useQuery("/scheduled");

    const lines = hooks.useMemo(function () {
      const d = series.data;
      if (!d || d.status !== "ok") return [];
      return d.lines.map(function (l) {
        const src = l.kind === "daily" ? Object.assign({}, l, {
          points: l.points.map(function (p) { const day = dailyDate(p[0]); return [dateX(day), p[1], day]; }) }) : l;
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
    // Stable identity so the chart's memoised paths survive legend hover.
    const visible = hooks.useMemo(function () {
      return lines.filter(function (l) { return !hidden.has(l.id); });
    }, [lines, hidden]);

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
          opts.reload(); health.reload(); scheduled.reload();
          if (rangeKey === "custom") series.reload(); else setNowMs(Date.now());
        }, disabled: series.loading },
          series.loading ? "Loading…" : "Refresh")),
      h(ScheduledSection, { q: scheduled }),
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
          series.data && series.data.status === "ok" && !("from_ts" in series.data) && h("div", { className: "upa-health upa-bad" },
            "The dashboard backend predates the range filter, so all data is shown — restart the dashboard to apply the range."),
          h("div", { className: "upa-chart-box" },
            h(Legend, { lines: lines, hidden: hidden, toggle: toggle, setHover: setHover }),
            h(Chart, { lines: rangeValid ? visible : NO_LINES,
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

  // A render exception would otherwise unmount the whole tab and leave a blank page.
  class CrashBoundary extends React.Component {
    constructor(props) { super(props); this.state = { error: null }; }
    static getDerivedStateFromError(error) { return { error: error }; }
    render() {
      const e = this.state.error;
      if (!e) return this.props.children;
      const self = this;
      return h("div", { className: "upa-page" },
        h("div", { className: "upa-health upa-bad upa-crash", role: "alert" },
          h("b", null, "Post analytics crashed: "), String((e && e.message) || e),
          e && e.stack && h("pre", { className: "upa-crash-stack" }, String(e.stack).split("\n").slice(0, 6).join("\n")),
          h(C.Button, { onClick: function () { self.setState({ error: null }); } }, "Retry")));
    }
  }

  window.__HERMES_PLUGINS__.register("upload-post-analytics", function UploadPostAnalytics() {
    return h(CrashBoundary, null, h(UploadPostAnalyticsPage));
  });
})();
