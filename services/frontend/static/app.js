"use strict";
// FactorySense SOC dashboard. Talks to the API through the proxy (same origin); the session lives in an
// HttpOnly cookie this script cannot read. Data from the API is only ever written with textContent.

const $ = (sel) => document.querySelector(sel);
const state = { user: null, alerts: new Map(), socket: null, flash: new Set(), telemetry: [] };
const COLORS = { temperature: "#ff7a59", vibration: "#4f8cff", pressure: "#2ecc8f" };

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`/api${path}`, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body && JSON.stringify(body),
  });
  const replica = res.headers.get("X-Replica");
  if (replica) $("#replica").textContent = `replica ${replica}`;
  if (res.status === 401 && path !== "/auth/login") { showLogin(); throw new Error("Session expired"); }
  if (res.status === 429) throw new Error("Too many attempts, wait a minute and retry");
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail;
    throw new Error(Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : detail || `HTTP ${res.status}`);
  }
  return data;
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
function ago(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(iso).toLocaleDateString();
}

// ------------------------------------------------------------------ session
function showLogin() {
  state.user = null;
  state.socket?.close();
  $("#app").hidden = true;
  $("#login-view").hidden = false;
}

function showApp() {
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  $("#whoami").textContent = `${state.user.username} · ${state.user.role}`;
  for (const node of document.querySelectorAll(".admin-only")) node.hidden = state.user.role !== "admin";
  $('[data-tab="alerts"]').click();
  loadSummary();
  connectLive();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    state.user = await api("/auth/login", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
    e.target.reset();
    showApp();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#logout").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

$("#tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (!tab) return;
  for (const b of $("#tabs").children) b.classList.toggle("active", b === e.target);
  for (const s of document.querySelectorAll("main > section")) s.hidden = s.id !== `tab-${tab}`;
  ({ alerts: loadAlerts, telemetry: loadTelemetry, users: loadUsers })[tab]();
});

// ------------------------------------------------------------------ alerts
const filters = () => ({ source: $("#f-source").value, severity: $("#f-severity").value, status: $("#f-status").value });
const matches = (a) => Object.entries(filters()).every(([k, v]) => !v || a[k] === v);
for (const id of ["#f-source", "#f-severity", "#f-status"]) $(id).addEventListener("change", loadAlerts);

async function loadAlerts() {
  const query = new URLSearchParams(Object.entries(filters()).filter(([, v]) => v));
  state.alerts = new Map((await api(`/alerts?${query}`)).map((a) => [a.id, a]));
  renderAlerts();
}

async function loadSummary() {
  for (const [key, value] of Object.entries(await api("/alerts/summary"))) {
    const tile = $(`#k-${key}`);
    if (tile) tile.textContent = value;
  }
}
let summaryTimer;
const refreshSummary = () => { clearTimeout(summaryTimer); summaryTimer = setTimeout(loadSummary, 800); };

function asset(a) {
  if (a.source === "ids") {
    const d = a.details || {};
    return `${d.src_ip ?? "?"} → ${d.dest_ip ?? "?"}${d.dest_port ? `:${d.dest_port}` : ""}`;
  }
  return `${a.site_id ?? "?"} / ${a.machine_id ?? "?"}`;
}

let expanded = null;
function renderAlerts() {
  const rows = [...state.alerts.values()].filter(matches).sort((a, b) => new Date(b.time) - new Date(a.time));
  $("#alerts").replaceChildren(...rows.flatMap(alertRows));
  $("#alerts-empty").hidden = rows.length > 0;
  state.flash.clear();
}

function alertRows(a) {
  const actions = el("td", { className: "actions" });
  if (a.status === "active") actions.append(alertButton("Ack", a.id, "acknowledged"));
  if (a.status !== "resolved") actions.append(alertButton("Resolve", a.id, "resolved"));
  const row = el("tr", { className: state.flash.has(a.id) ? "row flash" : "row", title: "Click for details" },
    el("td", {}, el("span", { className: `sev ${a.severity}`, textContent: a.severity })),
    el("td", {}, el("span", { className: `tag ${a.source}`, textContent: a.source })),
    el("td", { textContent: a.rule_name }),
    el("td", { textContent: asset(a) }),
    el("td", { textContent: a.count }),
    el("td", { textContent: ago(a.time), title: fmtTime(a.time) }),
    el("td", { textContent: ago(a.last_seen), title: fmtTime(a.last_seen) }),
    el("td", { className: `status-${a.status}`, textContent: a.status + (a.acknowledged_by ? ` · ${a.acknowledged_by}` : "") }),
    actions);
  row.addEventListener("click", () => { expanded = expanded === a.id ? null : a.id; renderAlerts(); });
  if (expanded !== a.id) return [row];
  const detail = { ...a.details, dedupe_key: a.dedupe_key, acknowledged_at: a.acknowledged_at, resolved_at: a.resolved_at };
  return [row, el("tr", { className: "detail" }, el("td", { colSpan: 9 }, el("pre", { textContent: JSON.stringify(detail, null, 2) })))];
}

function alertButton(label, id, status) {
  const button = el("button", { className: "small", textContent: label });
  button.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      const updated = await api(`/alerts/${id}`, { method: "PATCH", body: { status } });
      state.alerts.set(updated.id, updated);
      renderAlerts();
      loadSummary();
    } catch (err) { alert(err.message); }
  });
  return button;
}

