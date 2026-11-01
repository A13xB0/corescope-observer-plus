(() => {
  "use strict";

  const PLUGIN_ID = "openhop.neighbour_battery";
  const SETTINGS = "/api/plugins/settings";
  const RUNTIME = "/api/plugins/runtime";
  const $ = (id) => document.getElementById(id);

  let config = {};
  let loaded = {};  // settings as last read from or written to the plugin (see mergeLatest)
  let runtime = {};
  let dirty = false;
  let passwordUpdates = {};
  let tempTargets = null; // pending per-repeater temperature choices (a Set), null = unchanged
  let detailsTarget = null;
  let testRun = 0;  // bumped per Test (and when the dialog closes): stale runs stop updating
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
    loaded = JSON.parse(JSON.stringify(config));
  }

  // Every save writes the whole settings object, so a page opened before a
  // change made elsewhere (another tab, or the plugin itself) would undo it.
  // Per-repeater settings are therefore merged: only what this page changed
  // since it last loaded or saved is applied on top of the latest saved copy.
  let guestShown = false;  // the Details checkbox as opened
  let guestUseDefault = false;  // Use the default was pressed in Details
  const LIST_KEYS = ["targets", "temperature_targets"];
  const MAP_KEYS = ["guest_passwords", "poll_intervals", "guest_key_overrides"];
  async function mergeLatest() {
    let latest;
    try {
      const body = await call(`${SETTINGS}?id=${encodeURIComponent(PLUGIN_ID)}`);
      latest = (body.data || body).config || {};
    } catch (_) {
      return;  // save what we have
    }
    for (const k of LIST_KEYS) {
      const page = new Set(config[k] || []), base = new Set(loaded[k] || []);
      const merged = new Set(latest[k] || []);
      page.forEach((x) => { if (!base.has(x)) merged.add(x); });
      base.forEach((x) => { if (!page.has(x)) merged.delete(x); });
      config[k] = [...merged];
    }
    for (const k of MAP_KEYS) {
      const page = config[k] || {}, base = loaded[k] || {};
      const merged = { ...(latest[k] || {}) };
      new Set([...Object.keys(page), ...Object.keys(base)]).forEach((id) => {
        if (JSON.stringify(page[id]) === JSON.stringify(base[id])) return;  // unchanged here
        if (id in page) merged[id] = page[id]; else delete merged[id];
      });
      config[k] = merged;
    }
    const byId = (list) => new Map((list || []).map((m) => [m.node_id, m]));
    const pageM = byId(config.manual_repeaters), baseM = byId(loaded.manual_repeaters);
    const mergedM = byId(latest.manual_repeaters);
    pageM.forEach((m, id) => { if (!baseM.has(id)) mergedM.set(id, m); });
    baseM.forEach((_m, id) => { if (!pageM.has(id)) mergedM.delete(id); });
    config.manual_repeaters = [...mergedM.values()];
  }

  async function loadRuntime() {
    const body = await call(`${RUNTIME}?id=${encodeURIComponent(PLUGIN_ID)}`);
    const data = body.data || body;
    runtime = data.runtime || {};
  }

  async function saveConfig(extra = {}) {
    await mergeLatest();
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
    loaded = JSON.parse(JSON.stringify(config));
  }

  async function request(type, nodeId, extra) {
    const id = `${type}-${Date.now()}`;
    const body = { requests: [{ id, type, node_id: nodeId || "", ...(extra || {}) }] };
    try {
      await saveConfig(body);
    } catch (_e) {
      // The plugin may be momentarily busy serving a poll; one retry.
      await new Promise((r) => setTimeout(r, 1500));
      await saveConfig(body);
    }
    return id;
  }

  async function waitForAction(type, id, timeoutMs = 90000, onProgress, shouldAbort) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      if (shouldAbort && shouldAbort()) { const e = new Error("stopped"); e.aborted = true; throw e; }
      await new Promise((r) => setTimeout(r, 2000));
      // The plugin is single-threaded: while it is busy polling it can't serve
      // the runtime, so a fetch here can fail transiently. Keep waiting rather
      // than giving up — the poll is still running.
      try {
        await loadRuntime();
      } catch (_e) {
        continue;
      }
      const a = (runtime.actions || {})[type];
      if (a && a.id === id) {
        if (onProgress) onProgress(a);
        // `running` is set while the plugin is still working through the
        // request; the result isn't final until it clears. Older plugins
        // don't send it, so an absent flag means done, as before.
        if (!a.running) { render(); return a; }
      }
    }
    throw new Error("The plugin did not answer in time. Is it running?");
  }

  // pollText/pollKind describe one poll outcome (a check-now row, a test, or
  // a node's recorded state) in the same words everywhere.
  function pollText(x) {
    const route = x.route ? ` ${x.route}` : "";
    const temp = x.temperature_c != null ? `, ${Number(x.temperature_c).toFixed(1)} °C` : "";
    const role = x.role ? " as " + x.role : "";
    // A "covered" poll: we didn't hear the answer, another observer did and
    // CoreScope has it — there's no voltage of our own to show.
    if (x.covered) return `heard via another observer${route}`;
    const via = x.via_corescope ? " (heard via CoreScope)" : "";
    if (x.code === "ok") return `${(x.battery_mv / 1000).toFixed(2)} V${role}${route}${temp}${via}`;
    if (x.code === "no_battery") return `no battery fitted${role}${route}${temp}`;
    return (CODE_TEXT[x.code] || x.code || "no answer") + (x.message && x.message !== x.code ? ": " + x.message : "");
  }
  function pollKind(x) {
    return x.code === "ok" || x.code === "no_battery" ? (x.role === "admin" ? "warn" : "good") : "bad";
  }

  // corescopeText turns a CoreScope confirmation (action.corescope / a
  // progress row's .corescope) into a short "did it land?" line.
  function corescopeText(cs) {
    if (!cs) return "";
    if (!cs.reachable) return "CoreScope unreachable";
    if (!cs.latest_ts) return "CoreScope has nothing for it yet";
    const by = cs.by ? ` (${cs.by})` : "";
    return (cs.fresh ? "CoreScope has it" : "CoreScope last saw")
      + (cs.latest_mv != null ? ` ${(cs.latest_mv / 1000).toFixed(2)} V` : "")
      + by + " · " + ago(Date.parse(cs.latest_ts) / 1000);
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
  const guestDefault = () => config.guest_key_default !== false;
  const guestFor = (id) => {
    const ov = (config.guest_key_overrides || {})[id];
    return ov === undefined ? guestDefault() : ov;
  };
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
    if (n.covered && n.code === "ok") {
      // Not our reading: another observer overheard the answer to our request.
      const v = n.battery_mv ? `${(n.battery_mv / 1000).toFixed(2)} V` : "—";
      return `<span class="muted">${v}</span><div class="muted small">heard via another observer · ${ago(n.covered_at)}</div>`;
    }
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

  // A small badge showing how a repeater is polled: under the shared key, or
  // stopped because it answered the key as admin.
  function keyBadge(n) {
    const ov = (config.guest_key_overrides || {})[n.node_id];
    if (ov === undefined) return keyState(n);
    return keyState(n) + ` · <span class="pill warn" title="Set by hand in Details: guest key ${ov ? "on" : "off"}. The default doesn't change it until you press Use the default.">overridden</span>`;
  }

  function keyState(n) {
    if (n.shared_admin_seen) return ' · <span class="pill bad" title="Answered the guest key as admin; guest polling stopped here">admin!</span>';
    if (n.flood_replies >= 2) return ' · <span class="pill bad" title="Its replies keep coming back flooded (our return route isn\'t taking), so it is polled only once a day until a reply comes back direct">floods · daily</span>';
    if (n.key_mode === "shared") {
      const own = (runtime.shared_key || {}).kind === "own";
      return ` · <span class="pill good" title="${own ? "Polled under this plugin's separate polling key (not the repeater's key), registered with CoreScope" : "Polled under the public shared guest key"}">${own ? "guest key" : "shared"}</span>`;
    }
    if (n.shared_opt_in) return ' · <span class="muted small" title="Set to use it, but the guest key is not usable here yet">shared (off)</span>';
    return "";
  }

  function everyText(min) {
    min = Math.round(min);
    return min < 60 ? `every ${min} min` : min === 60 ? "hourly" : `every ${dur(min * 60)}`;
  }

  // -- community polling (CoreScope's limits and switches) -------------------

  // dur: a length of time in seconds, short ("2 h", "1 h 30 min", "45 min").
  function dur(s) {
    s = Math.round(Number(s) || 0);
    if (s < 3600) return `${Math.round(s / 60)} min`;
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const hhmm = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");
  const whenText = (iso) => {
    const t = Date.parse(iso);
    return Number.isNaN(t) ? String(iso) : new Date(t).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  const hopsText = (h) => (h === 0 ? " (direct)" : h === 1 ? " (1 hop)" : "");
  const community = () => runtime.community || {};
  const gate = () => community().gate || null;
  const GATE_LABEL = { paused: "Paused by CoreScope", blocked: "Stopped by CoreScope", version_disabled: "Upgrade needed" };
  const GATE_SHORT = { paused: "Paused", blocked: "Stopped", version_disabled: "Upgrade needed" };
  const gateKind = (g) => (g && g.reason === "version_disabled" ? "warn" : "bad");
  const hostOf = (url) => { try { return new URL(url).host; } catch (_) { return url || "CoreScope"; } };

  // The window CoreScope sets for a repeater: its own figure, else direct or
  // one-hop by this observer's best route (the floor for its interval).
  function windowFor(n) {
    if (n.community && n.community.window_s) return n.community.window_s;
    const p = community().policy || {};
    const routes = orderedRoutes(n);
    const w = (p.node_windows || {})[n.node_id] || ((routes[0] || {}).hops ? p.one_hop_s : p.direct_s) || 0;
    return community().stale ? w * 2 : w;
  }

  function clampText(c) {
    const v = (x) => (c.field === "max_requests" || c.field === "takeover_after" ? x : dur(x));
    return `CoreScope asked for ${v(c.asked)}, using ${v(c.using)}`;
  }

  // The "Who polls" cell: you (and when), standby (and who polls), covered by
  // someone's fresh reading, or not polled at all.
  function whoCell(n) {
    const c = n.community;
    if (!c) return '<span class="muted">—</span>';
    const g = gate();
    if (g) {
      const part = c.who === "standby" ? "standby" : c.who === "excluded" ? "excluded" : "you poll this one";
      return `<span class="pill ${gateKind(g)}">${esc(GATE_SHORT[g.reason] || "Stopped")}</span><div class="muted small">${part}</div>`;
    }
    if (c.who === "excluded") return '<span class="pill neutral">Not polled</span><div class="muted small">CoreScope excludes it (owner\'s request)</div>';
    if (c.who === "standby") {
      return `<span class="pill neutral">Standby</span><div class="muted small">${esc(c.by || "another observer")} polls${hopsText(c.owner_hops)}</div>`;
    }
    if (c.who === "covered") {
      return `<span class="pill good">Covered</span><div class="muted small">reading ${ago(c.fresh_at)}${c.fresh_by ? " by " + esc(c.fresh_by) : ""}, skip until ${hhmm(c.next_at)}</div>`;
    }
    if (c.who === "capped") return `<span class="pill warn">Limit reached</span><div class="muted small">${esc((c.skip || {}).text || "")}; next ${hhmm(c.next_at)}</div>`;
    const next = config.enabled ? `next ${hhmm(c.next_at)} · ${everyText(c.interval_s / 60)}` : "polling is off";
    return `<span class="pill good">You</span><div class="muted small">${next}</div>`;
  }

  // The banner for CoreScope's switches: pause, block, version minimum.
  function renderCommunityNotice() {
    const el = $("community-notice");
    const cm = community();
    const p = cm.policy || {};
    const g = gate();
    if (!g) { el.hidden = true; el.innerHTML = ""; return; }
    let html;
    if (g.reason === "paused") {
      html = `<b>Paused by CoreScope.${p.paused_reason ? " " + esc(p.paused_reason) : ""}</b>`
        + `<span class="small">Nothing is sent ${p.paused_until ? `until ${esc(whenText(p.paused_until))}, or ` : ""}until CoreScope lifts the pause. Claims still go out, so CoreScope shows this observer as paused. Test and Check now are off.</span>`;
    } else if (g.reason === "blocked") {
      html = "<b>CoreScope has stopped this observer from polling.</b>"
        + `<span class="small">This observer's key is on ${esc(hostOf(cm.corescope_url))}'s blocked list, so the plugin sends nothing and CoreScope ignores its readings. Ask the CoreScope operator why; nothing on this side can lift it.</span>`;
    } else {
      html = `<b>This version (${esc(runtime.plugin_version || "?")}) is disabled by CoreScope. Upgrade to ${esc(p.min_plugin_version || "a newer version")} or later to keep providing data.</b>`
        + `<span class="small">${p.min_version_reason ? `CoreScope's note: "${esc(p.min_version_reason)}" ` : ""}Until you upgrade, nothing is sent and CoreScope ignores this observer's claims and readings. Your settings and passwords are kept.</span>`;
    }
    el.hidden = false;
    el.className = `notice community-notice ${gateKind(g)}`;
    el.innerHTML = html;
  }

  function renderCommunity() {
    const cm = community();
    const p = cm.policy || {};
    const g = gate();
    const pill = $("community-state");
    const on = config.community !== false;
    pill.textContent = g ? (GATE_SHORT[g.reason] || "Stopped") : !on ? "Off"
      : `On · ${cm.claimed || 0} repeater${cm.claimed === 1 ? "" : "s"} claimed`;
    pill.className = "pill " + (g ? gateKind(g) : on ? "good" : "neutral");
    if (!dirty) $("community").checked = on;
    $("community-limits").innerHTML = p.direct_s ? [
      [dur(p.direct_s), "direct"], [dur(p.one_hop_s), "1 hop"], [dur(p.grace_s), "standby grace"],
      [String(p.max_requests), `request${p.max_requests === 1 ? "" : "s"} per window`],
    ].map(([v, l]) => `<div><b>${esc(v)}</b>${esc(l)}</div>`).join("") : "";
    const where = hostOf(cm.corescope_url);
    const why = "They apply to every observer polling a repeater, counted from the newest reading anyone sent.";
    $("community-source").textContent = !cm.corescope_url ? `Built-in defaults: no CoreScope address set. ${why}`
      : cm.has_policy ? `Limits from ${where}${p.version ? " · policy " + p.version : ""} · checked ${ago(cm.fetched_at)}${cm.last_error ? ` (last try: ${cm.last_error}; using the saved copy)` : ""}. ${why}`
      : `Built-in defaults: no policy from ${where} yet${cm.last_error ? ` (${cm.last_error})` : ""}. ${why}`;
    const warn = [];
    if (cm.stale) warn.push("CoreScope hasn't answered for over 6 hours: every repeater is polled at twice its window until it does.");
    (cm.clamped || []).filter((c) => !c.node_id).forEach((c) => warn.push(clampText(c) + "."));
    if (cm.fast_mode) warn.push("Test fast mode is on (OPENHOP_NEIGHBOUR_BATTERY_TEST_FAST): for bench runs only.");
    $("community-warn").textContent = warn.join(" ");
  }

  // The packet log: what was sent, and what answered it (packet_log.py).
  function packetAnswer(e) {
    if (e.reply === "none expected") return '<span class="muted">none expected</span>';
    if (e.reply === "waiting") return '<span class="pill warn">waiting</span>';
    if (e.reply === "refused") return `<span class="pill bad">refused</span>`;
    if (e.reply !== "answered") return `<span class="pill bad">no answer</span>${e.what ? ` <span class="muted small">${esc(e.what)}</span>` : ""}`;
    const how = e.flooded === true ? ' <span class="pill bad" title="The repeater had no route back, so it flooded the answer across the mesh">flooded</span>'
      : e.flooded === false ? ' <span class="pill good">direct</span>' : "";
    const via = e.via === "CoreScope" ? "via CoreScope" : "heard here";
    const after = e.after_s != null ? ` · ${Number(e.after_s).toFixed(1)} s` : "";
    return `<span class="pill good">answered</span>${how}<div class="muted small">${esc(via)}${after}${e.what ? " · " + esc(e.what) : ""}</div>`;
  }
  // Rows for a packet log, newest first; withName adds the Repeater column.
  function packetRows(entries, withName) {
    const log = (entries || []).slice().reverse();
    const cols = withName ? 6 : 5;
    return log.length ? log.map((e) => `<tr>
      <td class="small">${new Date(e.at * 1000).toLocaleTimeString()}<div class="muted small">${ago(e.at)}</div></td>
      ${withName ? `<td>${esc(e.name || String(e.node_id || "").slice(0, 8))}</td>` : ""}
      <td><strong>${esc(e.kind)}</strong>${e.detail ? `<div class="muted small">${esc(e.detail)}</div>` : ""}${e.unconfirmed ? '<div class="muted small">openHop busy: sent unconfirmed</div>' : ""}</td>
      <td class="small">${esc(e.route || "")}</td>
      <td class="small">${esc(e.key || "")}</td>
      <td>${packetAnswer(e)}</td></tr>`).join("")
      : `<tr><td colspan="${cols}" class="muted">Nothing sent yet.</td></tr>`;
  }
  function renderPackets() {
    $("packets").innerHTML = packetRows(runtime.packet_log, true);
  }

  function render() {
    renderPackets();
    $("version").textContent = runtime.plugin_version ? `v${runtime.plugin_version}` : "";
    const checks = runtime.self_check || {};
    const allOk = Object.keys(checks).length && Object.values(checks).every((c) => c.ok);
    const gs = $("global-status");
    // The plugin rewrites runtime.json every few seconds while it runs; a
    // stale one means it is stopped and nothing saved here is acted on yet.
    const age = runtime.generated_at ? Date.now() / 1000 - Date.parse(runtime.generated_at) / 1000 : 0;
    const stopped = age > 20 * 60;
    gs.textContent = stopped ? `Not running (last seen ${ago(Date.parse(runtime.generated_at) / 1000)})`
      : !Object.keys(checks).length ? "Starting" : allOk ? (config.enabled ? "Polling" : "Ready") : "Needs attention";
    gs.className = "pill " + (stopped ? "bad" : !Object.keys(checks).length ? "neutral" : allOk ? "good" : "warn");
    gs.title = stopped ? "The plugin isn't running. Changes are saved, but nothing is applied or polled until it starts." : "";
    const g = gate();
    if (g && !stopped) {
      gs.textContent = GATE_LABEL[g.reason] || "Stopped by CoreScope";
      gs.className = "pill " + gateKind(g);
      gs.title = g.text || "";
    }
    renderCommunityNotice();
    renderCommunity();
    $("check-now").disabled = !!g;
    $("check-now").title = g ? `${g.text}. Check now is off.` : "";
    // Telemetry intervals faster than CoreScope's direct window can't be used.
    const directMin = ((community().policy || {}).direct_s || 0) / 60;
    [...$("interval_minutes").options].forEach((o) => {
      if (!o.dataset.label) o.dataset.label = o.textContent;
      const tooFast = directMin && !community().fast_mode && Number(o.value) < directMin;
      o.disabled = !!tooFast && o.value !== $("interval_minutes").value;
      o.textContent = o.dataset.label + (tooFast ? " (too fast for CoreScope)" : "");
    });
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
      $("status_every_hours").value = String(config.status_every_hours || 6);
      $("alternative_routes").checked = !!config.alternative_routes;
      $("guest_key_default").checked = guestDefault();
      const hops = String(config.max_hops ? 1 : 0);
      document.querySelectorAll('input[name="max_hops"]').forEach((el) => { el.checked = el.value === hops; });
    }
    const showTemp = tempSet().size > 0;
    document.querySelectorAll(".temp-col").forEach((el) => { el.hidden = !showTemp; });

    const sk = runtime.shared_key || {};
    const nTargets = (sk.targets || []).length;
    const skPill = $("shared-state");
    const corescopeSet = !!(runtime.corescope && runtime.corescope.url);
    // Pre-opt-in the plugin hasn't fetched the key (by design), so "not
    // checked" rather than claiming CoreScope serves none; "none served" is
    // only honest once a repeater is opted in and the fetch came back empty.
    skPill.textContent = sk.configured ? (nTargets ? `${nTargets} repeater${nTargets === 1 ? "" : "s"}${sk.kind === "own" ? " · polling key" : " · shared key"}` : "ready")
      : nTargets ? "No key from CoreScope"
      : corescopeSet ? "Not in use"
      : "No CoreScope set";
    skPill.className = "pill " + (sk.configured ? "good" : nTargets ? "warn" : "neutral");
    $("shared-name").textContent = sk.kind === "own" ? "This plugin's polling key (separate from the repeater's key)"
      : sk.name || (sk.configured ? "shared guest" : nTargets ? "none served" : "not checked yet");
    $("shared-ver").textContent = sk.kind === "own" ? (sk.expires_at ? `· registered, kept until ${String(sk.expires_at).replace("T", " ").slice(0, 16)}` : "· registered")
      : sk.configured ? `· v${sk.version || 0}` : "";
    $("shared-reg").textContent = sk.kind !== "own" && sk.register_error ? `Own key not registered: ${sk.register_error}. Using the shared key meanwhile.` : "";
    $("shared-pub").textContent = sk.public ? maskKey(sk.public.toUpperCase()) : "—";
    $("shared-from").textContent = sk.from || (config.corescope_url || "—");
    if (!dirty) {
      $("corescope_url").value = config.corescope_url || "";
      $("check_corescope").checked = config.check_corescope !== false;
    }

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
    // Polled repeaters first, then the rest; reachable before unreachable,
    // then nearest and strongest. Ordered by the saved selection, so a row
    // doesn't jump away while it is being ticked.
    const saved = new Set(config.targets || []);
    const nodes = (runtime.nodes || []).slice().sort((a, b) =>
      (saved.has(b.node_id) - saved.has(a.node_id)) || (reachable(b) - reachable(a))
      || (hopsOf(a) - hopsOf(b)) || ((b.snr ?? -99) - (a.snr ?? -99)));
    const firstOther = nodes.findIndex((n) => !saved.has(n.node_id));
    $("selected-count").textContent = `${chosen.size} selected`;
    $("nodes").innerHTML = nodes.map((n, i) => {
      const ok = reachable(n);
      const divider = i === firstOther && i > 0
        ? '<tr class="group-row"><td colspan="9" class="muted small">Not polled</td></tr>' : "";
      return `${divider}<tr class="${ok ? "" : "dim"}">
        <td><input type="checkbox" data-node="${esc(n.node_id)}" ${chosen.has(n.node_id) ? "checked" : ""} ${ok ? "" : "disabled"} aria-label="Poll ${esc(n.name)}"></td>
        <td><strong>${esc(n.name)}</strong><div class="muted small mono">${esc(n.node_id.slice(0, 8))}…${n.uptime_secs ? ` · up ${Math.floor(n.uptime_secs / 86400)} d` : ""}${(config.poll_intervals || {})[n.node_id] ? ` · ${everyText(config.poll_intervals[n.node_id])}` : ""}${keyBadge(n)}</div></td>
        <td>${routeCell(n)}</td>
        <td>${saved.has(n.node_id) && ok ? whoCell(n) : ""}</td>
        <td>${ago(n.last_seen)}</td>
        <td>${ok || n.role ? loginCell(n) : ""}</td>
        <td>${ok ? batteryCell(n) : ""}</td>
        <td class="temp-col" ${showTemp ? "" : "hidden"}>${ok ? tempCell(n) : ""}</td>
        <td><button class="button ghost small" type="button" data-details="${esc(n.node_id)}" ${ok ? "" : "disabled"} aria-label="Details for ${esc(n.name)}">Details</button></td></tr>`;
    }).join("") || '<tr><td colspan="9" class="muted">No repeaters heard yet.</td></tr>';
    $("reach-note").textContent = maxHops()
      ? "Repeaters heard direct, or through one relay you hear directly."
      : "Only repeaters heard directly (zero hops) can be selected. Set Reach to \"Up to 1 hop\" for more.";
  }

  function collect() {
    config.enabled = $("enabled").checked;
    config.interval_minutes = Number($("interval_minutes").value);
    config.status_every_hours = Number($("status_every_hours").value);
    config.brokers = [...document.querySelectorAll("[data-broker]")].filter((el) => el.checked).map((el) => el.dataset.broker);
    // Targets with a row take its tick; one just added has no row until saved.
    const boxes = [...document.querySelectorAll("[data-node]")];
    const shown = new Set(boxes.map((el) => el.dataset.node));
    config.targets = [...boxes.filter((el) => el.checked).map((el) => el.dataset.node),
      ...(config.targets || []).filter((k) => !shown.has(k))];
    config.alternative_routes = $("alternative_routes").checked;
    config.guest_key_default = $("guest_key_default").checked;
    config.max_hops = maxHops();
    config.temperature_targets = [...tempSet()];
    const url = $("corescope_url").value.trim().replace(/\/+$/, "");
    if (url && !/^https?:\/\//.test(url)) throw new Error("The CoreScope address must start with http:// or https://");
    config.corescope_url = url;  // empty = no CoreScope
    config.check_corescope = $("check_corescope").checked;
    config.community = $("community").checked;
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

  // liveView builds a live status panel once — a spinner, a title, a "Stop
  // watching" button, a body and a footer — and afterwards only updates their
  // contents, so the button is never re-created mid-tap.
  function liveView(box) {
    box.hidden = false;
    box.className = box.dataset.base;
    box.innerHTML = '<div class="lv-head"><span class="spin" aria-hidden="true"></span>'
      + '<span class="lv-title" role="status" aria-live="polite"></span>'
      + '<button type="button" class="button ghost small" data-stop>Stop watching</button></div>'
      + '<div class="lv-body"></div><div class="lv-foot muted small"></div>';
    const q = (sel) => box.querySelector(sel);
    let stopped = false;
    q("[data-stop]").addEventListener("click", (ev) => {
      stopped = true;
      ev.currentTarget.disabled = true;
      ev.currentTarget.textContent = "Stopping…";
    });
    return {
      get stopped() { return stopped; },
      title(html) { q(".lv-title").innerHTML = html; },
      body(html) { q(".lv-body").innerHTML = html; },
      foot(text) { q(".lv-foot").textContent = text || ""; },
      finish(kind) {
        const spin = q(".spin"); if (spin) spin.remove();
        const stop = q("[data-stop]"); if (stop) stop.remove();
        if (kind) box.classList.add(kind);
      },
    };
  }

  // A short message in a live panel (no spinner), with a close button.
  function panelNote(box, html, kind) {
    box.hidden = false;
    box.className = `${box.dataset.base} ${kind || ""}`;
    box.innerHTML = `<div class="lv-head"><span class="lv-title">${html}</span>`
      + '<button type="button" class="button ghost small" data-dismiss aria-label="Close">×</button></div>';
  }

  const nodeName = (id) => (((runtime.nodes || []).find((x) => x.node_id === id)) || {}).name || id.slice(0, 8);

  // Test one repeater, live in its dialog (the page's notice bar is hidden by
  // the modal): each step the plugin takes, then the answer and whether it
  // reached CoreScope.
  async function test(nodeId) {
    const run = ++testRun;
    const current = () => run === testRun;
    const name = nodeName(nodeId);
    const v = liveView($("test-status"));
    const btn = $("test-now");
    btn.disabled = true;
    const t0 = Date.now();
    let last = { steps: [] };
    let finished = false;
    const draw = () => {
      if (!current()) return;
      if (!finished) v.title(`Testing <strong>${esc(name)}</strong>… ${Math.round((Date.now() - t0) / 1000)}s`);
      const steps = last.steps || [];
      const now = (i) => !finished && i === steps.length - 1;  // the step still in progress
      v.body(steps.length ? '<ol class="steps">' + steps.map((st, i) =>
        `<li><span class="${now(i) ? "step-now" : "step-ok"}" aria-hidden="true">${now(i) ? "›" : "✓"}</span> ${esc(st.label)}</li>`).join("") + "</ol>" : "");
    };
    draw();
    const timer = setInterval(draw, 1000);
    try {
      const id = await request("test_login", nodeId);
      // Up to ~5 min: logins wait for their answer, then CoreScope is asked
      // up to three times before anything else is sent.
      const a = await waitForAction("test_login", id, 300000, (p) => { last = p; draw(); }, () => v.stopped || !current());
      if (!current()) return;
      last = a;
      finished = true;
      if (a.ok === false && a.message) { v.title(esc(a.message)); v.finish("warn"); return; }
      draw();
      v.title(((a.code === "ok" || a.code === "no_battery") && !a.covered ? "Answered — " : "") + esc(pollText(a)));
      v.foot(corescopeText(a.corescope));
      v.finish(pollKind(a));
    } catch (e) {
      if (!current()) return;
      v.title(e.aborted ? `Stopped watching <strong>${esc(name)}</strong>. The poll may still finish in the background.` : esc(e.message));
      v.finish(e.aborted ? "warn" : "bad");
    } finally {
      clearInterval(timer);
      if (current()) btn.disabled = !!testBlock(nodeId);
    }
  }

  // One row of the live Check-now panel.
  function progressRow(name, statusHtml, kind) {
    const icon = { good: "✓", bad: "✗", warn: "!", skip: "–" }[kind] || "";
    return `<div class="cp-row cp-${kind}"><span class="cp-icon" aria-hidden="true">${icon}</span>`
      + `<span class="cp-name">${esc(name)}</span><span class="cp-status">${statusHtml}</span></div>`;
  }

  // Check now first asks what to poll: just the stale repeaters (no reading in
  // the last hour) or all of them; runCheck does the chosen one.
  function checkNow() {
    const panel = $("check-progress");
    if (dirty) { panelNote(panel, "Save first, so the check uses your selection.", "warn"); return; }
    if (!(config.targets || []).length) { panelNote(panel, "Tick at least one repeater in the list first.", "warn"); return; }
    if (gate()) { panelNote(panel, `${esc(gate().text)}. Check now is off.`, gateKind(gate())); return; }
    panelNote(panel, "<strong>Check which repeaters?</strong>");
    panel.insertAdjacentHTML("beforeend", '<div class="cp-choice">'
      + '<button type="button" class="button ghost small" data-check="stale">Only stale <span class="muted small">(no fresh reading from anyone; standby left alone)</span></button>'
      + '<button type="button" class="button ghost small" data-check="all">All of them <span class="muted small">(within CoreScope\'s request limit)</span></button></div>');
  }

  async function runCheck(scope) {
    const nTargets = (config.targets || []).length;
    const v = liveView($("check-progress"));
    $("check-now").disabled = true;
    // Drawn from the plugin's snapshot: a row per repeater actually polled,
    // filled in (with CoreScope's confirmation) as each streams back.
    const draw = (a) => {
      const prog = a.progress || [];
      const total = a.total != null ? a.total : (scope === "stale" ? "the stale" : nTargets);
      const nSkipped = prog.filter((p) => p.skipped).length;
      v.title(`<strong>Checking ${total} repeater${total === 1 ? "" : "s"}…</strong> <span class="muted small">${prog.length - nSkipped} asked · ${nSkipped} skipped · up to a minute each</span>`);
      v.body(prog.map((p) => {
        if (p.skipped) return progressRow(p.name || nodeName(p.node_id), `Skipped: ${esc(p.message || p.skipped)}`, "skip");
        const cs = corescopeText(p.corescope);
        return progressRow(p.name || nodeName(p.node_id), esc(pollText(p)) + (cs ? ` <span class="muted small">· ${esc(cs)}</span>` : ""), pollKind(p));
      }).join(""));
    };
    draw({ progress: [] });
    try {
      const id = await request("check_now", null, { scope });
      const a = await waitForAction("check_now", id, 120000 * Math.max(1, nTargets) + 30000, draw, () => v.stopped);
      draw(a);
      const prog = a.progress || [];
      const nSkipped = prog.filter((p) => p.skipped).length;
      v.title(a.skipped && !prog.length ? esc(a.message || "")
        : `<strong>Checked ${prog.length} repeater${prog.length === 1 ? "" : "s"}.</strong> <span class="muted small">${prog.length - nSkipped} asked · ${nSkipped} skipped · ${esc(a.message || "")}</span>`);
      v.finish(a.ok === false ? (a.skipped ? gateKind({ reason: a.skipped }) : "bad") : "");
    } catch (e) {
      v.title(e.aborted ? "Stopped watching. Repeaters already queued finish in the background." : esc(e.message));
      v.finish(e.aborted ? "warn" : "bad");
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

  // Why Test can't run for a repeater (CoreScope's switches or exclusion), or "".
  function testBlock(nodeId) {
    const g = gate();
    if (g) return g.text;
    const p = community().policy || {};
    if ((p.excluded_nodes || []).includes(nodeId)) return "CoreScope excludes this repeater (owner's request)";
    return "";
  }

  const ROLE_TEXT = {
    polling: ["Polling", "good"], standby: ["Standby", "neutral"], independent: ["Independent", "neutral"],
    excluded: ["Excluded", "neutral"], paused: ["Paused", "bad"], version_disabled: ["Version disabled", "warn"],
    blocked: ["Blocked", "bad"], stale: ["Stale claim", "neutral"],
  };
  let monitorsRun = 0;

  // The Details dialog's Community block: every observer that claims this
  // repeater, from CoreScope's /api/nodes/{pk}/monitors (asked through the
  // plugin, like the CoreScope search).
  async function loadMonitors(nodeId) {
    const run = ++monitorsRun;
    const block = $("community-block");
    block.hidden = !(runtime.corescope && runtime.corescope.url);
    if (block.hidden) return;
    const body = $("monitors"), note = $("monitors-note");
    if (dirty) {
      body.innerHTML = "";
      note.textContent = "Save the page first to see who else monitors it.";
      return;
    }
    body.innerHTML = '<tr><td colspan="4" class="muted">Asking CoreScope…</td></tr>';
    note.textContent = "";
    try {
      const id = await request("monitors", nodeId);
      const a = await waitForAction("monitors", id, 30000, null, () => run !== monitorsRun);
      if (run !== monitorsRun || detailsTarget !== nodeId) return;
      if (a.ok === false) { body.innerHTML = `<tr><td colspan="4" class="muted">${esc(a.message)}</td></tr>`; return; }
      renderMonitors(nodeId, a.monitors || {});
    } catch (e) {
      if (run !== monitorsRun || e.aborted) return;
      body.innerHTML = `<tr><td colspan="4" class="muted">${esc(e.message)}</td></tr>`;
    }
  }

  function renderMonitors(nodeId, m) {
    const me = runtime.observer_id || "";
    const rows = m.monitors || [];
    $("monitors").innerHTML = rows.map((r) => {
      const mine = me && String(r.observer_id || "").toLowerCase() === me.toLowerCase();
      const hops = r.effective_hops != null ? r.effective_hops : r.hops;
      const via = r.via_name || (r.via ? String(r.via).slice(0, 8) : "");
      const route = hops ? `1 hop${via ? " via " + esc(via) : ""}` : "Direct";
      const [label, kind] = ROLE_TEXT[r.role] || [r.role || "—", "neutral"];
      const last = r.last_reading ? ago(Date.parse(r.last_reading) / 1000) : "never";
      return `<tr><td>${esc(r.name || String(r.observer_id || "").slice(0, 8))}${mine ? ' <span class="muted small">(this one)</span>' : ""}</td>
        <td>${route}</td><td><span class="pill ${kind}">${esc(label)}</span></td><td>${last}</td></tr>`;
    }).join("") || '<tr><td colspan="4" class="muted">No observer claims this repeater yet.</td></tr>';
    const p = community().policy || {};
    const notes = [];
    const meRow = rows.find((r) => me && String(r.observer_id || "").toLowerCase() === me.toLowerCase());
    const poller = rows.find((r) => r.role === "polling");
    if (meRow && meRow.role === "standby" && poller) {
      const w = (m.window_s || 0) + (p.grace_s || 0);
      notes.push(`This observer steps in if ${poller.name || "the poller"}'s newest reading is older than ${dur(w)} (window plus grace), or once ${poller.name || "it"} misses ${p.takeover_after || 2} windows in a row.`);
    } else if (meRow && meRow.role === "polling") {
      notes.push("This observer polls it; any others stand by.");
    }
    if (m.paused) notes.push("Polling is paused by CoreScope.");
    if (m.excluded) notes.push("CoreScope excludes this repeater.");
    (community().clamped || []).filter((c) => c.node_id === nodeId).forEach((c) => notes.push(clampText(c) + "."));
    $("monitors-note").textContent = notes.join(" ");
  }

  function openDetails(nodeId) {
    detailsTarget = nodeId;
    const n = (runtime.nodes || []).find((x) => x.node_id === nodeId);
    if (!n) return;
    $("details-title").textContent = n.name;
    $("test-status").hidden = true;  // clear any result from a previous test
    $("login-password").value = "";
    $("remove-password").checked = false;
    const pending = passwordUpdates[nodeId];
    $("remove-password").disabled = !(pending !== undefined ? pending : n.password_set);
    // Show whether a password is saved: the field is write-only (always blank),
    // so without this the operator can't tell a save worked. "" pending is a
    // queued removal; a non-empty string is a queued new password.
    const pwState = $("password-state");
    if (pending === "") { pwState.textContent = "Removing the password didn't save — press Apply to retry."; pwState.className = "password-state small"; }
    else if (pending) { pwState.textContent = "The new password didn't save — press Apply to retry."; pwState.className = "password-state small"; }
    else if (n.password_set) { pwState.textContent = "✓ A password is saved. Leave blank to keep it, type to replace it, or tick Remove."; pwState.className = "password-state small saved"; }
    else { pwState.textContent = "No password saved — logs in blank (guest or ACL)."; pwState.className = "password-state small muted"; }
    $("remove-manual").hidden = !n.manual;
    $("temp-this").checked = tempSet().has(nodeId);
    const own = (config.poll_intervals || {})[nodeId];
    $("node-interval").value = own ? String(own) : "default";
    // Choices faster than CoreScope's window for this repeater are greyed out.
    const floorMin = community().fast_mode ? 0 : windowFor(n) / 60;
    const oneHop = ((orderedRoutes(n)[0] || {}).hops || 0) > 0;
    const globalMin = config.interval_minutes || 60;
    [...$("node-interval").options].forEach((o) => {
      if (o.value === "default") {
        o.textContent = floorMin && globalMin < floorMin
          ? `Default (CoreScope: ${dur(floorMin * 60)} ${oneHop ? "at 1 hop" : "direct"})`
          : `Default (${everyText(globalMin)})`;
        return;
      }
      if (!o.dataset.label) o.dataset.label = o.textContent;
      const tooFast = floorMin && Number(o.value) < floorMin;
      o.disabled = !!tooFast && o.value !== $("node-interval").value;
      o.textContent = o.dataset.label + (tooFast ? " — faster than CoreScope allows" : "");
    });
    const blocked = testBlock(nodeId);
    $("test-now").disabled = !!blocked;
    $("test-gate").hidden = !blocked;
    $("test-gate").textContent = blocked ? `Test is off: ${blocked}.` : "";
    loadMonitors(nodeId);
    $("temp-last").textContent = n.temperature_c != null ? `Last: ${Number(n.temperature_c).toFixed(1)} °C, ${ago(n.temp_at)}`
      : n.temp_misses >= 3 ? "None reported so far" : "";

    // Shared-key opt-in: offered whenever a CoreScope address is set. The key
    // is only fetched once a repeater is opted in, so this must not wait for a
    // fetched key — otherwise the first repeater could never be opted in. A
    // saved password blocks it; an earlier admin answer stops it until cleared.
    const sk = runtime.shared_key || {};
    const pwSet = pending !== undefined ? !!pending : n.password_set;
    $("shared-block").hidden = !(runtime.corescope && runtime.corescope.url);
    $("shared-this").checked = guestFor(nodeId);
    guestShown = guestFor(nodeId);
    guestUseDefault = false;
    $("guest-use-default").hidden = (config.guest_key_overrides || {})[nodeId] === undefined;
    const gp = (config.guest_passwords || {})[nodeId];
    $("guest-password").value = gp === undefined ? "auto" : gp;
    $("guest-pw-state").textContent = n.guest_password === "hello" ? "✓ Last logged in with hello."
      : n.guest_password === "" ? "✓ Last logged in with a blank password." : "Not logged in under the guest key yet.";
    $("guest-pw-state").className = "password-state small " + (n.guest_password != null ? "saved" : "muted");
    showPasswordBlock();
    $("shared-this").disabled = pwSet || n.shared_admin_seen;
    $("shared-pw-warn").hidden = !pwSet;
    const adminWarn = $("shared-admin-warn");
    if (n.shared_admin_seen) {
      adminWarn.hidden = false;
      adminWarn.textContent = `This repeater answered the shared key as ADMIN. The shared key is stopped here and the key's access was ${n.shared_admin_removed ? "removed automatically" : "NOT removed — ask the owner to run setperm 0"}.`;
    } else {
      adminWarn.hidden = true;
    }
    $("node-packets").innerHTML = packetRows(n.packets, false);
    const routes = orderedRoutes(n);
    $("routes").innerHTML = routes.map((r, i) => {
      const last = routeResult(n, r, i);
      return `<tr><td class="muted">${i + 1}</td><td>${routeName(r)}</td><td>${r.adverts}</td><td>${r.snr == null ? "—" : `${Number(r.snr).toFixed(1)} dB`}</td><td>${last}</td></tr>`;
    }).join("") || '<tr><td colspan="5" class="muted">No usable route.</td></tr>';
    $("details-dialog").showModal();
  }

  // routeResult: the route's latest outcome and when.
  function routeResult(n, r, i) {
    const res = (n.route_results || {})[r.key];
    let html;
    if (res) {
      const ok = res[1] === "answered";
      html = `<span class="pill ${ok ? "good" : "bad"}">${ok ? "answered" : "no answer"}</span> <span class="muted small">${ago(res[0])}</span>`;
    } else if (r.key === n.last_route || (i === 0 && n.code === "no_reply")) {
      // Before route_results existed: what the repeater's own state says.
      html = n.code === "ok" ? '<span class="pill good">answered</span>' : '<span class="pill bad">no answer</span>';
    } else {
      html = '<span class="muted small">not tried</span>';
    }
    return html;
  }

  $("details-form").addEventListener("submit", (ev) => {
    // Enter in the password box submits with the first button, which is Apply.
    const action = (ev.submitter && ev.submitter.value) || "save";
    if (!detailsTarget || action === "cancel") return;  // dialog closes, nothing saved
    if (action === "test") { ev.preventDefault(); test(detailsTarget); return; }  // stays open for the live test
    const node = (runtime.nodes || []).find((x) => x.node_id === detailsTarget) || {};
    if (action === "remove") {
      if (!node.manual) { ev.preventDefault(); return; }  // only repeaters you added can be removed
      const id = detailsTarget;
      config.manual_repeaters = manualList().filter((m) => m.node_id !== id);
      config.targets = (config.targets || []).filter((k) => k !== id);
      document.querySelectorAll(`[data-node="${id}"]`).forEach((el) => { el.checked = false; });
    } else {  // Apply: stage this repeater's password / temperature / shared-key choices
      const guest = !$("shared-this").disabled && $("shared-this").checked;
      if (guest) {
        // Under the guest key the password is the guest one (blank or hello),
        // a plain setting; the companion password box is hidden and ignored.
        const gps = { ...(config.guest_passwords || {}) };
        const v = $("guest-password").value;
        if (v === "auto") delete gps[detailsTarget]; else gps[detailsTarget] = v;
        config.guest_passwords = gps;
      } else {
        const pw = $("login-password").value;
        if ($("remove-password").checked) passwordUpdates[detailsTarget] = "";
        else if (pw) passwordUpdates[detailsTarget] = pw;
      }
      const pis = { ...(config.poll_intervals || {}) };
      const iv = $("node-interval").value;
      if (iv === "default") delete pis[detailsTarget]; else pis[detailsTarget] = Number(iv);
      config.poll_intervals = pis;
      const temps = new Set(tempSet());
      if ($("temp-this").checked) temps.add(detailsTarget); else temps.delete(detailsTarget);
      tempTargets = temps;
      // Only a choice made here becomes an override; opening and applying
      // the dialog leaves a repeater following the default.
      const ovs = { ...(config.guest_key_overrides || {}) };
      if (guestUseDefault) delete ovs[detailsTarget];
      else if (!$("shared-this").disabled && $("shared-this").checked !== guestShown) ovs[detailsTarget] = $("shared-this").checked;
      config.guest_key_overrides = ovs;
    }
    // Save straight away — the repeater's own dialog shouldn't need a second
    // trip to the page's Save bar. The dialog closes (method="dialog"); a
    // failure keeps the edits staged (dirty) and says so by the Save bar.
    saveAll().catch((e) => { dirty = true; saveStatus(`Not saved: ${e.message}`, "bad"); });
  });
  // A guest-key repeater uses the guest password (blank or hello); the
  // companion-key password only matters when the guest key is off.
  function showPasswordBlock() {
    const guest = !$("shared-block").hidden && $("shared-this").checked && !$("shared-this").disabled;
    $("guest-pw-block").hidden = !guest;
    $("companion-pw-block").hidden = guest;
  }
  $("shared-this").addEventListener("change", () => { guestUseDefault = false; showPasswordBlock(); });
  $("guest-use-default").addEventListener("click", () => {
    guestUseDefault = true;
    $("shared-this").checked = guestDefault();
    $("guest-use-default").hidden = true;
    showPasswordBlock();
  });
  // Closing the dialog abandons a Test that is still being watched.
  $("details-dialog").addEventListener("close", () => { testRun++; monitorsRun++; });

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
    nodeId = String(nodeId || "").toLowerCase();
    if (!KEY_RE.test(nodeId)) { notice("That isn't a 64-character repeater key.", "warn"); return; }
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
  $("check-progress").addEventListener("click", (e) => {
    const choice = e.target.closest("[data-check]");
    if (choice) runCheck(choice.dataset.check);
    else if (e.target.closest("[data-dismiss]")) $("check-progress").hidden = true;
  });

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
  // A status line beside the Save button: the sticky bar is always on screen,
  // unlike the notice at the top of the page.
  function saveStatus(text, kind) {
    const el = $("save-status");
    el.textContent = text || "";
    el.className = "small " + (kind || "");
  }

  // saveAll persists everything pending (page settings + any staged per-repeater
  // password/temperature/shared changes). Used by the page's Save bar and by
  // the details dialog's Apply.
  async function saveAll() {
    collect();
    const extra = Object.keys(passwordUpdates).length ? { password_updates: passwordUpdates } : {};
    saveStatus("Saving…");
    await saveConfig(extra);
    passwordUpdates = {};
    tempTargets = null;
    dirty = false;
    saveStatus("Saved.", "good");
    refresh();  // show the saved state (e.g. "password set") straight away
  }

  $("save").addEventListener("click", async () => {
    try { await saveAll(); } catch (e) { saveStatus(`Not saved: ${e.message}`, "bad"); }
  });

  async function refresh() {
    try {
      await loadRuntime();
      if (!dirty) await loadConfig();
      render();
      // Keep an open repeater dialog's packet list current.
      const open = $("details-dialog").open && (runtime.nodes || []).find((x) => x.node_id === detailsTarget);
      if (open) $("node-packets").innerHTML = packetRows(open.packets, false);
    } catch (e) {
      notice(e.message, "bad");
    }
  }

  refresh();
  setInterval(refresh, 15000);
})();
