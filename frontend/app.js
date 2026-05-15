/* ALeRCE Status Page — vanilla JS, no frameworks */

const DATA_BASE = "/data";  // CloudFront serves /data/* from the data S3 bucket
const POLL_STATUS_MS  = 30_000;
const POLL_HISTORY_MS = 60_000;
const POLL_INCIDENTS_MS = 60_000;
const HISTORY_BUCKETS = 90 * 24 * 60 / 5; // 90 days of 5-min buckets (max)
const DISPLAY_BUCKETS = 60; // how many bars to show in the UI

let history  = {};
let incidents = [];

// ── Fetch helpers ────────────────────────────────────────────────────────────

async function fetchJSON(path) {
  const res = await fetch(path + "?_=" + Date.now());
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
  const groups = { apis: {label: "Public APIs", el: document.getElementById("apis-rows")},
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
    if (comp.status !== "operational" && comp.probe_url) {
      const meta = [];
      if (comp.http_code != null) meta.push(`HTTP ${esc(String(comp.http_code))}`);
      if (comp.response_ms != null) meta.push(`${esc(String(comp.response_ms))} ms`);
      meta.push(`Last checked: ${esc(fmtDate(comp.checked_at))}`);
      detailsHtml = `
        <details class="probe-details">
          <summary>Affected endpoint</summary>
          <div class="probe-detail-content">
            <a href="${esc(comp.probe_url)}" target="_blank" rel="noopener">${esc(comp.probe_url)}</a>
            &nbsp;· ${meta.join(" · ")}
          </div>
        </details>`;
    }

    const row = document.createElement("div");
    row.className = "component-row";
    row.innerHTML = `
      <div class="component-left">
        <div class="status-dot ${cls}"></div>
        <span class="component-name">${esc(comp.label)}</span>
      </div>
      <div class="uptime-bar">${bar}<span class="pct">${uptimePct(comp.id)}</span></div>
      <span class="status-text ${cls}">${esc(comp.status_label)}</span>
      ${detailsHtml}
    `;
    group.el.appendChild(row);
  }
}

function buildUptimeBar(componentId) {
  const buckets = (history[componentId] || []).slice(-DISPLAY_BUCKETS);
  // Pad left with empty buckets so bar is always full width
  const padded = Array(DISPLAY_BUCKETS - buckets.length).fill(null).concat(buckets);
  return '<div class="buckets">' +
    padded.map(b => {
      const cls = b ? statusClass(b.status) : "";
      const title = b ? `${b.ts}: ${b.status}` : "no data";
      return `<div class="bucket ${cls}" title="${esc(title)}"></div>`;
    }).join("") +
    '</div>';
}

function uptimePct(componentId) {
  const buckets = history[componentId];
  if (!buckets || buckets.length === 0) return "";
  const ok = buckets.filter(b => b.status === "operational").length;
  const pct = (ok / buckets.length * 100).toFixed(1);
  return pct + "%";
}

function renderIncidents() {
  const container = document.getElementById("incidents-list");
  if (!container) return;
  const now = Date.now();
  const thirtyDays = 30 * 24 * 60 * 60 * 1000;

  const visible = incidents
    .filter(i => i.status !== "resolved" || (now - new Date(i.started_at).getTime()) < thirtyDays)
    .sort((a, b) => {
      // Active first, then newest
      const aActive = a.status !== "resolved" ? 1 : 0;
      const bActive = b.status !== "resolved" ? 1 : 0;
      if (aActive !== bActive) return bActive - aActive;
      return new Date(b.started_at) - new Date(a.started_at);
    });

  if (visible.length === 0) {
    container.innerHTML = '<div class="incident"><p style="color:var(--text-muted);font-size:14px">No incidents or maintenance events in the past 30 days.</p></div>';
    return;
  }

  container.innerHTML = visible.map(i => {
    const date = fmtDate(i.started_at);
    const updates = (i.updates || []).slice().reverse().map(u =>
      `<div class="incident-update"><time>${esc(fmtDate(u.at))}</time> — ${esc(u.message)}</div>`
    ).join("");
    return `
      <div class="incident">
        <div class="incident-header">
          <span class="incident-title">${esc(i.title)}</span>
          <span class="incident-badge ${esc(i.status)}">${esc(i.status)}</span>
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
    history = await fetchJSON(DATA_BASE + "/history.json");
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
  // Load history first so uptime bars render on first status paint
  await refreshHistory();
  await refreshStatus();
  await refreshIncidents();

  setInterval(refreshStatus, POLL_STATUS_MS);
  setInterval(async () => { await refreshHistory(); await refreshStatus(); }, POLL_HISTORY_MS);
  setInterval(refreshIncidents, POLL_INCIDENTS_MS);
})();
