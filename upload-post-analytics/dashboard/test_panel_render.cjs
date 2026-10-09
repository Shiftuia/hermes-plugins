// Renders dist/index.js in jsdom against stubbed SDK shims and drives every range control.
// Run: node --test upload-post-analytics/dashboard/test_panel_render.cjs (needs hermes-agent's node_modules).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const AGENT = process.env.HERMES_AGENT_DIR || path.join(os.homedir(), ".hermes", "hermes-agent");
const req = (m) => require(path.join(AGENT, "node_modules", m));
const { JSDOM } = req("jsdom");
const React = req("react");

function useWindow(w) {
  for (const k of ["window", "document", "navigator", "HTMLElement", "Node", "Event", "MouseEvent"]) {
    Object.defineProperty(globalThis, k, { value: k === "window" ? w : w[k], configurable: true, writable: true });
  }
}
// react-dom probes `window` once at require time; without it the input-event plugin stays off and onChange never fires.
useWindow(new JSDOM("").window);
const ReactDOMClient = req("react-dom/client");
const BUNDLE = fs.readFileSync(path.join(__dirname, "dist", "index.js"), "utf8");

const NOW = Date.now();
const DAY_MS = 86400000;
const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);
const POST = { key: "c:HS-2026-028", title: "HS-2026-028 — free models on OpenRouter (Short)",
  published_at: Math.floor((NOW - 3 * DAY_MS) / 1000), snapshots: 6, joined_by: "content_id",
  platforms: [{ platform: "instagram", url: "https://i/1" }, { platform: "youtube", url: "https://y/1" }] };
const OPTIONS = { status: "ok", metrics: ["primary"], profiles: [{ profile: "holyshifted", posts: [POST],
  accounts: [{ platform: "youtube", connected: true, reauth_required: false, handle: "@h", unsupported_note: null }] }] };
const HEALTH = { status: "ok", window_hours: 24, runs: { count: 3, failed: 0, requests: 9 },
  last_run: { started_at: NOW / 1000 - 60, finished_at: NOW / 1000 - 50, status: "ok" },
  rate_limit: { limit: 64, min_remaining: 40, count_429: 0 }, error_count: 0, errors: [] };
const snaps = (n) => Array.from({ length: n }, (_, i) => [NOW - (n - i) * 6 * 3600000, 100 + i * 10]);

// Current backend: daily points are "YYYY-MM-DD" strings and the range is echoed back.
function seriesNew(q) {
  const lines = q.get("target") === "channel"
    ? [{ platform: "youtube", metric_type: "views", kind: "daily",
      points: Array.from({ length: 10 }, (_, i) => [isoDay(NOW - (9 - i) * DAY_MS), i * 3]) }]
    : [{ platform: "instagram", metric_type: "reach", kind: "total", points: snaps(4) },
      { platform: "youtube", metric_type: "views", kind: "total", points: snaps(6) }];
  return { status: "ok", profile: q.get("profile"), target: q.get("target"), metric: q.get("metric"),
    from_ts: +q.get("from_ts"), to_ts: +q.get("to_ts"), tz: q.get("tz"), lines };
}
// Pre-aa346fe backend (still mounted until the dashboard restarts): epoch-ms daily points, no range echo.
function seriesOld(q) {
  const d = seriesNew(q);
  return { status: "ok", profile: d.profile, target: d.target, metric: d.metric,
    lines: d.lines.map((l) => l.kind !== "daily" ? l
      : Object.assign({}, l, { points: l.points.map((p) => [new Date(p[0] + "T00:00:00").getTime(), p[1]]) })) };
}