// Open alerts are re-sent every second while their condition lasts. Re-rendering under the pointer
// would swallow clicks on Ack/Resolve, so renders wait until the button is released.
let pressing = false, pending = false;
$("#alerts").addEventListener("pointerdown", () => { pressing = true; });
document.addEventListener("pointerup", () => {
  pressing = false;
  if (pending) { pending = false; setTimeout(renderAlerts, 100); }
});
const scheduleRender = () => (pressing ? (pending = true) : renderAlerts());

// ------------------------------------------------------------------ live stream (WebSocket)
function setLive(on, replica) {
  $("#live").className = `pill ${on ? "on" : "off"}`;
  $("#live").textContent = on ? `● live · ${replica}` : "● offline";
}

function connectLive() {
  if (!state.user || state.socket) return;
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws/alerts`);
  state.socket = socket;
  socket.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "hello") return setLive(true, msg.replica);
    if (!state.alerts.has(msg.alert.id)) state.flash.add(msg.alert.id);
    state.alerts.set(msg.alert.id, msg.alert);
    scheduleRender();
    refreshSummary();
  };
  socket.onclose = () => {
    state.socket = null;
    setLive(false);
    if (state.user) setTimeout(connectLive, 3000);
  };
}

// ------------------------------------------------------------------ telemetry
async function loadTelemetry() {
  const machines = await api("/machines");
  const select = $("#machine"), current = select.value;
  select.replaceChildren(...machines.map((m) => el("option", { value: m.machine_id, textContent: `${m.site_id} / ${m.machine_id}` })));
  if (current) select.value = current;
  $("#telemetry-note").textContent = machines.length ? "" : "No telemetry received in the last 7 days.";
  const query = new URLSearchParams({ machine_id: select.value, hours: $("#range").value });
  state.telemetry = select.value ? await api(`/telemetry?${query}`) : [];
  drawCharts();
}
$("#machine").addEventListener("change", loadTelemetry);
$("#range").addEventListener("change", loadTelemetry);

function drawCharts() {
  for (const canvas of document.querySelectorAll("canvas[data-metric]")) {
    const metric = canvas.dataset.metric;
    const points = state.telemetry.filter((r) => r[metric] != null).map((r) => [new Date(r.time).getTime(), r[metric]]);
    drawChart(canvas, points, COLORS[metric]);
    $(`#v-${metric}`).textContent = points.length ? points.at(-1)[1].toFixed(1) : "–";
  }
}

