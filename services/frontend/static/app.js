// Phase 1: only proves the frontend reaches the backend through the proxy.
// TODO(phase 2): fetch telemetry/alerts and open the /api/ws/alerts WebSocket.
const statusEl = document.getElementById("api-status");

fetch("/api/health")
  .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
  .then((body) => { statusEl.textContent = body.status; statusEl.className = "ok"; })
  .catch((err) => { statusEl.textContent = `unreachable (${err})`; statusEl.className = "error"; });
