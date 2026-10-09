// Hover cost of the post-analytics chart in headless Chromium: 6 lines x N points, production React.
// Run: node upload-post-analytics/dashboard/bench_hover.cjs [points-per-line] [bundle.js]
// Needs hermes-agent's node_modules (esbuild, react, react-dom) and a playwright-core + Playwright Chromium
// (PLAYWRIGHT_CORE=<dir>, default ~/dev/opensource/hermes-agent/node_modules/playwright-core).
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const AGENT = process.env.HERMES_AGENT_DIR || path.join(os.homedir(), ".hermes", "hermes-agent");
const PW = process.env.PLAYWRIGHT_CORE || path.join(os.homedir(), "dev", "opensource", "hermes-agent", "node_modules", "playwright-core");
const esbuild = require(path.join(AGENT, "node_modules", "esbuild"));
const { chromium } = require(PW);

const N = +(process.argv[2] || 4000);
const BUNDLE = fs.readFileSync(process.argv[3] || path.join(__dirname, "dist", "index.js"), "utf8");
const MOVES = 200;

const vendor = esbuild.buildSync({
  // Profiling build = production React plus Profiler callbacks, which mark when each hover's commit lands.
  stdin: { contents: "window.React = require('react'); window.ReactDOMClient = require('react-dom/profiling');",
    resolveDir: AGENT },
  bundle: true, write: false, minify: true, define: { "process.env.NODE_ENV": '"production"' },
}).outputFiles[0].text;

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.setContent("<!doctype html><div id=root style='width:900px'></div>");
  await page.addScriptTag({ content: vendor });
  const res = await page.evaluate(async ({ bundle, N, MOVES }) => {
    const React = window.React, h = React.createElement;
    const now = Date.now(), step = 3 * 3600000;
    const plats = ["youtube", "instagram", "facebook", "threads", "x", "tiktok"];
    const series = { status: "ok", from_ts: 0, to_ts: 0, lines: plats.map((p, k) => ({ platform: p, metric_type: "views",
      kind: "total", points: Array.from({ length: N }, (_, i) => [now - (N - i) * step, i * (k + 1) + Math.sin(i / 7) * 50]) })) };
    const options = { status: "ok", profiles: [{ profile: "p", accounts: [],
      posts: [{ key: "c:X", title: "X", published_at: Math.floor((now - N * step) / 1000), snapshots: N }] }] };
    const health = { status: "ok", window_hours: 24, runs: { count: 0, failed: 0, requests: 0 }, last_run: null,
      rate_limit: { limit: 0, min_remaining: null, count_429: 0 }, error_count: 0, errors: [] };
    const div = (p) => h("div", { className: p.className }, p.children);
    let Page;
    window.__HERMES_PLUGINS__ = { register: (_n, C) => { Page = C; } };
    window.__HERMES_PLUGIN_SDK__ = {
      React, hooks: React,
      fetchJSON: (url) => Promise.resolve(JSON.parse(JSON.stringify(
        url.includes("/options") ? options : url.includes("/health") ? health : series))),
      components: { Card: div, CardContent: div,
        Button: (p) => h("button", { onClick: p.onClick }, p.children),
        Select: (p) => h("select", { value: p.value, onChange: p.onChange }, p.children),
        SelectOption: (p) => h("option", { value: p.value }, p.children) },
      utils: { timeAgo: () => "" },
    };
    (0, eval)(bundle);
    const commits = [];
    const root = window.ReactDOMClient.createRoot(document.getElementById("root"));
    root.render(h(React.Profiler, { id: "p", onRender: (_i, _ph, dur) => commits.push(dur) }, h(Page)));
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
    for (let i = 0; i < 100 && !document.querySelector("svg.upa-chart path"); i++) await sleep(20);
    const sel = document.querySelectorAll("select")[1];
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(sel, "c:X");
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(300);
    // Fully drawn range: switch to "Since publish".
    [...document.querySelectorAll('[aria-label="Time range"] button')].find((b) => b.textContent === "Since publish").click();
    await sleep(300);
    const svg = document.querySelector("svg.upa-chart");
    const segs = [...svg.querySelectorAll("path.upa-line")].map((p) => (p.getAttribute("d").match(/[ML]/g) || []).length);
    const dots = svg.querySelectorAll(".upa-series circle").length;
    const r = svg.getBoundingClientRect();
    // One move per frame; work = event dispatch -> React commit -> forced style+layout, frame = rAF-to-rAF interval.
    const work = [], frames = [];
    let last = await frame();
    last = performance.now();
    for (let i = 0; i < MOVES; i++) {
      const x = r.left + r.width * (0.08 + 0.9 * ((i * 37) % MOVES) / MOVES);
      const t0 = performance.now();
      const before = commits.length;
      svg.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: x, clientY: r.top + 100 }));
      while (commits.length === before) {
        if (performance.now() - t0 > 2000) throw new Error("no React commit after pointermove " + i);
        await new Promise((res) => { const c = new MessageChannel(); c.port1.onmessage = res; c.port2.postMessage(0); });
      }
      document.body.getBoundingClientRect();
      work.push(performance.now() - t0);
      await frame();
      const t = performance.now();
      frames.push(t - last);
      last = t;
    }
    const st = (a) => { const s = [...a].sort((x, y) => x - y); const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
      return { p50: +q(0.5).toFixed(2), p95: +q(0.95).toFixed(2), max: +s[s.length - 1].toFixed(2) }; };
    return { segsPerLine: segs, perPointCircles: dots, svgNodes: svg.querySelectorAll("*").length,
      hoverWorkMs: st(work), frameIntervalMs: st(frames) };
  }, { bundle: BUNDLE, N, MOVES });
  console.log(JSON.stringify(Object.assign({ chromium: browser.version(), lines: 6, pointsPerLine: N, moves: MOVES }, res), null, 1));
  await browser.close();
}
main().catch((e) => { console.error(e); process.exit(1); });
