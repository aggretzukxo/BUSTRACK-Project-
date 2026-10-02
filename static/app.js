const $ = sel => document.querySelector(sel);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = localStorage.getItem("bt_token");
let map, gmapsLoaded = false;
let routeLine = null, endpointMarkers = [], segmentStopMarkers = [];
let stops = [], buses = {}, busMarkers = {};
let socket = null, wsRetry = null, pollTimer = null;
let lastQuery = null, currentPlan = null, selectedBusId = null;
let authMode = "login";
const STOP_MIN_ZOOM = 13;          // below this zoom, no stop dots are drawn
const stopMarkerPool = new Map();  // stop id -> marker, created once and reused
let stopIdleListener = null, stopIcon = null;

/* ---------------------------------------------------------------- API */
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401 && token) {
    signOut(false);
    throw new Error("Your session expired. Please log in again.");
  }
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : "Please check the details you entered.");
  }
  return data;
}

/* --------------------------------------------------------------- Auth */
function showAuth() {
  $("#app-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
}

function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.mode === mode));
  $("#name-row").classList.toggle("hidden", mode !== "signup");
  $("#pw-hint").classList.toggle("hidden", mode !== "signup");
  $("#forgot-row").classList.toggle("hidden", mode === "signup");
  $("#password").autocomplete = mode === "signup" ? "new-password" : "current-password";
  $("#auth-submit").textContent = mode === "signup" ? "Create account" : "Log in";
  setAuthError("");
}

function setAuthError(text) {
  const el = $("#auth-error");
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => setAuthMode(t.dataset.mode)));

$("#auth-form").addEventListener("submit", async e => {
  e.preventDefault();
  setAuthError("");
  const email = $("#email").value.trim();
  const password = $("#password").value;
  const name = $("#name").value.trim();
  if (!email || !password || (authMode === "signup" && !name)) {
    return setAuthError("Please fill in all the fields.");
  }
  const btn = $("#auth-submit");
  btn.disabled = true;
  try {
    const payload = authMode === "signup" ? { name, email, password } : { email, password };
    const data = await api(authMode === "signup" ? "/api/register" : "/api/login", { method: "POST", body: payload });
    token = data.token;
    localStorage.setItem("bt_token", token);
    $("#password").value = "";
    await startApp(data.user);
  } catch (err) {
    setAuthError(err.message === "Failed to fetch" ? "Can't reach the server. Is it running?" : err.message);
  } finally {
    btn.disabled = false;
  }
});

/* ------------------------------------------------ Forgot / reset password */
let resetToken = null;

function showPanel(name) {
  ["login", "forgot", "reset"].forEach(p => $("#panel-" + p).classList.toggle("hidden", p !== name));
  ["#auth-error", "#forgot-error", "#reset-error"].forEach(id => $(id).classList.add("hidden"));
}

