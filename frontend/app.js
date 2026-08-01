/* ALeRCE Status Page — vanilla JS, no frameworks */

const DATA_BASE = "/data";  // CloudFront serves /data/* from the data S3 bucket
const POLL_STATUS_MS  = 30_000;
const POLL_HISTORY_MS = 60_000;
const POLL_INCIDENTS_MS = 60_000;
const DISPLAY_DAYS = 30;       // each bar = one day
const BUCKET_MINUTES = 5;      // fallback only; the real width is read off each day's row length
// Threshold coloring: a day is colored by the *fraction* of its samples in each
// state, not by the single worst sample — so one transient failed check no longer
// paints a whole day red. Exact per-day downtime/degraded time lives in the tooltip.
const OK_GREEN_FRAC = 0.995;   // ≥99.5% operational stays green (absorbs a lone 5-min blip)
const OUTAGE_RED_FRAC = 0.05;  // >5% of the day down (≈>1h) turns the bar red; less → yellow

let history  = {};
let incidents = [];

// ── Fetch helpers ────────────────────────────────────────────────────────────

async function fetchJSON(path, { bustCache = true } = {}) {
  // status/incidents bust the cache for freshness; history is cacheable
  // (served with Cache-Control: max-age=60) so we let the browser/CDN reuse it.
  const url = bustCache ? path + "?_=" + Date.now() : path;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ── Rendering ────────────────────────────────────────────────────────────────

function statusClass(s) {
  return ["operational", "degraded", "outage"].includes(s) ? s : "unknown";
}

function renderBanner(snapshot) {
  const banner = document.getElementById("banner");
  const cls = statusClass(snapshot.status);
  banner.className = "banner " + cls;
  banner.innerHTML = `<div class="dot"></div><span>${esc(snapshot.status_label)}</span>`;
}

function renderComponents(snapshot) {
  const groups = { apis: {label: "ZTF APIs", el: document.getElementById("apis-rows")},
                   apis_lsst: {label: "Multi-survey (LSST) APIs", el: document.getElementById("apis-lsst-rows")},
                   tap: {label: "Data Access (TAP)", el: document.getElementById("tap-rows")},
                   apis_other: {label: "Other APIs", el: document.getElementById("apis-other-rows")},
                   frontends: {label: "Frontends & Docs", el: document.getElementById("frontends-rows")} };

  for (const g of Object.values(groups)) {
    if (g.el) g.el.innerHTML = "";
  }

  for (const comp of snapshot.components) {
    const group = groups[comp.group];
    if (!group || !group.el) continue;
    const cls = statusClass(comp.status);
    const bar = buildUptimeBar(comp.id);

    let detailsHtml = "";
    if (comp.probe_url) {
      const meta = [];
      if (comp.http_code != null) meta.push(`HTTP ${esc(String(comp.http_code))}`);
      if (comp.response_ms != null) meta.push(`${esc(String(comp.response_ms))} ms`);
      meta.push(`Last checked: ${esc(fmtDate(comp.checked_at))}`);
      const summaryLabel = comp.status === "operational" ? "Endpoint" : "Affected endpoint";
      detailsHtml = `
        <details class="probe-details">
          <summary>${summaryLabel}</summary>
          <div class="probe-detail-content">
            <a href="${esc(comp.probe_url)}" target="_blank" rel="noopener">${esc(comp.probe_url)}</a>
            &nbsp;· ${meta.join(" · ")}
          </div>
        </details>`;
    }

    const info = comp.description
      ? `<span class="info" tabindex="0" aria-label="${esc(comp.description)}" data-tip="${esc(comp.description)}">?</span>`
      : "";

    const row = document.createElement("div");
    row.className = "component-row";
    row.innerHTML = `
      <div class="component-left">
        <div class="status-dot ${cls}"></div>
        <span class="component-name">${esc(comp.label)}</span>
        ${info}
      </div>
      <div class="uptime-bar">${bar}</div>
      <span class="status-text ${cls}">${esc(comp.status_label)}</span>
      ${detailsHtml}
    `;
    group.el.appendChild(row);
  }
}

// Build DISPLAY_DAYS daily slots, oldest first, from the prober's uptime series.
// Each slot keeps the per-state sample counts for its UTC day so we can report
// the *fraction* of the day spent operational/degraded/down (and derive a
// threshold color) instead of collapsing the day to its single worst sample.
//
// The wire format is one fixed-width string per day, one character per check slot
// ("o" operational, "d" degraded, "x" outage, "-" no check recorded), so counting
// is just tallying characters. The slot width is derived from the row's own length
// rather than assumed, which keeps days recorded at one granularity rendering
// correctly if the prober's bucket size ever changes.
const CHAR_STATUS = { o: "operational", d: "degraded", x: "outage" };
const KNOWN_STATUS = new Set(Object.values(CHAR_STATUS));

function aggregateDaily(componentId) {
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);

  const slots = [];
  for (let i = DISPLAY_DAYS - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    slots.push({ date: d.toISOString().slice(0, 10), operational: 0, degraded: 0,
                 outage: 0, bucketMinutes: BUCKET_MINUTES });
  }
  const indexByDate = Object.fromEntries(slots.map((s, i) => [s.date, i]));
  const series = history[componentId];

  if (Array.isArray(series)) {
    // Legacy per-sample format ([{ts, status}, ...]). Kept so a stale cached copy
    // of the old data file still renders instead of throwing mid-render.
    for (const b of series) {
      const idx = indexByDate[(b.ts || "").slice(0, 10)];
      if (idx !== undefined && KNOWN_STATUS.has(b.status)) slots[idx][b.status] += 1;
    }
  } else if (series && typeof series === "object") {
    for (const [day, row] of Object.entries(series)) {
      const idx = indexByDate[day];
      if (idx === undefined || typeof row !== "string" || !row.length) continue;
      slots[idx].bucketMinutes = (24 * 60) / row.length;
      for (const ch of row) {
        const status = CHAR_STATUS[ch];
        if (status) slots[idx][status] += 1;
      }
    }
  }
  return slots.map(dayStats);
}

