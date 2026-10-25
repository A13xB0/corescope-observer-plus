(() => {
  "use strict";

  const PLUGIN_ID = "openhop.neighbour_battery";
  const SETTINGS = "/api/plugins/settings";
  const RUNTIME = "/api/plugins/runtime";
  const $ = (id) => document.getElementById(id);

  let config = {};
  let runtime = {};
  let dirty = false;
  let passwordUpdates = {};
  let tempTargets = null; // pending per-repeater temperature choices (a Set), null = unchanged
  let detailsTarget = null;
  let showKey = false; // the companion key is masked until asked for

  // maskKey keeps the first and last four characters, which is enough to
  // recognise the key without showing it.
  const maskKey = (k) => (k.length > 8 ? `${k.slice(0, 4)}${"•".repeat(12)}${k.slice(-4)}` : "•".repeat(k.length));

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function jwt() {
    try { return window.localStorage.getItem("pymc_jwt_token") || ""; } catch (_) { return ""; }
  }

  async function call(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const token = jwt();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const r = await fetch(url, { ...options, headers });
    if (r.status === 401) throw new Error("Log in to the openHop dashboard again, then reopen this page.");
    const body = await r.json().catch(() => ({}));
    if (!r.ok || body.success === false) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  }

  function notice(text, kind = "") {
    const el = $("notice");
    el.textContent = text || "";
    el.className = "notice " + kind;
  }

  async function loadConfig() {
    const body = await call(`${SETTINGS}?id=${encodeURIComponent(PLUGIN_ID)}`);
    const data = body.data || body;
    config = { ...(data.config || {}) };
  }

  async function loadRuntime() {
    const body = await call(`${RUNTIME}?id=${encodeURIComponent(PLUGIN_ID)}`);
    const data = body.data || body;
    runtime = data.runtime || {};
  }

  async function saveConfig(extra = {}) {
    const out = { ...config, ...extra };
    await call(SETTINGS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: PLUGIN_ID, config: out, restart: false }),
    });
    config = out;
    delete config.api_token_input;
    delete config.password_updates;
    delete config.requests;
  }

  function request(type, nodeId) {
    const id = `${type}-${Date.now()}`;
    return saveConfig({ requests: [{ id, type, node_id: nodeId || "" }] }).then(() => id);
  }

  async function waitForAction(type, id, timeoutMs = 90000) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      await new Promise((r) => setTimeout(r, 2500));
      await loadRuntime();
      const a = (runtime.actions || {})[type];
      if (a && a.id === id) { render(); return a; }
    }
    throw new Error("The plugin did not answer in time. Is it running?");
  }

  function ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return "just now";
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 129600) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  const CODE_TEXT = {
    ok: "", no_battery: "No battery fitted", needs_password: "Needs password", password_rejected: "Password rejected",
    no_reply: "No reply", not_direct: "Not reachable", not_heard: "Not heard", error: "Error",
  };

  const maxHops = () => Number((document.querySelector('input[name="max_hops"]:checked') || {}).value || 0);
  const tempSet = () => tempTargets || new Set(config.temperature_targets || []);
  const reachable = (n) => (n.routes || []).some((r) => r.hops <= maxHops());

  // The routes in the order the plugin will try them: best first, the one that
  // last answered on top.
  function orderedRoutes(n) {
    const routes = (n.routes || []).filter((r) => r.hops <= maxHops());
    const i = routes.findIndex((r) => r.key === n.last_route);
    if (i > 0) routes.unshift(routes.splice(i, 1)[0]);
    return routes;
  }

  const routeName = (r) => (r.hops ? `1 hop ${esc(r.label)}` : "Direct");
  const KEY_RE = /^[0-9a-f]{64}$/;
  const manualList = () => config.manual_repeaters || [];
  const snrText = (snr) => (snr == null ? "" : `SNR ${snr > 0 ? "+" : ""}${Number(snr).toFixed(1)} dB`);

  function routeCell(n) {
    const all = n.routes || [];
    if (!all.length) return '<span class="muted">2+ hops</span><div class="muted small">beyond reach</div>';
    const routes = orderedRoutes(n);
    if (!routes.length) return `<span class="muted">1 hop</span><div class="muted small">set Reach to "Up to 1 hop"</div>`;
    const best = routes[0];
    const notes = [];
    if (n.retried && n.last_route_label && n.code === "ok") notes.push(`last answered ${esc(n.last_route_label)}`);
    else if (snrText(best.snr)) notes.push(snrText(best.snr));
    if (routes.length > 1) notes.push(`${routes.length - 1} other route${routes.length > 2 ? "s" : ""}`);
    if (n.manual && !best.hops) notes.unshift("added by you");
    return `<div class="route"><b>${routeName(best)}</b><div class="muted small">${notes.join(" · ")}</div></div>`;
  }

  function loginCell(n) {
    if (n.code === "needs_password" || n.code === "password_rejected") return `<span class="pill bad">${esc(CODE_TEXT[n.code])}</span>`;
    if (n.code === "no_reply" && n.no_reply_count >= 2) return `<span class="pill bad">no answer ×${n.no_reply_count}</span>`;
    if (n.code === "ok" && n.retried) return '<span class="pill warn">retry used</span>';
    const pending = passwordUpdates[n.node_id];
    const pwSet = pending !== undefined ? !!pending : n.password_set;
    if (n.role) return `<span class="pill good">${esc(n.role)}</span>${pwSet ? '<div class="muted small">password set</div>' : ""}`;
    return `<span class="muted small">${pwSet ? "Password set" : "Automatic"}</span>`;
  }

  function batteryCell(n) {
    if (n.code === "ok" && n.battery_mv) {
      const via = n.method === "telemetry" ? ' · <span title="This repeater ignores a guest\'s status request, so the battery comes from telemetry">telemetry</span>' : "";
      return `<strong>${(n.battery_mv / 1000).toFixed(2)} V</strong><div class="muted small">${ago(n.last_success)}${via}</div>`;
    }
    if (n.code === "no_battery") return '<span class="pill neutral">No battery fitted</span>';
    if (!n.code || n.code === "ok") return '<span class="muted">—</span>';
    if (n.battery_mv) return `<span class="muted">${(n.battery_mv / 1000).toFixed(2)} V</span><div class="muted small">${ago(n.last_success)}</div>`;
    return `<span class="pill warn">${esc(CODE_TEXT[n.code] || n.code)}</span>${n.detail ? `<div class="muted small">${esc(n.detail)}</div>` : ""}`;
  }

  function tempCell(n) {
    if (!tempSet().has(n.node_id)) return '<span class="muted small">off</span>';
    if (n.temp_code === "ok" || (n.temperature_c != null && n.temp_misses < 3)) {
      return n.temperature_c == null ? '<span class="muted">—</span>'
        : `${Number(n.temperature_c).toFixed(1)} °C<div class="muted small">${ago(n.temp_at)}</div>`;
    }
    if (n.temp_misses >= 3) return '<span class="muted small">none reported</span>';
    return '<span class="muted">—</span>';
  }

  function render() {
    $("version").textContent = runtime.plugin_version ? `v${runtime.plugin_version}` : "";
    const checks = runtime.self_check || {};
    const allOk = Object.keys(checks).length && Object.values(checks).every((c) => c.ok);
    const gs = $("global-status");
    gs.textContent = !Object.keys(checks).length ? "Starting" : allOk ? (config.enabled ? "Polling" : "Ready") : "Needs attention";
    gs.className = "pill " + (allOk ? "good" : "warn");
    const LABELS = { config: "openHop config", api: "openHop API", companion: "Plugin companion", publish: "Publishing key" };
    $("checks").innerHTML = Object.entries(checks).map(([k, c]) =>
      `<li class="${c.ok ? "ok" : "bad"}"><span>${c.ok ? "✓" : "✗"}</span> <strong>${esc(LABELS[k] || k)}</strong> <span class="muted">${esc(c.message)}</span></li>`).join("");
    $("connect").textContent = runtime.token_configured && checks.companion && checks.companion.ok ? "Reconnect" : "Connect";
    const r = runtime.repeater || {};
    $("repeater-name").textContent = r.name ? `${r.name}${r.iata ? " · " + r.iata : ""}` : "—";
    const key = (runtime.companion || {}).public_key || "";
    $("companion-key").textContent = key ? (showKey ? key.toUpperCase() : maskKey(key.toUpperCase())) : "—";
    $("show-key").textContent = showKey ? "Hide" : "Show";
    $("show-key").setAttribute("aria-pressed", String(showKey));
    $("show-key").disabled = !key;
    $("companion-key").dataset.key = key;

    if (!dirty) {
      $("enabled").checked = !!config.enabled;
      $("interval_minutes").value = String(config.interval_minutes || 60);
      $("alternative_routes").checked = !!config.alternative_routes;
      const hops = String(config.max_hops ? 1 : 0);
      document.querySelectorAll('input[name="max_hops"]').forEach((el) => { el.checked = el.value === hops; });
    }
    const showTemp = tempSet().size > 0;
    document.querySelectorAll(".temp-col").forEach((el) => { el.hidden = !showTemp; });

    // While editing, keep what's ticked on screen rather than the saved lists.
    const ticked = (attr, saved) => {
      const els = [...document.querySelectorAll(`[data-${attr}]`)];
      return new Set(dirty && els.length ? els.filter((el) => el.checked).map((el) => el.dataset[attr]) : saved || []);
    };
    const chosenBrokers = ticked("broker", config.brokers);
    $("brokers").innerHTML = (runtime.brokers || []).map((b) => {
      const last = b.last_error ? `<span class="pill warn" title="${esc(b.last_error)}">Failed</span> <span class="muted small">${esc(b.last_error)}</span>`
        : b.last_ok ? `${ago(b.last_ok)} · ${b.last_count} reading(s)` : '<span class="muted">—</span>';
      return `<tr><td><input type="checkbox" data-broker="${esc(b.name)}" ${chosenBrokers.has(b.name) ? "checked" : ""} aria-label="Publish to ${esc(b.name)}"></td>
        <td><strong>${esc(b.name)}</strong><div class="muted small">${esc(b.host)} · ${esc(b.format)}</div></td>
        <td>${!b.enabled ? '<span class="muted">disabled</span>' : b.openhop_connected ? '<span class="pill good">connected</span>' : '<span class="pill warn">not connected</span>'}</td>
        <td>${last}${b.queued ? ` <span class="muted small">· ${b.queued} queued</span>` : ""}</td></tr>`;
    }).join("") || '<tr><td colspan="4" class="muted">No brokers configured in openHop.</td></tr>';

    const chosen = ticked("node", config.targets);
    const hopsOf = (n) => (n.hops == null ? 9 : n.hops);
    const nodes = (runtime.nodes || []).slice().sort((a, b) => (hopsOf(a) - hopsOf(b)) || ((b.snr ?? -99) - (a.snr ?? -99)));
    $("selected-count").textContent = `${chosen.size} selected`;
    $("nodes").innerHTML = nodes.map((n) => {
      const ok = reachable(n);
      return `<tr class="${ok ? "" : "dim"}">
        <td><input type="checkbox" data-node="${esc(n.node_id)}" ${chosen.has(n.node_id) ? "checked" : ""} ${ok ? "" : "disabled"} aria-label="Poll ${esc(n.name)}"></td>
        <td><strong>${esc(n.name)}</strong><div class="muted small mono">${esc(n.node_id.slice(0, 8))}…${n.uptime_secs ? ` · up ${Math.floor(n.uptime_secs / 86400)} d` : ""}</div></td>
        <td>${routeCell(n)}</td>
        <td>${ago(n.last_seen)}</td>
        <td>${ok || n.role ? loginCell(n) : ""}</td>
        <td>${ok ? batteryCell(n) : ""}</td>
        <td class="temp-col" ${showTemp ? "" : "hidden"}>${ok ? tempCell(n) : ""}</td>
        <td><button class="button ghost small" type="button" data-details="${esc(n.node_id)}" ${ok ? "" : "disabled"}>Details</button></td></tr>`;
    }).join("") || '<tr><td colspan="8" class="muted">No repeaters heard yet.</td></tr>';
    $("reach-note").textContent = maxHops()
      ? "Repeaters heard direct, or through one relay you hear directly."
      : "Only repeaters heard directly (zero hops) can be selected. Set Reach to \"Up to 1 hop\" for more.";
  }

  function collect() {
    config.enabled = $("enabled").checked;
    config.interval_minutes = Number($("interval_minutes").value);
    config.brokers = [...document.querySelectorAll("[data-broker]")].filter((el) => el.checked).map((el) => el.dataset.broker);
    // Targets with a row take its tick; one just added has no row until saved.
    const boxes = [...document.querySelectorAll("[data-node]")];
    const shown = new Set(boxes.map((el) => el.dataset.node));
    config.targets = [...boxes.filter((el) => el.checked).map((el) => el.dataset.node),
      ...(config.targets || []).filter((k) => !shown.has(k))];
    config.alternative_routes = $("alternative_routes").checked;
    config.max_hops = maxHops();
    config.temperature_targets = [...tempSet()];
  }

  async function connect() {
    notice("Connecting…");
    try {
      let extra = {};
      if (!runtime.token_configured) {
        const t = await call("/api/auth/tokens", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "Neighbour Battery plugin" }),
        });
        extra.api_token_input = t.token;
      }
      const id = `connect-${Date.now()}`;
      await saveConfig({ ...extra, requests: [{ id, type: "connect" }] });
      const a = await waitForAction("connect", id);
      notice(a.message, a.ok ? "good" : "bad");
    } catch (e) {
      notice(e.message, "bad");
    }
  }

  async function test(nodeId) {
    notice("Asking for status, logging in if needed. This can take up to a minute…");
    try {
      const id = await request("test_login", nodeId);
      const a = await waitForAction("test_login", id);
      const extra = `${a.route ? ` ${a.route}` : ""}${a.temperature_c != null ? `, ${Number(a.temperature_c).toFixed(1)} °C` : ""}`;
      const text = a.code === "ok" ? `Answered${a.role ? " as " + a.role : ""}${extra}: ${(a.battery_mv / 1000).toFixed(2)} V`
        : a.code === "no_battery" ? `Answered${a.role ? " as " + a.role : ""}${extra}: no battery fitted (0 mV)`
        : `${CODE_TEXT[a.code] || a.code || "Failed"}${a.message && a.message !== a.code ? ": " + a.message : ""}`;
      notice(text, a.code === "ok" || a.code === "no_battery" ? (a.role === "admin" ? "warn" : "good") : "bad");
    } catch (e) {
      notice(e.message, "bad");
    }
  }

  // Poll every selected neighbour now (saved selection), then publish.
  async function checkNow() {
    if (dirty) { notice("Save first, so the check uses your selection.", "warn"); return; }
    if (!(config.targets || []).length) { notice("Select at least one neighbour first.", "warn"); return; }
    notice(`Checking ${config.targets.length} neighbour(s). Each can take up to a minute…`);
    $("check-now").disabled = true;
    try {
      const id = await request("check_now");
      const a = await waitForAction("check_now", id, 120000 * config.targets.length);
      notice(a.ok ? `Checked: ${a.message}` : a.message, a.ok && a.readings ? "good" : "bad");
    } catch (e) {
      notice(e.message, "bad");
    } finally {
      $("check-now").disabled = false;
    }
  }

  function copyViaTextarea(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("copy refused");
  }

  function openDetails(nodeId) {
    detailsTarget = nodeId;
    const n = (runtime.nodes || []).find((x) => x.node_id === nodeId);
    if (!n) return;
    $("details-title").textContent = n.name;
    $("login-password").value = "";
    $("remove-password").checked = false;
    const pending = passwordUpdates[nodeId];
    $("remove-password").disabled = !(pending !== undefined ? pending : n.password_set);
    $("remove-manual").hidden = !n.manual;
    $("temp-this").checked = tempSet().has(nodeId);
    $("temp-last").textContent = n.temperature_c != null ? `Last: ${Number(n.temperature_c).toFixed(1)} °C, ${ago(n.temp_at)}`
      : n.temp_misses >= 3 ? "None reported so far" : "";
    const routes = orderedRoutes(n);
    $("routes").innerHTML = routes.map((r, i) => {
      const last = r.key === n.last_route ? (n.code === "ok" ? '<span class="pill good">answered</span>' : '<span class="pill bad">no answer</span>')
        : i === 0 && n.code === "no_reply" ? '<span class="pill bad">no answer</span>' : '<span class="muted small">not tried</span>';
      return `<tr><td class="muted">${i + 1}</td><td>${routeName(r)}</td><td>${r.adverts}</td><td>${r.snr == null ? "—" : `${Number(r.snr).toFixed(1)} dB`}</td><td>${last}</td></tr>`;
    }).join("") || '<tr><td colspan="5" class="muted">No usable route.</td></tr>';
    $("details-dialog").showModal();
  }

  $("details-form").addEventListener("submit", (ev) => {
    const action = ev.submitter && ev.submitter.value;
    if (!detailsTarget || action === "cancel") return;
    if (action === "test") { test(detailsTarget); return; }
    if (action === "remove") {
      const id = detailsTarget;
      config.manual_repeaters = manualList().filter((m) => m.node_id !== id);
      config.targets = (config.targets || []).filter((k) => k !== id);
      document.querySelectorAll(`[data-node="${id}"]`).forEach((el) => { el.checked = false; });
      dirty = true;
      render();
      notice("Save to remove the repeater.");
      return;
    }
    const pw = $("login-password").value;
    if ($("remove-password").checked) passwordUpdates[detailsTarget] = "";
    else if (pw) passwordUpdates[detailsTarget] = pw;
    const temps = new Set(tempSet());
    if ($("temp-this").checked) temps.add(detailsTarget); else temps.delete(detailsTarget);
    tempTargets = temps;
    dirty = true;
    render();
    notice("Save to apply the changes.");
  });

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.dataset.details) openDetails(t.dataset.details);
    else if (t.dataset.add) addManual(t.dataset.add, t.dataset.name);
  });
  document.addEventListener("change", (ev) => {
    if (ev.target.closest("dialog")) return;
    if (ev.target.matches("input, select")) {
      dirty = true;
      if (ev.target.name === "max_hops") render();
    }
  });
  function addManual(nodeId, name) {
    nodeId = nodeId.toLowerCase();
    if (!manualList().some((m) => m.node_id === nodeId)) config.manual_repeaters = [...manualList(), { node_id: nodeId, name: name || nodeId.slice(0, 8) }];
    if (!(config.targets || []).includes(nodeId)) config.targets = [...(config.targets || []), nodeId];
    $("add-results").innerHTML = "";
    $("add-query").value = "";
    dirty = true;
    notice(`Save to add ${name || nodeId.slice(0, 8)}. It is treated as heard direct; Details shows its routes once saved.`, "good");
  }

  $("add-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("add-query").value.trim();
    if (KEY_RE.test(q.toLowerCase())) { addManual(q, ""); return; }
    if (q.length < 3) { notice("Type at least 3 characters, or paste a 64-character key.", "warn"); return; }
    if (dirty) { notice("Save first, then search.", "warn"); return; }
    notice(`Searching CoreScope for "${q}"…`);
    try {
      const id = `lookup-${Date.now()}`;
      await saveConfig({ requests: [{ id, type: "lookup", query: q }] });
      const a = await waitForAction("lookup", id, 60000);
      if (!a.ok) { notice(a.message, "bad"); return; }
      const known = new Set((runtime.nodes || []).map((n) => n.node_id));
      $("add-results").innerHTML = (a.results || []).map((r) => `<li><span><strong>${esc(r.name)}</strong>
        <span class="muted small mono">${esc(r.node_id.slice(0, 8))}… · ${esc(r.role)}${r.last_seen ? " · heard " + esc(String(r.last_seen).replace("T", " ").slice(0, 16)) : ""}</span></span>
        ${known.has(r.node_id) ? '<span class="muted small">already listed</span>'
          : `<button class="button ghost small" type="button" data-add="${esc(r.node_id)}" data-name="${esc(r.name)}">Add</button>`}</li>`).join("")
        || '<li class="muted">Nothing found.</li>';
      notice(a.message);
    } catch (e) {
      notice(e.message, "bad");
    }
  });

  $("connect").addEventListener("click", connect);
  $("check-now").addEventListener("click", checkNow);
  $("show-key").addEventListener("click", () => { showKey = !showKey; render(); });
  $("copy-key").addEventListener("click", async () => {
    const key = ($("companion-key").dataset.key || "").toUpperCase();
    if (!key) return;
    // navigator.clipboard only exists on https; openHop is usually plain http.
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(key);
      else copyViaTextarea(key);
      notice("Key copied.");
    } catch (_) {
      notice("Couldn't copy. Press Show to see the key.", "warn");
    }
  });
  $("save").addEventListener("click", async () => {
    try {
      collect();
      const extra = Object.keys(passwordUpdates).length ? { password_updates: passwordUpdates } : {};
      await saveConfig(extra);
      passwordUpdates = {};
      tempTargets = null;
      dirty = false;
      notice("Saved.", "good");
    } catch (e) {
      notice(e.message, "bad");
    }
  });

  async function refresh() {
    try {
      await loadRuntime();
      if (!dirty) await loadConfig();
      render();
    } catch (e) {
      notice(e.message, "bad");
    }
  }

  refresh();
  setInterval(refresh, 15000);
})();