function setInfo(text) {
  const el = $("#auth-info");
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

function fieldError(sel, text) {
  const el = $(sel);
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

$("#forgot-link").addEventListener("click", () => {
  setInfo("");
  $("#forgot-email").value = $("#email").value.trim();
  showPanel("forgot");
});
document.querySelectorAll(".back-to-login").forEach(b => b.addEventListener("click", () => {
  setInfo(""); resetToken = null; showPanel("login");
}));

$("#forgot-form").addEventListener("submit", async e => {
  e.preventDefault();
  const email = $("#forgot-email").value.trim();
  if (!email) return fieldError("#forgot-error", "Please enter your email.");
  const btn = $("#forgot-submit");
  btn.disabled = true;
  try {
    const data = await api("/api/forgot-password", { method: "POST", body: { email } });
    fieldError("#forgot-error", "");
    showPanel("login");
    setInfo(data.message);
  } catch (err) {
    fieldError("#forgot-error", err.message === "Failed to fetch" ? "Can't reach the server. Is it running?" : err.message);
  } finally {
    btn.disabled = false;
  }
});

$("#reset-form").addEventListener("submit", async e => {
  e.preventDefault();
  const p1 = $("#new-password").value, p2 = $("#new-password2").value;
  if (p1.length < 8) return fieldError("#reset-error", "Password must be at least 8 characters.");
  if (p1 !== p2) return fieldError("#reset-error", "The two passwords don't match.");
  const btn = $("#reset-submit");
  btn.disabled = true;
  try {
    await api("/api/reset-password", { method: "POST", body: { token: resetToken, password: p1 } });
    resetToken = null;
    $("#new-password").value = ""; $("#new-password2").value = "";
    showPanel("login");
    setAuthMode("login");
    setInfo("Password updated. Please log in with your new password.");
  } catch (err) {
    fieldError("#reset-error", err.message === "Failed to fetch" ? "Can't reach the server. Is it running?" : err.message);
  } finally {
    btn.disabled = false;
  }
});

async function signOut(callApi = true) {
  if (callApi && token) { try { await api("/api/logout", { method: "POST" }); } catch (_) {} }
  token = null;
  localStorage.removeItem("bt_token");
  closeSocket();
  stopPolling();
  clearTrip();
  Object.values(busMarkers).forEach(m => m.setMap(null));
  busMarkers = {}; buses = {};
  stopMarkerPool.forEach(m => m.setMap(null));
  showAuth();
}
$("#logout").addEventListener("click", () => signOut(true));

/* ---------------------------------------------------------------- Map */
function loadGoogleMaps() {
  return new Promise((resolve, reject) => {
    if (window.google && window.google.maps) return resolve();
    const key = window.__BUSTRACK_MAPS_KEY;
    if (!key) return reject(new Error("No Google Maps key configured (see .env)."));
    window.__bustrackMapsReady = resolve;
    const s = document.createElement("script");
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&callback=__bustrackMapsReady&loading=async`;
    s.async = true;
    s.onerror = () => reject(new Error("Google Maps failed to load. Check the key, billing, and enabled APIs."));
    document.head.appendChild(s);
  });
}

function initMap() {
  if (map) return;
  // Kolkata metro area; some real WBTC routes run far out (Digha, Bardhaman etc.),
  // those are reachable by scrolling/zooming out or by searching a trip.
  map = new google.maps.Map(document.getElementById("map"), {
    center: { lat: 22.58, lng: 88.38 },
    zoom: 11,
    // A Map ID switches on Google's newer vector renderer, which is far smoother to
    // zoom/pan than the default raster tiles. Falls back to the old renderer if unset.
    mapId: window.__BUSTRACK_MAP_ID || undefined,
  });
}

/* Stop dots: only the stops inside the current view, and only when zoomed in.
   Runs on the map's 'idle' event (after a pan/zoom settles), so gestures stay smooth. */
function refreshVisibleStops() {
  if (!map) return;
  const bounds = map.getBounds();
  const zoomedIn = map.getZoom() >= STOP_MIN_ZOOM;
  stopIcon = stopIcon || {
    path: google.maps.SymbolPath.CIRCLE, scale: 3, fillColor: "#64748b",
    fillOpacity: 0.85, strokeColor: "#fff", strokeWeight: 1,
  };
  for (const s of stops) {
    let m = stopMarkerPool.get(s.id);
    const visible = zoomedIn && bounds && bounds.contains({ lat: s.lat, lng: s.lng });
    if (visible) {
      if (!m) {
        m = new google.maps.Marker({ position: { lat: s.lat, lng: s.lng }, title: s.name, icon: stopIcon });
        stopMarkerPool.set(s.id, m);
      }
      if (!m.getMap()) m.setMap(map);
    } else if (m && m.getMap()) {
      m.setMap(null);
    }
  }
}

function initStopLayer() {
  if (!map) return;
  if (!stopIdleListener) stopIdleListener = map.addListener("idle", refreshVisibleStops);
  refreshVisibleStops();
}

function busIcon(bus) {
  const selected = bus.id === selectedBusId;
  return {
    path: google.maps.SymbolPath.CIRCLE,
    scale: selected ? 11 : 8,
    fillColor: selected ? "#0f766e" : "#334155",
    fillOpacity: 1,
    strokeColor: "#fff",
    strokeWeight: 2,
  };
}

function updateBus(bus) {
  buses[bus.id] = bus;
  const pos = { lat: bus.lat, lng: bus.lng };
  if (!busMarkers[bus.id]) {
    busMarkers[bus.id] = new google.maps.Marker({
      position: pos, map, icon: busIcon(bus),
      label: { text: bus.number, color: "#fff", fontSize: "10px", fontWeight: "700" },
      title: `Bus ${bus.number} · ${bus.status === "ON_TIME" ? "On time" : "Delayed"}`,
    });
  } else {
    busMarkers[bus.id].setPosition(pos);
  }
}

// Only the buses relevant to the trip currently on screen - keeps marker count (and
// therefore zoom/pan smoothness) independent of how many buses exist system-wide.
function relevantBusIds() {
  return currentPlan && currentPlan.options ? new Set(currentPlan.options.map(o => o.bus_id)) : new Set();
}

function pruneBusMarkers(keepIds) {
  for (const id of Object.keys(busMarkers)) {
    if (!keepIds.has(id)) { busMarkers[id].setMap(null); delete busMarkers[id]; delete buses[id]; }
  }
}

function refreshBusIcons() {
  Object.values(buses).forEach(b => busMarkers[b.id] && busMarkers[b.id].setIcon(busIcon(b)));
}

/* ---------------------------------------------------------- Live socket */
function setLive(on) {
  const el = $("#live");
  el.textContent = on ? "● Live" : "● Reconnecting…";
  el.className = "live " + (on ? "on" : "off");
}

function closeSocket() {
  clearTimeout(wsRetry);
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  setLive(false);
  $("#live").textContent = "● Offline";
}

function connectSocket() {
  if (!token) return;
  closeSocket();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.onopen = () => setLive(true);
  socket.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === "positions") {
      const keep = relevantBusIds();
      msg.buses.forEach(b => { if (keep.has(b.id)) updateBus(b); });
      pruneBusMarkers(keep);
    }
  };
  socket.onclose = e => {
    setLive(false);
    if (e.code === 4401) return signOut(false);
    if (token) wsRetry = setTimeout(connectSocket, 3000);
  };
}

/* ---------------------------------------------------------- Trip planner */
function showMessage(text, kind = "info") {
  const el = $("#message");
  el.textContent = text;
  el.className = "msg " + kind + (text ? "" : " hidden");
}

function fillStopSelects() {
  const sorted = [...stops].sort((a, b) => a.name.localeCompare(b.name));
  const opts = '<option value="">Choose a stop</option>' +
    sorted.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  $("#from").innerHTML = opts;
  $("#to").innerHTML = opts;
}

$("#swap").addEventListener("click", () => {
  const a = $("#from").value;
  $("#from").value = $("#to").value;
  $("#to").value = a;
});

function distKm(lat1, lng1, lat2, lng2) {
  const r = Math.PI / 180;
  const h = Math.sin((lat2 - lat1) * r / 2) ** 2 +
    Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lng2 - lng1) * r / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

$("#locate").addEventListener("click", () => {
  if (!navigator.geolocation) return showMessage("Your browser doesn't support location.", "error");
  navigator.geolocation.getCurrentPosition(pos => {
    let best = null;
    stops.forEach(s => {
      const d = distKm(pos.coords.latitude, pos.coords.longitude, s.lat, s.lng);
      if (!best || d < best.d) best = { s, d };
    });
    if (!best) return showMessage("No stops loaded yet.", "error");
    $("#from").value = best.s.id;
    showMessage(best.d > 5
      ? `Your nearest stop is ${best.s.name}, ${best.d.toFixed(1)} km away.`
      : `Nearest stop: ${best.s.name}`, "info");
  }, () => showMessage("Couldn't get your location. Please choose a stop.", "error"), { timeout: 8000 });
});

$("#trip-form").addEventListener("submit", async e => {
  e.preventDefault();
  const origin = $("#from").value, destination = $("#to").value;
  if (!origin || !destination) return showMessage("Choose both a starting point and a destination.", "error");
  if (origin === destination) return showMessage("Starting point and destination must be different.", "error");
  lastQuery = { origin, destination };
  selectedBusId = null;
  stopPolling();
  await runPlan(true);
  if (currentPlan && currentPlan.options.length) pollTimer = setInterval(() => runPlan(false), 4000);
});

function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

function clearMapOverlays() {
  if (routeLine) { routeLine.setMap(null); routeLine = null; }
  endpointMarkers.forEach(m => m.setMap(null)); endpointMarkers = [];
  segmentStopMarkers.forEach(m => m.setMap(null)); segmentStopMarkers = [];
}

function clearTrip() {
  lastQuery = null; currentPlan = null; selectedBusId = null;
  clearMapOverlays();
  pruneBusMarkers(new Set());
  $("#results").innerHTML = "";
  showMessage("");
}

async function runPlan(fresh) {
  const q = lastQuery;
  try {
    const plan = await api("/api/plan", { method: "POST", body: q });
    if (q !== lastQuery) return; // a newer search replaced this one
    currentPlan = plan;
    renderPlan(plan, fresh);
  } catch (err) {
    if (fresh) showMessage(err.message, "error");
  }
}

const fmtMin = m => (m < 0.5 ? "now" : `${Math.round(m)} min`);

function renderPlan(plan, fresh) {
  const results = $("#results");
  showMessage("");
  if (fresh) drawEndpoints(plan);

  if (!plan.options.length) {
    stopPolling();
    if (routeLine) { routeLine.setMap(null); routeLine = null; }
    segmentStopMarkers.forEach(m => m.setMap(null)); segmentStopMarkers = [];
    let html = `<div class="empty"><b>No direct bus</b> from ${esc(plan.origin.name)} to ${esc(plan.destination.name)}.`;
    if (plan.transfers.length) {
      html += `<p>You can change buses:</p><ul>` + plan.transfers.map(t =>
        `<li>Change at <b>${esc(t.via)}</b>: take ${t.first.map(esc).join(" or ")}, then ${t.second.map(esc).join(" or ")}</li>`).join("") + `</ul>`;
    } else {
      html += `<p>No route through a common stop was found either — these two stops may be on opposite ends of the network.</p>`;
    }
    results.innerHTML = html + "</div>";
    return;
  }

  if (!selectedBusId) selectedBusId = plan.options[0].bus_id;

  results.innerHTML = `<p class="results-title">${plan.options.length} bus${plan.options.length > 1 ? "es" : ""} run this trip, fastest first</p>` +
    plan.options.map(o => `
      <div class="bus-card${o.bus_id === selectedBusId ? " selected" : ""}" data-id="${esc(o.bus_id)}">
        <div class="bus-num">${esc(o.number)}</div>
        <div class="bus-main">
          <div class="bus-route">${esc(o.route_name)}</div>
          <div class="bus-meta">
            <span class="pill ${o.status === "ON_TIME" ? "ok" : "warn"}">${o.status === "ON_TIME" ? "On time" : "Delayed"}</span>
            <span class="pill crowd">${esc(o.crowding)} crowd</span>
            ${o.stops} stop${o.stops > 1 ? "s" : ""} · next: ${esc(o.next_stop)}
          </div>
          <div class="bus-times">${o.eta_min < 0.5 ? "Arriving now" : `Reaches ${esc(plan.origin.name)} in <b>${fmtMin(o.eta_min)}</b>`} · ride ${fmtMin(o.ride_min)}</div>
        </div>
        <div class="bus-total"><b>${Math.round(o.total_min)}</b><span>min total</span></div>
      </div>`).join("") +
    `<p class="hint">Times are from the live simulation, not an official WBTC timetable — WBTC does not publish per-route schedules.</p>`;

  results.querySelectorAll(".bus-card").forEach(card =>
    card.addEventListener("click", () => selectBus(card.dataset.id, true)));

  const chosen = plan.options.find(o => o.bus_id === selectedBusId) || plan.options[0];
  selectedBusId = chosen.bus_id;
  drawRoute(chosen, fresh);
  refreshBusIcons();
}

function selectBus(id, fit) {
  selectedBusId = id;
  document.querySelectorAll(".bus-card").forEach(c => c.classList.toggle("selected", c.dataset.id === id));
  const opt = currentPlan.options.find(o => o.bus_id === id);
  if (opt) drawRoute(opt, fit);
  refreshBusIcons();
}

function drawEndpoints(plan) {
  endpointMarkers.forEach(m => m.setMap(null));
  endpointMarkers = [
    new google.maps.Marker({
      position: { lat: plan.origin.lat, lng: plan.origin.lng }, map,
      icon: { path: google.maps.SymbolPath.CIRCLE, scale: 9, fillColor: "#16a34a", fillOpacity: 1, strokeColor: "#fff", strokeWeight: 3 },
      title: "Start: " + plan.origin.name, zIndex: 999,
    }),
    new google.maps.Marker({
      position: { lat: plan.destination.lat, lng: plan.destination.lng }, map,
      icon: { path: google.maps.SymbolPath.CIRCLE, scale: 9, fillColor: "#dc2626", fillOpacity: 1, strokeColor: "#fff", strokeWeight: 3 },
      title: "End: " + plan.destination.name, zIndex: 999,
    }),
  ];
}

function drawRoute(option, fit) {
  if (routeLine) routeLine.setMap(null);
  segmentStopMarkers.forEach(m => m.setMap(null)); segmentStopMarkers = [];

  const path = option.segment.map(s => ({ lat: s.lat, lng: s.lng }));
  routeLine = new google.maps.Polyline({
    path, map, strokeColor: "#0f766e", strokeWeight: 5, strokeOpacity: 0.85,
  });

  segmentStopMarkers = option.segment.slice(1, -1).map(s => new google.maps.Marker({
    position: { lat: s.lat, lng: s.lng }, map,
    icon: { path: google.maps.SymbolPath.CIRCLE, scale: 4, fillColor: "#0f766e", fillOpacity: 1, strokeColor: "#fff", strokeWeight: 2 },
    title: s.name,
  }));

  if (fit) {
    const bounds = new google.maps.LatLngBounds();
    path.forEach(p => bounds.extend(p));
    map.fitBounds(bounds, 60);
  }
}

/* ------------------------------------------------------------- Start-up */
async function startApp(user) {
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#user-name").textContent = user.name;
  try {
    if (!gmapsLoaded) { await loadGoogleMaps(); gmapsLoaded = true; }
    initMap();
  } catch (err) {
    showMessage(err.message, "error");
  }
  if (!stops.length) {
    stops = await api("/api/stops");
    fillStopSelects();
    if (!stops.length) showMessage("The server has no stop locations loaded yet.", "error");
  }
  initStopLayer();
  connectSocket();
}

(async function init() {
  setAuthMode("login");
  const params = new URLSearchParams(location.search);
  if (params.has("reset")) {
    // Came from the emailed link: keep the token in memory and strip it from the address bar.
    resetToken = params.get("reset");
    history.replaceState(null, "", location.pathname);
    showPanel("reset");
    return showAuth();
  }
  if (!token) return showAuth();
  try {
    await startApp(await api("/api/me"));
  } catch (_) {
    if (token) { closeSocket(); token = null; localStorage.removeItem("bt_token"); }
    showAuth();
  }
})();