// Derive fractions and the threshold bar color from a day's sample counts.
function dayStats(s) {
  const total = s.operational + s.degraded + s.outage;
  if (total === 0) return { ...s, total: 0, status: "unknown" };
  const outageFrac = s.outage / total;
  const opFrac = s.operational / total;
  let status;
  if (outageFrac >= OUTAGE_RED_FRAC)   status = "outage";       // sustained downtime → red
  else if (opFrac < OK_GREEN_FRAC)     status = "degraded";     // some degraded / minor outage → yellow
  else                                 status = "operational";  // fully-up, or a single absorbed blip → green
  // "Uptime" = time not fully down; degraded still counts as reachable-but-slow.
  return { ...s, total, status, upFrac: 1 - outageFrac };
}

function buildUptimeBar(componentId) {
  const days = aggregateDaily(componentId);
  const bars = days.map(d => {
    const cls = d.status === "unknown" ? "" : statusClass(d.status);
    return `<div class="bucket ${cls}" title="${esc(dayTooltip(d))}"></div>`;
  }).join("");
  const pct = uptimePct(componentId);
  return `<div class="buckets">${bars}</div>` +
         `<div class="bar-axis">` +
           `<span>${DISPLAY_DAYS} days ago</span>` +
           `<span class="pct">Uptime: ${pct}</span>` +
           `<span>Today</span>` +
         `</div>`;
}

// Per-day hover text: "12 Jul 2026 · 99.7% up · 5 min down · 30 min degraded (1 down, 6 degraded / 288 checks)"
function dayTooltip(d) {
  const date = fmtDay(d.date);
  if (d.total === 0) return `${date} · no data`;
  const parts = [`${fmtPct(d.upFrac)} up`];
  if (d.outage)   parts.push(`${fmtMins(d.outage, d.bucketMinutes)} down`);
  if (d.degraded) parts.push(`${fmtMins(d.degraded, d.bucketMinutes)} degraded`);
  const failed = [];
  if (d.outage)   failed.push(`${d.outage} down`);
  if (d.degraded) failed.push(`${d.degraded} degraded`);
  const tail = failed.length ? ` (${failed.join(", ")} / ${d.total} checks)` : "";
  return `${date} · ${parts.join(" · ")}${tail}`;
}

// Window uptime: sample-weighted fraction of time not fully down, across all
// observed samples (a 5-min blip costs 5 min out of the window, not a whole day).
function uptimePct(componentId) {
  const days = aggregateDaily(componentId);
  let notDown = 0, total = 0;
  for (const d of days) { notDown += d.operational + d.degraded; total += d.total; }
  if (total === 0) return "—";
  return fmtPct(notDown / total);
}

const TERMINAL_STATUSES = new Set(["resolved", "completed"]);

function renderIncidents() {
  const container = document.getElementById("incidents-list");
  if (!container) return;
  const now = Date.now();
  const thirtyDays = 30 * 24 * 60 * 60 * 1000;

  const visible = incidents
    .filter(i => !TERMINAL_STATUSES.has(i.status) || (now - new Date(i.started_at).getTime()) < thirtyDays)
    .sort((a, b) => {
      // Active first, then newest
      const aActive = TERMINAL_STATUSES.has(a.status) ? 0 : 1;
      const bActive = TERMINAL_STATUSES.has(b.status) ? 0 : 1;
      if (aActive !== bActive) return bActive - aActive;
      return new Date(b.started_at) - new Date(a.started_at);
    });

  if (visible.length === 0) {
    container.innerHTML = '<div class="incident"><p style="color:var(--text-muted);font-size:14px">No incidents or maintenance events in the past 30 days.</p></div>';
    return;
  }

  container.innerHTML = visible.map(i => {
    const isMaint = i.type === "maintenance";
    const date = fmtDate(i.started_at);
    const updates = (i.updates || []).slice().reverse().map(u =>
      `<div class="incident-update"><time>${esc(fmtDate(u.at))}</time> — ${esc(u.message)}</div>`
    ).join("");
    const typeTag = isMaint ? `<span class="incident-type maintenance">Maintenance</span>` : "";
    return `
      <div class="incident${isMaint ? " maintenance" : ""}">
        <div class="incident-header">
          <span class="incident-title">${typeTag}${esc(i.title)}</span>
          <span class="incident-badge ${esc(i.status)}">${esc(i.status.replace(/_/g, " "))}</span>
        </div>
        <div class="incident-date">${esc(date)}</div>
        ${updates}
      </div>`;
  }).join("");
}

