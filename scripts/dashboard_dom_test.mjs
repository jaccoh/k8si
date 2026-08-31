// DOM-level behavioural harness for k8si/ui/dashboard.html.
//
// Driven by tests/test_dashboard_dom.py (skipped unless node + jsdom are
// installed — `npm install` at the repo root). Source-regex tests can prove a
// function exists; only this harness proves the dashboard *behaves*: rows
// render grouped, headers sort asc/desc on click, the sort chip clears,
// queued backups are counted, and message cells keep their full text in the
// title tooltip while the cell itself ellipsizes.
//
// Prints one JSON line at the end: {"pass": [...], "fail": [...]}
// Exit code 0 iff nothing failed.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { JSDOM } from "jsdom";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const uiDir = path.join(repoRoot, "k8si", "ui");
// The shell references /static assets; inline them so jsdom executes the real
// script without a resource loader.
const html = readFileSync(path.join(uiDir, "dashboard.html"), "utf8")
  .replace('<link rel="stylesheet" href="/static/app.css">',
    `<style>${readFileSync(path.join(uiDir, "static", "app.css"), "utf8")}</style>`)
  .replace('<script src="/static/app.js"></script>',
    `<script>${readFileSync(path.join(uiDir, "static", "app.js"), "utf8")}</script>`);

const LONG_MSG =
  "Timed out waiting for in-progress VolumeSnapshot(s) on PVC pvc-traefik-acme after 1800s: " +
  "snapshot k8si-traefik-acme-2026083016 never became Ready (server outage 2026-08-30)";

const FIXTURES = [
  {
    name: "zeta", namespace: "beta", pvc: "pvc-zeta", schedule: "0 2 * * *",
    paused: false, lastBackupResult: "failed",
    lastBackupTime: "2026-08-30T16:00:00+02:00", nextBackupTime: "2026-08-31T02:00:00+02:00",
    lastBackupDuration: 1812, message: LONG_MSG,
    successRate: 0.4, streak: -2, recentRuns: [], lastRunRef: "zeta-run-2",
  },
  {
    name: "alpha", namespace: "beta", pvc: "pvc-alpha", schedule: "15 2 * * *",
    paused: false, lastBackupResult: "success",
    lastBackupTime: "2026-08-30T15:00:00+02:00", nextBackupTime: "2026-08-31T02:15:00+02:00",
    lastBackupDuration: 95, message: "",
    successRate: 1, streak: 7, recentRuns: [], lastRunRef: "alpha-run-9",
  },
  {
    name: "never", namespace: "beta", pvc: "pvc-never", schedule: "30 2 * * *",
    paused: false, lastBackupResult: "pending",
    lastBackupTime: null, nextBackupTime: "2026-08-31T02:30:00+02:00",
    lastBackupDuration: null, message: "",
    successRate: null, streak: 0, recentRuns: [], lastRunRef: null,
  },
  {
    name: "gamma", namespace: "alpha-ns", pvc: "pvc-gamma", schedule: "0 3 * * *",
    paused: false, lastBackupResult: "queued",
    lastBackupTime: "2026-08-29T03:00:00+02:00", nextBackupTime: "2026-08-30T03:00:00+02:00",
    lastBackupDuration: 240, message: "",
    successRate: 0.9, streak: 3, recentRuns: [], lastRunRef: "gamma-run-4",
  },
  {
    name: "delta", namespace: "alpha-ns", pvc: "pvc-delta", schedule: "45 3 * * *",
    paused: false, lastBackupResult: "success",
    lastBackupTime: "2026-08-29T03:45:00+02:00", nextBackupTime: "2026-08-30T03:45:00+02:00",
    lastBackupDuration: 60, message: "",
    successRate: 1, streak: 5, recentRuns: [], lastRunRef: "delta-run-5",
  },
];

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "http://dashboard.test/",
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (url) => {
      const u = String(url);
      if (u === "/api/version") {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ version: "dom-test" }) });
      }
      if (u.includes("/trigger")) {
        if (window.__failNextTrigger) {
          window.__failNextTrigger = false;
          return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({ detail: "boom-409" }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ runName: "zeta-run-x" }) });
      }
      if (u === "/api/backups") {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(FIXTURES) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    };
    class FakeEventSource {
      constructor() { this.onmessage = null; this.onerror = null; this.onopen = null;
        (window.__esInstances = window.__esInstances || []).push(this); }
      close() {}
    }
    window.EventSource = FakeEventSource;
  },
});

