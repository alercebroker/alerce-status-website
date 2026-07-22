/* ALeRCE Status Page — vanilla JS, no frameworks */

const DATA_BASE = "/data";  // CloudFront serves /data/* from the data S3 bucket
const POLL_STATUS_MS  = 30_000;
const POLL_HISTORY_MS = 60_000;
const POLL_INCIDENTS_MS = 60_000;
const HISTORY_BUCKETS = 90 * 24 * 60 / 5; // 90 days of 5-min buckets (max)
const DISPLAY_DAYS = 30; // each bar = one day, colored by the worst status seen that day

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

// Aggregate raw 5-min buckets into DISPLAY_DAYS daily slots, oldest first.
// Each slot's status is the worst seen that UTC day; days with no data stay "unknown".
function aggregateDaily(componentId) {
  const rank = { unknown: 0, operational: 1, degraded: 2, outage: 3 };
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);

  const slots = [];
  for (let i = DISPLAY_DAYS - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    slots.push({ date: d.toISOString().slice(0, 10), status: "unknown" });
  }
  const indexByDate = Object.fromEntries(slots.map((s, i) => [s.date, i]));

  for (const b of history[componentId] || []) {
    const day = (b.ts || "").slice(0, 10);
    const idx = indexByDate[day];
    if (idx === undefined) continue;
    const cur = slots[idx].status;
    if ((rank[b.status] ?? 0) > rank[cur]) slots[idx].status = b.status;
  }
  return slots;
}

function buildUptimeBar(componentId) {
  const days = aggregateDaily(componentId);
  const bars = days.map(d => {
    const cls = d.status === "unknown" ? "" : statusClass(d.status);
    const title = d.status === "unknown" ? `${d.date}: no data` : `${d.date}: ${d.status}`;
    return `<div class="bucket ${cls}" title="${esc(title)}"></div>`;
  }).join("");
  const pct = uptimePct(componentId);
  return `<div class="buckets">${bars}</div>` +
         `<div class="bar-axis">` +
           `<span>${DISPLAY_DAYS} days ago</span>` +
           `<span class="pct">Uptime: ${pct}</span>` +
           `<span>Today</span>` +
         `</div>`;
}

function uptimePct(componentId) {
  const days = aggregateDaily(componentId);
  const known = days.filter(d => d.status !== "unknown");
  if (known.length === 0) return "—";
  const ok = known.filter(d => d.status === "operational").length;
  return (ok / known.length * 100).toFixed(1) + "%";
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
    history = await fetchJSON(DATA_BASE + "/history.json", { bustCache: false });
  } catch (e) {
    console.error("Failed to fetch history.json", e);
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
