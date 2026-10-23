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
  let loginTarget = null;

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
    no_reply: "No reply", not_direct: "Not direct", not_heard: "Not heard", error: "Error",
  };

  function batteryCell(n) {
    if (n.code === "ok" && n.battery_mv) return `<strong>${(n.battery_mv / 1000).toFixed(2)} V</strong><div class="muted small">${ago(n.last_success)}</div>`;
    if (!n.code) return '<span class="muted">—</span>';
    const kind = n.code === "no_battery" ? "neutral" : "warn";
    return `<span class="pill ${kind}">${esc(CODE_TEXT[n.code] || n.code)}</span>${n.detail ? `<div class="muted small">${esc(n.detail)}</div>` : ""}`;
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
    $("companion-key").textContent = key ? key.toUpperCase() : "—";
    $("companion-key").dataset.key = key;

    if (!dirty) {
      $("enabled").checked = !!config.enabled;
      $("interval_minutes").value = String(config.interval_minutes || 60);
    }

    const chosenBrokers = new Set(config.brokers || []);
    $("brokers").innerHTML = (runtime.brokers || []).map((b) => {
      const last = b.last_error ? `<span class="pill warn" title="${esc(b.last_error)}">Failed</span> <span class="muted small">${esc(b.last_error)}</span>`
        : b.last_ok ? `${ago(b.last_ok)} · ${b.last_count} reading(s)` : '<span class="muted">—</span>';
      return `<tr><td><input type="checkbox" data-broker="${esc(b.name)}" ${chosenBrokers.has(b.name) ? "checked" : ""} aria-label="Publish to ${esc(b.name)}"></td>
        <td><strong>${esc(b.name)}</strong><div class="muted small">${esc(b.host)} · ${esc(b.format)}</div></td>
        <td>${!b.enabled ? '<span class="muted">disabled</span>' : b.openhop_connected ? '<span class="pill good">connected</span>' : '<span class="pill warn">not connected</span>'}</td>
        <td>${last}${b.queued ? ` <span class="muted small">· ${b.queued} queued</span>` : ""}</td></tr>`;
    }).join("") || '<tr><td colspan="4" class="muted">No brokers configured in openHop.</td></tr>';

    const chosen = new Set(config.targets || []);
    const nodes = (runtime.nodes || []).slice().sort((a, b) => (b.zero_hop - a.zero_hop) || ((b.snr ?? -99) - (a.snr ?? -99)));
    $("selected-count").textContent = `${chosen.size} selected`;
    $("nodes").innerHTML = nodes.map((n) => {
      const pending = passwordUpdates[n.node_id];
      const pwSet = pending !== undefined ? !!pending : n.password_set;
      const login = `${pwSet ? "Password set" : "Automatic"}${n.role ? ` <span class="muted small">(${esc(n.role)})</span>` : ""}`;
      return `<tr class="${n.zero_hop ? "" : "dim"}">
        <td><input type="checkbox" data-node="${esc(n.node_id)}" ${chosen.has(n.node_id) ? "checked" : ""} ${n.zero_hop ? "" : "disabled"} aria-label="Poll ${esc(n.name)}"></td>
        <td><strong>${esc(n.name)}</strong><div class="muted small mono">${esc(n.node_id.slice(0, 8))}…${n.zero_hop ? "" : " · not direct"}</div></td>
        <td>${n.snr == null ? "—" : `${n.snr > 0 ? "+" : ""}${Number(n.snr).toFixed(1)} dB`}</td>
        <td>${ago(n.last_seen)}</td>
        <td>${login} <button class="button ghost small" type="button" data-login="${esc(n.node_id)}" ${n.zero_hop ? "" : "disabled"}>Set</button></td>
        <td>${batteryCell(n)}</td>
        <td><button class="button ghost small" type="button" data-test="${esc(n.node_id)}" ${n.zero_hop ? "" : "disabled"}>Test</button></td></tr>`;
    }).join("") || '<tr><td colspan="7" class="muted">No repeaters heard yet.</td></tr>';
  }

  function collect() {
    config.enabled = $("enabled").checked;
    config.interval_minutes = Number($("interval_minutes").value);
    config.brokers = [...document.querySelectorAll("[data-broker]")].filter((el) => el.checked).map((el) => el.dataset.broker);
    config.targets = [...document.querySelectorAll("[data-node]")].filter((el) => el.checked).map((el) => el.dataset.node);
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
    notice("Logging in and asking for status. This can take up to a minute…");
    try {
      const id = await request("test_login", nodeId);
      const a = await waitForAction("test_login", id);
      const text = a.code === "ok" ? `Logged in${a.role ? " as " + a.role : ""}: ${(a.battery_mv / 1000).toFixed(2)} V`
        : a.code === "no_battery" ? `Logged in${a.role ? " as " + a.role : ""}: no battery fitted (0 mV)`
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

  function openLogin(nodeId) {
    loginTarget = nodeId;
    const n = (runtime.nodes || []).find((x) => x.node_id === nodeId);
    $("login-title").textContent = `Login for ${n ? n.name : nodeId.slice(0, 8)}`;
    $("login-password").value = "";
    $("login-dialog").showModal();
  }

  $("login-form").addEventListener("submit", (ev) => {
    const action = ev.submitter && ev.submitter.value;
    if (!loginTarget || action === "cancel") return;
    passwordUpdates[loginTarget] = action === "remove" ? "" : $("login-password").value;
    dirty = true;
    render();
    notice("Save to apply the login change.");
  });

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.dataset.login) openLogin(t.dataset.login);
    else if (t.dataset.test) test(t.dataset.test);
  });
  document.addEventListener("change", (ev) => {
    if (ev.target.matches("input, select")) dirty = true;
  });
  $("connect").addEventListener("click", connect);
  $("check-now").addEventListener("click", checkNow);
  $("copy-key").addEventListener("click", async () => {
    const key = ($("companion-key").dataset.key || "").toUpperCase();
    if (!key) return;
    try { await navigator.clipboard.writeText(key); notice("Key copied."); }
    catch (_) { notice(key); }
  });
  $("save").addEventListener("click", async () => {
    try {
      collect();
      const extra = Object.keys(passwordUpdates).length ? { password_updates: passwordUpdates } : {};
      await saveConfig(extra);
      passwordUpdates = {};
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