const { window } = dom;
const { document } = window;

const results = { pass: [], fail: [] };
function check(name, fn) {
  return Promise.resolve()
    .then(() => fn())
    .then(() => results.pass.push(name))
    .catch((err) => results.fail.push(`${name}: ${err && err.message}`));
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function tableRows(tableIdx) {
  return [...document.querySelectorAll(".backup-table")].length < tableIdx + 1
    ? []
    : [...document.querySelectorAll(".backup-table")[tableIdx].querySelectorAll("tbody tr")];
}
function firstTableHeader(key) {
  return document.querySelector(`.backup-table th[data-key="${key}"]`);
}

await sleep(120); // let the initial fetch → render settle

await check("renders one table per namespace (2)", () => {
  const tables = document.querySelectorAll(".backup-table");
  assert(tables.length === 2, `expected 2 tables, got ${tables.length}`);
});

await check("message cell keeps full text in title tooltip", () => {
  const cells = [...document.querySelectorAll(".msg-cell")];
  assert(cells.length > 0, "no .msg-cell rendered");
  const zeta = cells.find((c) => (c.getAttribute("title") || "").includes("1800s"));
  assert(zeta, "the long failed message must be in a title tooltip");
  assert(zeta.textContent.includes("after 1800s"), "cell text must not be pre-truncated by JS");
});

await check("row actions are icon-only buttons in one aligned group", () => {
  const rows = [...document.querySelectorAll(".backup-table tbody tr")];
  assert(rows.length > 0, "no rows rendered");
  for (const tr of rows) {
    const tds = tr.querySelectorAll("td");
    assert(tds.length === 8, `row must have 8 cells — got ${tds.length}`);
    const group = tr.querySelector("td .actions-cell");
    assert(group, "each row needs a div.actions-cell in its last cell");
    const btns = [...group.querySelectorAll("button")];
    assert(btns.length === 3, `three icon buttons expected — got ${btns.length}`);
    const labels = btns.map((b) => b.getAttribute("aria-label") || "");
    assert(
      labels.includes("Backup now") && labels.includes("Logs"),
      `aria-labels must name the actions — got ${labels}`
    );
    for (const b of btns) {
      assert(b.querySelector("svg"), `button '${b.getAttribute("aria-label")}' needs an SVG icon`);
      assert((b.textContent || "").trim() === "", "icon buttons must carry no visible text");
      assert((b.getAttribute("title") || "").length > 0, "icon buttons need a title tooltip");
    }
  }
  const ths = [...document.querySelector(".backup-table thead").querySelectorAll("th")];
  assert(ths.length === 8, `8 headers expected per table — got ${ths.length}`);
  assert(!ths.some((th) => th.textContent.trim() === "Logs"), "no separate Logs header column");
});

await check("trigger button reflects live phase across re-renders (proof.mjs)", () => {
  // proof.mjs (2026-08-30 22:17): after triggerBackup's optimistic render()
  // the clicked button is detached; the FRESH button used to render with no
  // state and enabled — inviting a second click that 409s.
  const btn = document.querySelector('tr[data-name="zeta"] button[aria-label="Backup now"]');
  assert(btn, "no Backup now button rendered");
  window.triggerBackup("beta", "zeta", btn);
  const fresh = document.querySelector('tr[data-name="zeta"] button[aria-label="Backup now"]');
  assert(fresh && fresh !== btn, "render() must have replaced the clicked button");
  assert(fresh.disabled === true, "fresh button must be disabled while the run is queued");
  assert(fresh.className.includes("queued"), `fresh button must carry the queued class — got "${fresh.className}"`);
  assert((fresh.title || "").includes("Queued"), `tooltip must reflect the state — got "${fresh.title}"`);
});

await check("queued backup renders a queued status badge", () => {
  const badge = document.querySelector(".status.queued");
  assert(badge, "a queued CRD status must render the .status.queued badge");
});

await check("queued counts wired into sidebar badge and stat card", () => {
  assert(document.getElementById("badge-queued") !== null, "badge-queued element missing");
  assert(document.getElementById("stat-queued") !== null, "stat-queued element missing");
  assert((document.getElementById("badge-queued").textContent || "").trim() === "1",
    `badge-queued should read 1, got "${document.getElementById("badge-queued").textContent}"`);
});

await check("clicking Name header sorts ascending within every section", () => {
  const th = firstTableHeader("name");
  assert(th, "no sortable th[data-key=name]");
  th.click();
  // render() replaces the tables' innerHTML — re-query for the live header.
  const thNow = firstTableHeader("name");
  // Sections stay (alpha-ns first alphabetically); sorting applies within each.
  const names0 = tableRows(0).map((tr) => tr.dataset.name); // alpha-ns
  const names1 = tableRows(1).map((tr) => tr.dataset.name); // beta
  assert(JSON.stringify(names0) === JSON.stringify(["delta", "gamma"]),
    `alpha-ns section asc expected delta,gamma — got ${names0}`);
  assert(JSON.stringify(names1) === JSON.stringify(["alpha", "never", "zeta"]),
    `beta section asc expected alpha,never,zeta — got ${names1}`);
  assert(thNow.getAttribute("aria-sort") === "ascending", "aria-sort must be ascending");
});

await check("second click flips to descending", () => {
  firstTableHeader("name").click();
  const names1 = tableRows(1).map((tr) => tr.dataset.name);
  assert(JSON.stringify(names1) === JSON.stringify(["zeta", "never", "alpha"]),
    `beta section desc — got ${names1}`);
  assert(firstTableHeader("name").getAttribute("aria-sort") === "descending",
    "aria-sort must be descending");
});

await check("Last backup sorts newest-first by default and nulls last", () => {
  firstTableHeader("lastBackupTime").click();
  const names1 = tableRows(1).map((tr) => tr.dataset.name);
  assert(JSON.stringify(names1) === JSON.stringify(["zeta", "alpha", "never"]),
    `newest first with never last — got ${names1}`);
  const th = firstTableHeader("lastBackupTime");
  assert(th.getAttribute("aria-sort") === "descending", "time columns default to descending");
});

await check("sort chip shows the active sort and clears on click", () => {
  const chip = document.getElementById("sort-chip");
  assert(chip, "sort-chip element missing");
  assert(chip.style.display !== "none" && chip.textContent.trim() !== "",
    `chip must be visible with a label while sorting — got "${chip.textContent}"`);
  chip.click();
  const names0 = tableRows(0).map((tr) => tr.dataset.name);
  assert(JSON.stringify(names0) === JSON.stringify(["gamma", "delta"]),
    `clearSort must restore default order — got ${names0}`);
  assert(chip.textContent.trim() === "" || chip.style.display === "none",
    "chip must hide after clear");
});

await check("status sort ranks running < queued < failed < pending < success", () => {
  firstTableHeader("status").click();
  const all = [...document.querySelectorAll(".backup-table tbody tr")].map((tr) => tr.dataset.name);
  assert(JSON.stringify(all) === JSON.stringify(["gamma", "delta", "zeta", "never", "alpha"]),
    `status asc within sections — got ${all}`);
});

await check("SSE phase events drive the fresh button, not stale refs", async () => {
  // The previous check left a live trigger for zeta with an open EventSource.
  const es = window.__esInstances[window.__esInstances.length - 1];
  assert(es, "no EventSource captured");
  es.onmessage({ data: JSON.stringify({ type: "phase", phase: "Running", time: "t", message: "x" }) });
  const runningBtn = document.querySelector('tr[data-name="zeta"] button[aria-label="Backup now"]');
  assert(runningBtn.className.includes("running"), `fresh button must show running via data — got "${runningBtn.className}"`);
  assert(runningBtn.disabled, "running button must be disabled");
  es.onmessage({ data: JSON.stringify({ type: "done", result: "success" }) });
  await sleep(20);
  const doneBtn = document.querySelector('tr[data-name="zeta"] button[aria-label="Backup now"]');
  assert(!doneBtn.disabled, "after done the button must reset (enabled, no state class)");
  assert(!doneBtn.className.includes("running"), `state class must clear — got "${doneBtn.className}"`);
});

await check("failed trigger surfaces a toast", async () => {
  window.__failNextTrigger = true;
  const btn = document.querySelector('tr[data-name="delta"] button[aria-label="Backup now"]');
  window.triggerBackup("alpha-ns", "delta", btn);
  await sleep(40);
  const toast = document.getElementById("toast");
  assert(toast, "toast element missing");
  assert(toast.classList.contains("show"), `toast must be visible on failure — classes "${toast.className}"`);
  assert((toast.textContent || "").includes("boom-409"), `toast must carry the error detail — got "${toast.textContent}"`);
});

console.log(JSON.stringify(results));
process.exit(results.fail.length ? 1 : 0);