async function mount({ series = seriesNew, brokenCard = false } = {}) {
  const dom = new JSDOM("<!doctype html><div id=root></div>", { runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  useWindow(w);
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const h = React.createElement;
  const div = (p) => h("div", { className: p.className }, p.children);
  const errors = [];
  let Page = null;
  w.__HERMES_PLUGINS__ = { register: (_n, C) => { Page = C; } };
  w.__HERMES_PLUGIN_SDK__ = {
    React,
    hooks: { useState: React.useState, useEffect: React.useEffect, useCallback: React.useCallback,
      useMemo: React.useMemo, useRef: React.useRef },
    fetchJSON: (url) => {
      const u = new URL(url, "http://x");
      const route = u.pathname.replace("/api/plugins/upload-post-analytics", "");
      const body = route === "/options" ? OPTIONS : route === "/health" ? HEALTH : route === "/series" ? series(u.searchParams) : null;
      return body ? Promise.resolve(JSON.parse(JSON.stringify(body))) : Promise.reject(new Error("404 " + route));
    },
    components: {
      Card: brokenCard ? () => { throw new Error("card exploded"); } : div, CardContent: div,
      Button: (p) => h("button", { type: "button", onClick: p.onClick, disabled: p.disabled }, p.children),
      Select: (p) => h("select", { value: p.value, onChange: p.onChange, className: p.className }, p.children),
      SelectOption: (p) => h("option", { value: p.value }, p.children),
    },
    utils: { timeAgo: () => "just now" },
  };
  w.eval(BUNDLE);
  assert.ok(Page, "plugin did not register");
  const container = w.document.getElementById("root");
  const root = ReactDOMClient.createRoot(container, {
    onUncaughtError: (e) => errors.push(e), onCaughtError: (e) => errors.push(e),
  });
  const act = (fn) => React.act(async () => { await fn(); await new Promise((r) => setTimeout(r, 0)); });
  await act(() => root.render(h(Page)));
  await act(() => {});
  const $ = (sel) => container.querySelector(sel);
  const $$ = (sel) => [...container.querySelectorAll(sel)];
  const setValue = async (el, v, evt) => {
    const proto = el.tagName === "SELECT" ? w.HTMLSelectElement.prototype : w.HTMLInputElement.prototype;
    await act(() => {
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
      el.dispatchEvent(new w.Event(evt, { bubbles: true }));
    });
  };
  const click = (el) => act(() => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true })));
  const hover = async (clientX = 600) => {
    const svg = $("svg.upa-chart");
    if (!svg) return;
    svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 900, height: 340 });
    await act(() => svg.dispatchEvent(new w.MouseEvent("pointermove", { bubbles: true, clientX })));
  };
  const alive = (what) => {
    assert.deepEqual(errors.map((e) => String(e && e.stack || e)), [], what + ": render errors");
    assert.ok($(".upa-page"), what + ": page unmounted");
    assert.ok($("svg.upa-chart") || $(".upa-empty"), what + ": no chart area");
  };
  const rangeButtons = () => $$('[aria-label="Time range"] button');
  async function everyRange(what) {
    const labels = rangeButtons().map((b) => b.textContent);
    assert.ok(labels.includes("Custom…") && labels.includes("1 year"), what + ": presets missing");
    for (const label of labels) {
      await click(rangeButtons().find((b) => b.textContent === label));
      await act(() => {});
      await hover();
      alive(what + " / " + label);
    }
    const [from, to] = $$("input.upa-dt");
    assert.ok(from && to, what + ": custom inputs missing");
    await setValue(from, "", "input");
    alive(what + " / custom cleared");
    await setValue(from, "2026-03-01T00:00", "input");
    await setValue(to, "2026-12-01T00:00", "input");
    await hover();
    alive(what + " / custom DST-crossing");
    await setValue(to, "2026-01-01T00:00", "input");
    alive(what + " / custom inverted");
    assert.ok(container.textContent.includes("must be after"), what + ": inverted range not flagged");
  }
  return { container, errors, $, $$, setValue, act, click, hover, alive, everyRange, unmount: () => act(() => root.unmount()) };
}

for (const [name, series] of [["current backend", seriesNew], ["pre-range backend (not restarted)", seriesOld]]) {
  test("channel and post stay rendered across every range: " + name, async () => {
    const p = await mount({ series });
    p.alive("initial");
    await p.everyRange("channel");
    await p.setValue(p.$$("select")[1], POST.key, "change");
    p.alive("post selected");
    assert.ok(p.$$('[aria-label="Time range"] button').some((b) => b.textContent === "Since publish"));
    await p.everyRange("post");
    await p.unmount();
  });
}

test("old backend response is flagged instead of silently ignoring the range", async () => {
  const p = await mount({ series: seriesOld });
  assert.match(p.container.textContent, /restart the dashboard/i);
  await p.unmount();
});

test("every point is in the path; per-point dots only on lines with <= 120 points", async () => {
  const dense = (q) => Object.assign(seriesNew(q), { lines: [
    { platform: "instagram", metric_type: "reach", kind: "total", points: snaps(20) },
    { platform: "youtube", metric_type: "views", kind: "total", points: snaps(121) }] });
  const p = await mount({ series: dense });
  await p.setValue(p.$$("select")[1], POST.key, "change");
  const pick = (label) => p.click(p.$$('[aria-label="Time range"] button').find((b) => b.textContent === label));
  await pick("1 year");
  p.alive("dense");
  const groups = p.$$("g.upa-series");
  assert.equal(groups.length, 2);
  const segs = groups.map((g) => (g.querySelector("path").getAttribute("d").match(/[ML]/g) || []).length);
  assert.deepEqual(segs, [20, 121]);
  assert.deepEqual(groups.map((g) => g.querySelectorAll("circle").length), [20, 0]);
  await p.hover(880);
  assert.equal(p.$$("circle.upa-cursor-dot").length, 2, "hover marker per visible line");
  await p.unmount();
});

test("render exception shows the error text instead of a blank page", async () => {
  const p = await mount({ brokenCard: true });
  const box = p.$(".upa-crash");
  assert.ok(box, "error boundary did not render");
  assert.match(box.textContent, /card exploded/);
  await p.unmount();
});