function drawChart(canvas, points, color) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.font = "11px system-ui";
  ctx.fillStyle = "#8592ad";
  ctx.strokeStyle = "#24304a";
  if (points.length < 2) return ctx.fillText("No data in this range", w / 2 - 55, h / 2);

  const pad = { l: 44, r: 10, t: 10, b: 24 };
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(Math.max(...xs), x0 + 1);
  const margin = (Math.max(...ys) - Math.min(...ys)) * 0.1 || 1;
  const y0 = Math.min(...ys) - margin, y1 = Math.max(...ys) + margin;
  const X = (x) => pad.l + ((x - x0) / (x1 - x0)) * (w - pad.l - pad.r);
  const Y = (y) => h - pad.b - ((y - y0) / (y1 - y0)) * (h - pad.t - pad.b);

  for (let i = 0; i <= 4; i++) {  // horizontal grid with value labels
    const v = y0 + ((y1 - y0) * i) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, Y(v)); ctx.lineTo(w - pad.r, Y(v)); ctx.stroke();
    ctx.fillText(v.toFixed(1), 4, Y(v) + 4);
  }
  const long = x1 - x0 > 86400e3;
  for (const x of [x0, (x0 + x1) / 2, x1]) {  // start, middle and end time labels
    const d = new Date(x);
    const label = long ? d.toLocaleDateString([], { day: "2-digit", month: "short" }) : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    ctx.fillText(label, Math.min(Math.max(X(x) - 15, pad.l), w - 40), h - 6);
  }
  const fill = ctx.createLinearGradient(0, pad.t, 0, h - pad.b);
  fill.addColorStop(0, `${color}55`);
  fill.addColorStop(1, `${color}00`);
  ctx.beginPath();
  points.forEach(([x, y], i) => (i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y))));
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.lineTo(X(x1), h - pad.b);
  ctx.lineTo(X(x0), h - pad.b);
  ctx.fillStyle = fill;
  ctx.fill();
}
window.addEventListener("resize", () => { if (!$("#tab-telemetry").hidden) drawCharts(); });

// ------------------------------------------------------------------ users (admin)
async function loadUsers() {
  const users = await api("/users");
  $("#users").replaceChildren(...users.map((u) => {
    const self = u.username === state.user.username;
    const actions = el("td", { className: "actions" });
    if (!self) {
      actions.append(
        userButton(u.active ? "Disable" : "Enable", u.id, { active: !u.active }),
        userButton(u.role === "admin" ? "Make analyst" : "Make admin", u.id, { role: u.role === "admin" ? "analyst" : "admin" }));
    }
    return el("tr", {},
      el("td", { textContent: u.username + (self ? " (you)" : "") }),
      el("td", { textContent: u.role }),
      el("td", { className: u.active ? "status-resolved" : "status-active", textContent: u.active ? "active" : "disabled" }),
      el("td", { textContent: fmtTime(u.created_at) }),
      el("td", { textContent: fmtTime(u.last_login) }),
      actions);
  }));
}

function userButton(label, id, body) {
  const button = el("button", { className: "small", textContent: label });
  button.addEventListener("click", async () => {
    try { await api(`/users/${id}`, { method: "PATCH", body }); loadUsers(); } catch (err) { alert(err.message); }
  });
  return button;
}

$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const user = await api("/users", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
    $("#user-msg").textContent = `Created ${user.username}`;
    e.target.reset();
    loadUsers();
  } catch (err) { $("#user-msg").textContent = err.message; }
});

// ------------------------------------------------------------------ boot
setInterval(() => {  // keeps "x s ago" fresh and the charts moving
  if (!state.user) return;
  scheduleRender();
  if (!$("#tab-telemetry").hidden) loadTelemetry();
}, 10000);

api("/auth/me").then((user) => { state.user = user; showApp(); }).catch(showLogin);