function renderStaleWarning(updatedAt) {
  const el = document.getElementById("stale-warning");
  if (!el) return;
  if (!updatedAt) { el.style.display = "none"; return; }
  const ageSec = Math.floor((Date.now() - new Date(updatedAt).getTime()) / 1000);
  if (ageSec < 5 * 60) { el.style.display = "none"; return; }
  const ageMin = Math.floor(ageSec / 60);
  el.style.display = "block";
  el.className = "stale-warning " + (ageSec > 15 * 60 ? "error" : "warn");
  el.textContent = `Status data is ${ageMin} minutes old — checks may be paused.`;
}

function updateLastUpdated(updatedAt) {
  const el = document.getElementById("last-updated");
  if (!el || !updatedAt) return;
  const ageSec = Math.floor((Date.now() - new Date(updatedAt).getTime()) / 1000);
  const label = ageSec < 60 ? "just now" : `${Math.floor(ageSec / 60)} min ago`;
  el.textContent = `Last updated: ${label}`;
}

// ── Info tooltips ──────────────────────────────────────────────────────────────
// The "?" descriptions live in a floating element on <body> rather than a CSS
// tooltip inside the row, because `.card` uses overflow:hidden which would clip
// an absolutely-positioned bubble. Positioned on hover/focus, clamped to the
// viewport, and flipped below the icon when there's no room above.

function setupTooltips() {
  const tip = document.createElement("div");
  tip.id = "tooltip";
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);

  function show(el) {
    const text = el.getAttribute("data-tip");
    if (!text) return;
    tip.textContent = text;                     // set content before measuring
    const r = el.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    let left = r.left + r.width / 2 - t.width / 2;
    left = Math.min(Math.max(8, left), window.innerWidth - t.width - 8);
    let top = r.top - t.height - 8;
    if (top < 8) top = r.bottom + 8;            // flip below if no room above
    tip.style.left = left + "px";
    tip.style.top = top + "px";
    tip.classList.add("show");
  }
  function hide() { tip.classList.remove("show"); }

  // Delegated: component rows are re-rendered on every status refresh.
  document.addEventListener("mouseover", e => { const el = e.target.closest?.(".info"); if (el) show(el); });
  document.addEventListener("mouseout",  e => { if (e.target.closest?.(".info")) hide(); });
  document.addEventListener("focusin",   e => { const el = e.target.closest?.(".info"); if (el) show(el); });
  document.addEventListener("focusout",  e => { if (e.target.closest?.(".info")) hide(); });
  window.addEventListener("scroll", hide, true);
}

// ── Poll loops ───────────────────────────────────────────────────────────────

async function refreshStatus() {
  try {
    const snapshot = await fetchJSON(DATA_BASE + "/status.json");
    renderBanner(snapshot);
    renderComponents(snapshot);
    renderStaleWarning(snapshot.updated_at);
    updateLastUpdated(snapshot.updated_at);
  } catch (e) {
    console.error("Failed to fetch status.json", e);
  }
}

async function refreshHistory() {
  try {
    history = await fetchJSON(DATA_BASE + "/uptime.json", { bustCache: false });
  } catch (e) {
    console.error("Failed to fetch uptime.json", e);
  }
}

async function refreshIncidents() {
  try {
    incidents = await fetchJSON(DATA_BASE + "/incidents.json");
    renderIncidents();
  } catch (e) {
    console.error("Failed to fetch incidents.json", e);
  }
}

// ── Utils ────────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short"
  });
}

// A "YYYY-MM-DD" day key -> localized date, kept in UTC to match the bucketing.
function fmtDay(dateStr) {
  return new Date(dateStr + "T00:00:00Z").toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric", timeZone: "UTC"
  });
}

// Sample count -> human duration (each sample covers `perSample` minutes).
function fmtMins(samples, perSample = BUCKET_MINUTES) {
  const mins = samples * perSample;
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

// Fraction -> percent string; keeps extra precision near 100% so a real dip
// (e.g. 99.97%) never rounds up to a misleading "100%".
function fmtPct(frac) {
  if (frac >= 1) return "100%";
  const pct = frac * 100;
  const decimals = pct > 99.9 ? 2 : 1;
  let s = pct.toFixed(decimals);
  if (parseFloat(s) >= 100) s = (100 - Math.pow(10, -decimals)).toFixed(decimals);
  return s + "%";
}

// ── Boot ────────────────────────────────────────────────────────────────────

(async function init() {
  setupTooltips();
  // Load history first so uptime bars render on first status paint
  await refreshHistory();
  await refreshStatus();
  await refreshIncidents();

  setInterval(refreshStatus, POLL_STATUS_MS);
  setInterval(async () => { await refreshHistory(); await refreshStatus(); }, POLL_HISTORY_MS);
  setInterval(refreshIncidents, POLL_INCIDENTS_MS);
})();
