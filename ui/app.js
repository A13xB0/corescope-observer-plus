(() => {
  "use strict";

  const PLUGIN_ID = "openhop.corescope_observer_plus";
  const SETTINGS = "/api/plugins/settings";
  const RUNTIME = "/api/plugins/runtime";
  const $ = (id) => document.getElementById(id);

  let config = {};
  let loaded = {};  // settings as last read from or written to the plugin (see mergeLatest)
  let runtime = {};
  let dirty = false;
  let passwordUpdates = {};
  let tempTargets = null; // pending per-repeater temperature choices (a Set), null = unchanged
  let pendingTargets = null; // repeaters ticked but not saved (a Set), null = unchanged
  let pwApplied = {};  // passwords just saved, until the plugin's runtime catches up
  let detailsTarget = null;
  let testRun = 0;  // bumped per Test (and when the panel closes or changes): stale runs stop updating
  let testBusy = false;  // a Test is being watched in the open panel
  let checking = false;  // a Check now is being watched
  let filter = "all";  // the repeater filter chip
  let query = "";  // the repeater search box
  let view = "repeaters";  // the open tab
  let saveMsg = {};  // what the Save bar last reported
  let saveMsgTimer = 0;
  let othersOpened = false;  // Not polled opened itself once, with nothing selected
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
  let guestShown = false;  // the panel's guest key switch as opened
  let guestUseDefault = false;  // Use the default was pressed in the panel
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
    return (CODE_TEXT[x.code] || x.code || "no answer") + (x.message && x.message !== x.code ? ": " + skipText(x.message) : "");
  }
  function pollKind(x) {
    return x.code === "ok" || x.code === "no_battery" ? (x.role === "admin" ? "warn" : "good") : "bad";
  }

  // This station, as CoreScope names it: its reading is "you read it".
  const isMe = (name) => {
    const me = String((runtime.repeater || {}).name || "").trim().toLowerCase();
    return !!me && String(name || "").trim().toLowerCase() === me;
  };
  // The plugin's skip reasons, in the page's words.
  function skipText(text) {
    let s = String(text || "").replace(/you're standby/g, "you're the backup").replace(/\bstandby\b/gi, "backup");
    const me = (runtime.repeater || {}).name;
    if (me) s = s.split(`${me} sent a reading`).join("you sent a reading");
    return s;
  }

  // corescopeText turns a CoreScope confirmation (action.corescope / a
  // progress row's .corescope) into a short "did it land?" line.
  function corescopeText(cs) {
    if (!cs) return "";
    if (!cs.reachable) return "CoreScope unreachable";
    if (!cs.latest_ts) return "CoreScope has nothing for it yet";
    const by = cs.by ? ` (${isMe(cs.by) ? "you" : cs.by})` : "";
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

  const maxHops = () => Number($("max_hops").value || 0);
  const tempSet = () => tempTargets || new Set(config.temperature_targets || []);
  const chosenSet = () => pendingTargets || new Set(config.targets || []);
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

  // A password waiting to save, or just saved, beats the plugin's last report.
  function pwSetFor(n) {
    const pending = passwordUpdates[n.node_id];
    if (pending !== undefined) return !!pending;
    const applied = pwApplied[n.node_id];
    return applied ? applied.set : !!n.password_set;
  }

  // Route, as the row and panel name it.
  function routeText(n) {
    if (!(n.routes || []).length) return "2+ hops";
    const best = orderedRoutes(n)[0];
    return best ? routeName(best) : "1 hop";
  }
  const unreachableWhy = (n) => ((n.routes || []).length ? 'Set Reach to "Direct and 1 hop"' : "2+ hops, beyond reach");

  const volts = (mv) => `${(mv / 1000).toFixed(2)} V`;
  const gaugePct = (mv) => Math.max(0, Math.min(100, Math.round((mv - 3300) / 9)));

  // The battery figure and gauge. A reading that isn't a fresh one of ours
  // (another observer's, or an old one before a failed poll) is shown muted.
  function battHtml(n) {
    if (n.code === "no_battery") return '<span class="batt"><span class="muted small">No battery fitted</span></span>';
    if (!n.battery_mv) return '<span class="batt"><span class="muted small">No reading</span></span>';
    const mine = n.code === "ok" && !n.covered;
    return `<span class="batt${mine ? "" : " old"}"><b>${volts(n.battery_mv)}</b>`
      + `<span class="gauge"><i class="${n.battery_mv < 3600 ? "low" : ""}" style="width:${gaugePct(n.battery_mv)}%"></i></span></span>`;
  }

  function batteryFact(n) {
    if (n.code === "no_battery") return "No battery fitted";
    if (n.covered && n.code === "ok") {
      // Not our reading: another observer overheard the answer to our request.
      return `${n.battery_mv ? volts(n.battery_mv) : "—"} · heard via another observer · ${ago(n.covered_at)}`;
    }
    if (n.code === "ok" && n.battery_mv) {
      return `${volts(n.battery_mv)} · ${ago(n.last_success)}${n.method === "telemetry" ? " · from telemetry, as this repeater ignores a guest's status request" : ""}`;
    }
    if (!n.code || n.code === "ok") return "No reading";
    const failed = esc(CODE_TEXT[n.code] || n.code) + (n.detail ? `: ${esc(n.detail)}` : "");
    return n.battery_mv ? `${volts(n.battery_mv)} · ${ago(n.last_success)} · ${failed}` : failed;
  }

  function tempText(n) {
    if (!tempSet().has(n.node_id)) return "Off";
    if (n.temp_code === "ok" || (n.temperature_c != null && n.temp_misses < 3)) {
      return n.temperature_c == null ? "—" : `${Number(n.temperature_c).toFixed(1)} °C · ${ago(n.temp_at)}`;
    }
    return n.temp_misses >= 3 ? "None reported" : "—";
  }

  function loginText(n) {
    const pw = pwSetFor(n);
    if (n.role) return esc(n.role) + (pw ? " · password set" : "");
    return pw ? "Password set" : "Automatic";
  }

  // What's wrong with a polled repeater, if anything: drives its red tag, the
  // Needs attention filter and the need attention figure.
  function trouble(n) {
    if (!reachable(n)) return { text: "Out of reach", title: unreachableWhy(n) };
    if (n.shared_admin_seen) return { text: "Admin!", title: "Answered the guest key as admin; guest polling stopped here" };
    if (n.flood_replies >= 2) return { text: "Floods · daily", title: "Its replies keep coming back flooded (our return route isn't taking), so it is polled only once a day until a reply comes back direct" };
    if (n.code === "no_reply") {
      if (n.no_reply_count < 2) return null;
      return { text: n.no_reply_count === 2 ? "No answer twice" : `No answer ×${n.no_reply_count}`, title: n.detail || "" };
    }
    if (!n.code || n.code === "ok" || n.code === "no_battery") return null;
    return { text: CODE_TEXT[n.code] || n.code, title: n.detail || "" };
  }

  function keyTag(n) {
    if (n.key_mode === "shared") {
      const own = (runtime.shared_key || {}).kind === "own";
      return `<span class="pill" title="${own ? "Polled under this plugin's separate polling key (not the repeater's key), registered with CoreScope" : "Polled under the public shared guest key"}">${own ? "Guest key" : "Shared key"}</span>`;
    }
    if (pwSetFor(n)) return '<span class="pill">Password</span>';
    if (n.shared_opt_in) return '<span class="pill" title="Set to use it, but the guest key is not usable here yet">Guest key off</span>';
    return '<span class="pill">Companion key</span>';
  }

  function rowTags(n, polled) {
    const tags = [];
    const t = polled ? trouble(n) : null;
    if (t) tags.push(`<span class="pill bad" title="${esc(t.title)}">${esc(t.text)}</span>`);
    if (polled && n.code === "ok" && n.retried) tags.push('<span class="pill warn">Retry used</span>');
    if (n.role === "admin") tags.push('<span class="pill warn">Admin login</span>');
    if (polled) tags.push(keyTag(n));
    const ov = (config.guest_key_overrides || {})[n.node_id];
    if (ov !== undefined) tags.push(`<span class="pill warn" title="Set by hand in its details: guest key ${ov ? "on" : "off"}. The default doesn't change it until you press Use the default.">Overridden</span>`);
    const own = (config.poll_intervals || {})[n.node_id];
    if (own) tags.push(`<span class="pill">${esc(everyText(own).replace(/^./, (c) => c.toUpperCase()))}</span>`);
    if (n.manual) tags.push('<span class="pill">Added by you</span>');
    return tags.join("");
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

  // Who polls a repeater, as [tag colour, tag, line under it]. Uses the backend's
  // who values (you, standby, covered, capped, excluded); on the page the word
  // is Backup.
  function whoLine(n) {
    const id = n.node_id;
    const saved = (config.targets || []).includes(id);
    const picked = chosenSet().has(id);
    const c = n.community;
    const excluded = (c && c.who === "excluded") || ((community().policy || {}).excluded_nodes || []).includes(id);
    if (saved && !picked) return ["", "Not selected", "Save to stop polling it"];
    if (!saved && picked) return ["info", "Selected", "Save to start polling it"];
    if (!saved) return ["", "Not polled", excluded ? "Excluded by CoreScope" : reachable(n) ? "Not selected" : unreachableWhy(n)];
    if (!reachable(n)) return ["warn", "Not polled", unreachableWhy(n)];
    const g = gate();
    if (g) {
      return [gateKind(g), GATE_SHORT[g.reason] || "Stopped",
        g.reason === "paused" ? "Nothing is sent until CoreScope lifts it"
          : g.reason === "blocked" ? "Nothing is sent from this station" : "This version is turned off"];
    }
    if (excluded) return ["warn", "Not polled", "Excluded by CoreScope"];
    if (!c) return ["", "Selected", "Waiting for the plugin"];
    if (c.who === "standby") return ["info", `${c.by || "Another observer"} polls it`, "You're the backup if it goes quiet"];
    const next = hhmm(Math.max(c.next_at || 0, Date.now() / 1000));
    if (c.who === "capped") return ["warn", "Limit reached", `${skipText((c.skip || {}).text)}; next ${next}`];
    if (!config.enabled) return ["good", "You poll it", "Polling is off"];
    if (c.who === "covered") {
      const reader = !c.fresh_by ? "Another observer" : isMe(c.fresh_by) ? "you" : c.fresh_by;
      return ["good", "You poll it", `${reader} read it ${ago(c.fresh_at)}, so next ${next}`];
    }
    return ["good", "You poll it", `Next ${next}`];
  }

  // The banner for CoreScope's switches: pause, block, version minimum. Each
  // says that Check now and Test are off, as a disabled button can't on a phone.
  function renderCommunityNotice() {
    const el = $("community-notice");
    const cm = community();
    const p = cm.policy || {};
    const g = gate();
    if (!g) { el.hidden = true; el.innerHTML = ""; return; }
    let html;
    if (g.reason === "paused") {
      html = `<b>Paused by CoreScope${p.paused_reason ? ": " + esc(p.paused_reason) : ""}.</b>`
        + `<span class="small">Nothing is sent ${p.paused_until ? `until ${esc(whenText(p.paused_until))}, or ` : ""}until CoreScope lifts the pause. You don't need to do anything. Claims still go out, so CoreScope shows this observer as paused. Check now and Test are off.</span>`;
    } else if (g.reason === "blocked") {
      html = "<b>CoreScope has stopped this observer from polling.</b>"
        + `<span class="small">This observer's key is on ${esc(hostOf(cm.corescope_url))}'s blocked list, so the plugin sends nothing and CoreScope ignores its readings. Ask the CoreScope operator why; nothing on this side can lift it. Check now and Test are off.</span>`;
    } else {
      html = `<b>This version (${esc(runtime.plugin_version || "?")}) is turned off. Upgrade to ${esc(p.min_plugin_version || "a newer version")} or later to keep providing data.</b>`
        + `<span class="small">${p.min_version_reason ? `CoreScope says: ${esc(p.min_version_reason)} ` : ""}Until you upgrade, nothing is sent and CoreScope ignores this observer's claims and readings. Your settings and passwords are kept. Check now and Test are off.</span>`;
    }
    el.hidden = false;
    el.className = `notice ${gateKind(g)}`;
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
      [dur(p.direct_s), "direct"], [dur(p.one_hop_s), "1 hop"], [dur(p.grace_s), "backup wait"],
      [String(p.max_requests), "per window"],
    ].map(([v, l]) => `<div><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("") : "";
    const where = hostOf(cm.corescope_url);
    $("community-source").textContent = !cm.corescope_url ? "Built-in limits: no CoreScope address set."
      : cm.has_policy ? `Limits set by ${where}${p.version ? " · policy " + p.version : ""}. Checked ${ago(cm.fetched_at)}${cm.last_error ? ` (last try: ${cm.last_error}; using the saved copy)` : ""}.`
      : `Built-in limits: no policy from ${where} yet${cm.last_error ? ` (${cm.last_error})` : ""}.`;
    const warn = [];
    if (cm.stale) warn.push("CoreScope hasn't answered for over 6 hours: every repeater is polled at twice its window until it does.");
    (cm.clamped || []).filter((c) => !c.node_id).forEach((c) => warn.push(clampText(c) + "."));
    if (cm.fast_mode) warn.push("Test fast mode is on (OPENHOP_OBSERVER_PLUS_TEST_FAST): for bench runs only.");
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
  // Rows for the Activity table, newest first.
  function packetRows(entries) {
    const log = (entries || []).slice().reverse();
    return log.length ? log.map((e) => `<tr>
      <td class="t">${new Date(e.at * 1000).toLocaleTimeString()}<div>${ago(e.at)}</div></td>
      <td>${esc(e.name || String(e.node_id || "").slice(0, 8))}</td>
      <td><strong>${esc(e.kind)}</strong>${e.detail ? `<div class="muted small">${esc(e.detail)}</div>` : ""}${e.unconfirmed ? '<div class="muted small">openHop busy: sent unconfirmed</div>' : ""}</td>
      <td class="small">${esc(e.route || "")}</td>
      <td class="small">${esc(e.key || "")}</td>
      <td class="wrap">${packetAnswer(e)}</td></tr>`).join("")
      : '<tr><td colspan="6" class="muted">Nothing sent yet.</td></tr>';
  }
  // One repeater's log for the details panel, too narrow for the table.
  function packetItems(entries) {
    const log = (entries || []).slice().reverse();
    return log.map((e) => `<div>
      <span><span class="mono muted">${new Date(e.at * 1000).toLocaleTimeString()}</span> <strong>${esc(e.kind)}</strong>${e.detail ? ` <span class="muted">${esc(e.detail)}</span>` : ""}</span>
      <span class="muted">${esc([e.route, e.key].filter(Boolean).join(" · "))}${e.unconfirmed ? " · openHop busy: sent unconfirmed" : ""}</span>
      <span>${packetAnswer(e)}</span></div>`).join("") || '<span class="muted">Nothing sent yet.</span>';
  }
  function renderPackets() {
    $("packets").innerHTML = packetRows(runtime.packet_log);
    renderActivity();
  }
  // Counts for the station's day. A flood is an answer that came back flooded
  // because the repeater had no route to us.
  function renderActivity() {
    const a = runtime.activity;
    if (!a) return;
    const t = a.today, y = a.yesterday, w = a.week;
    const asked = t.answered + t.no_answer + t.refused;
    $("a-answered").textContent = t.answered;
    $("a-none").textContent = t.no_answer + t.refused;
    $("a-rate").textContent = asked ? `${Math.round(t.answered * 100 / asked)}%` : "—";
    $("a-floods").textContent = t.flooded;
    const by = (t.flooded_by || []).map((n) => `${n.name || "Unknown"} ×${n.count}`).join(", ");
    $("a-floodby").hidden = !by;
    $("a-floodby").textContent = by ? `Flooded back today: ${by}` : "";
    $("a-history").textContent = `Yesterday: ${y.answered} successful, ${y.flooded} flood${y.flooded === 1 ? "" : "s"}. Last 7 days: ${w.answered} successful, ${w.flooded} flood${w.flooded === 1 ? "" : "s"}.`;
  }

  // The status beside Check now, as [text, kind, tooltip].
  function statusLine(checks, allOk) {
    // The plugin rewrites runtime.json every few seconds while it runs; a
    // stale one means it is stopped and nothing saved here is acted on yet.
    const age = runtime.generated_at ? Date.now() / 1000 - Date.parse(runtime.generated_at) / 1000 : 0;
    if (age > 20 * 60) {
      return [`Not running (last seen ${ago(Date.parse(runtime.generated_at) / 1000)})`, "bad",
        "The plugin isn't running. Changes are saved, but nothing is applied or polled until it starts."];
    }
    const g = gate();
    if (g) return [GATE_LABEL[g.reason] || "Stopped by CoreScope", gateKind(g), g.text || ""];
    if (!Object.keys(checks).length) return [runtime.token_configured ? "Starting" : "Needs setup", runtime.token_configured ? "neutral" : "warn", ""];
    if (!allOk) return [needsSetup(checks) ? "Needs setup" : "Needs attention", "warn", ""];
    return config.enabled ? ["Polling normally", "good", ""] : ["Polling is off", "neutral", ""];
  }
  const needsSetup = (checks) => !runtime.token_configured || !!(checks.companion && !checks.companion.ok);

  function subtitleText() {
    const r = runtime.repeater || {};
    const cm = community();
    const parts = [];
    if (r.name) parts.push(`Publishing as ${r.name}${r.iata ? " · " + r.iata : ""}`);
    if (!(runtime.corescope && runtime.corescope.url)) parts.push("No CoreScope set");
    else if (cm.stale || (cm.last_error && !cm.has_policy)) parts.push("CoreScope not answering");
    else parts.push("CoreScope connected");
    return parts.join(" · ");
  }

  function render() {
    renderPackets();
    $("version").textContent = runtime.plugin_version ? `v${runtime.plugin_version}` : "";
    const checks = runtime.self_check || {};
    const allOk = Object.keys(checks).length && Object.values(checks).every((c) => c.ok);
    const [statusText, statusKind, statusTitle] = statusLine(checks, allOk);
    $("global-status-text").textContent = statusText;
    $("global-status").className = "status " + statusKind;
    $("global-status").title = statusTitle;
    $("subtitle").textContent = subtitleText();
    $("setup-notice").hidden = !needsSetup(checks);
    const g = gate();
    renderCommunityNotice();
    renderCommunity();
    $("check-now").disabled = !!g || checking;
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
      `<div class="obs"><span>${esc(LABELS[k] || k)}<span class="sub">${esc(c.message)}</span></span><span class="pill ${c.ok ? "good" : "bad"}">${c.ok ? "ok" : "failed"}</span></div>`).join("");
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
      showClearOverrides();
      $("max_hops").value = String(config.max_hops ? 1 : 0);
    }

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

    // While editing, keep what's ticked on screen rather than the saved list.
    const brokerBoxes = [...document.querySelectorAll("[data-broker]")];
    const chosenBrokers = new Set(dirty && brokerBoxes.length
      ? brokerBoxes.filter((el) => el.checked).map((el) => el.dataset.broker) : config.brokers || []);
    $("brokers").innerHTML = (runtime.brokers || []).map((b) => {
      const state = !b.enabled ? '<span class="pill">disabled</span>' : b.openhop_connected ? '<span class="pill good">connected</span>' : '<span class="pill warn">not connected</span>';
      const last = b.last_error ? `Last publish failed: ${esc(b.last_error)}`
        : b.last_ok ? `Last publish ${ago(b.last_ok)} · ${b.last_count} reading(s)` : "Nothing published yet";
      return `<label class="broker"><input type="checkbox" data-broker="${esc(b.name)}" ${chosenBrokers.has(b.name) ? "checked" : ""} aria-label="Publish to ${esc(b.name)}">
        <strong>${esc(b.name)}</strong>${state}
        <span class="sub">${esc(b.host)} · ${esc(b.format)}</span>
        <span class="sub">${last}${b.queued ? ` · ${b.queued} queued` : ""}</span></label>`;
    }).join("") || '<p class="muted small">No brokers configured in openHop.</p>';

    renderRepeaters();
    updateSaveBar();
  }

  const polledByYou = (n) => n.community && ["you", "covered", "capped"].includes(n.community.who);
  const polledByOthers = (n) => n.community && n.community.who === "standby";

  function rowHtml(n, picked, saved) {
    const ok = reachable(n);
    const id = esc(n.node_id);
    const [kind, tag, line] = whoLine(n);
    const when = saved
      ? (n.covered && n.covered_at ? n.covered_at : n.code === "ok" || n.battery_mv ? n.last_success : n.last_seen)
      : n.last_seen;
    const parts = [routeText(n), when ? (saved && when !== n.last_seen ? ago(when) : `heard ${ago(when)}`) : ""];
    if (saved && tempSet().has(n.node_id) && n.temperature_c != null) parts.push(`${Number(n.temperature_c).toFixed(1)} °C`);
    return `<div class="row${ok ? "" : " dim"}${detailsTarget === n.node_id ? " current" : ""}">
      <label class="tick"><input type="checkbox" data-node="${id}" ${picked ? "checked" : ""} ${ok ? "" : "disabled"} aria-label="Poll ${esc(n.name)}"></label>
      <button type="button" class="row-main" data-details="${id}" aria-current="${detailsTarget === n.node_id}" ${ok ? "" : "disabled"}>
        ${battHtml(n)}
        <span><span class="name">${esc(n.name)}</span><span class="sub">${parts.filter(Boolean).join(" · ")}</span></span>
        <span class="who"><span class="pill ${kind}">${esc(tag)}</span><span class="sub">${esc(line)}</span></span>
        <span class="tags">${rowTags(n, saved)}</span>
      </button></div>`;
  }

  // The repeater list: what's selected, then the rest under Not polled. Ordered
  // by the saved selection, so a row doesn't jump away while it is being ticked.
  // Ticks live in pendingTargets, so a filter hiding a row can't lose one.
  function renderRepeaters() {
    const focus = document.activeElement;
    const refocus = focus && focus.dataset
      ? ["node", "details", "f"].filter((k) => focus.dataset[k]).map((k) => `[data-${k}="${focus.dataset[k]}"]`)[0] : null;

    const chosen = chosenSet();
    const saved = new Set(config.targets || []);
    const hopsOf = (n) => (n.hops == null ? 9 : n.hops);
    const nodes = (runtime.nodes || []).slice().sort((a, b) =>
      (reachable(b) - reachable(a)) || (hopsOf(a) - hopsOf(b)) || ((b.snr ?? -99) - (a.snr ?? -99)));
    const polled = nodes.filter((n) => saved.has(n.node_id));
    const rest = nodes.filter((n) => !saved.has(n.node_id));
    const usable = polled.filter(reachable);
    const you = usable.filter(polledByYou), others = usable.filter(polledByOthers);
    const attention = polled.filter(trouble);

    const counts = { all: polled.length, you: you.length, other: others.length, attention: attention.length };
    const names = { all: "All", you: "You poll", other: "Others poll", attention: "Needs attention" };
    $("filters").innerHTML = Object.keys(names).map((k) =>
      `<button type="button" class="chip" data-f="${k}" aria-pressed="${filter === k}">${names[k]} ${counts[k]}</button>`).join("");

    const q = query.trim().toLowerCase();
    const found = (n) => !q || String(n.name || "").toLowerCase().includes(q) || n.node_id.startsWith(q);
    const inFilter = { all: polled, you, other: others, attention }[filter];
    const shown = inFilter.filter(found);
    const hidden = rest.filter(found);
    $("nodes").innerHTML = shown.map((n) => rowHtml(n, chosen.has(n.node_id), true)).join("")
      || `<p class="muted empty">${!nodes.length ? "No repeaters heard yet."
        : q || polled.length ? "Nothing here right now."
          : "No repeaters selected yet. Tick some under Not polled."}</p>`;

    const box = $("others");
    box.hidden = filter !== "all" || !hidden.length;
    $("others-count").textContent = String(hidden.length);
    $("others-list").innerHTML = hidden.map((n) => rowHtml(n, chosen.has(n.node_id), false)).join("");
    if (q) box.open = true;
    else if (!polled.length && !othersOpened && rest.length) { box.open = true; othersOpened = true; }

    const stopped = !!gate() || !config.enabled;
    const next = you.map((n) => n.community.next_at).filter(Boolean).sort((a, b) => a - b)[0];
    $("g-you").textContent = you.length;
    $("g-others").textContent = others.length;
    $("g-attention").textContent = attention.length;
    $("g-next").textContent = stopped || !next ? "—" : hhmm(Math.max(next, Date.now() / 1000));

    $("reach-note").textContent = `${chosen.size} selected. ` + (maxHops()
      ? "Repeaters heard direct, or through one relay you hear directly."
      : "Only repeaters heard directly (zero hops) can be selected. Set Reach to \"Direct and 1 hop\" for more.");

    if (refocus) {
      const el = document.querySelector(refocus);
      if (el) el.focus({ preventScroll: true });
    }
  }

  function collect() {
    config.enabled = $("enabled").checked;
    config.interval_minutes = Number($("interval_minutes").value);
    config.status_every_hours = Number($("status_every_hours").value);
    config.brokers = [...document.querySelectorAll("[data-broker]")].filter((el) => el.checked).map((el) => el.dataset.broker);
    config.targets = [...chosenSet()];
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
          body: JSON.stringify({ name: "CoreScope Observer Plus plugin" }),
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

  // Test one repeater, live in its panel: each step the plugin takes, then the
  // answer and whether it reached CoreScope.
  async function test(nodeId) {
    const run = ++testRun;
    const current = () => run === testRun;
    const name = nodeName(nodeId);
    const v = liveView($("test-status"));
    const btn = $("test-now");
    btn.disabled = true;
    testBusy = true;
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
      if (a.ok === false && a.message) { v.title(esc(skipText(a.message))); v.finish("warn"); return; }
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
      if (current()) { testBusy = false; btn.disabled = !!testBlock(nodeId); }
    }
  }

  // One row of the live Check-now panel.
  function progressRow(name, statusHtml, kind) {
    const icon = { good: "✓", bad: "✗", warn: "!", skip: "–" }[kind] || "";
    return `<div class="cp-row cp-${kind}"><span class="cp-icon" aria-hidden="true">${icon}</span>`
      + `<span class="cp-name">${esc(name)}</span><span class="cp-status">${statusHtml}</span></div>`;
  }

  // Check now first asks what to poll: just the stale repeaters (no reading in
  // the last hour) or all of them; runCheck does the chosen one. Its panel sits
  // under the header, so it can be watched from any tab.
  function checkNow() {
    const panel = $("check-progress");
    if (dirty) { panelNote(panel, "Save first, so the check uses your selection.", "warn"); return; }
    if (!(config.targets || []).length) { panelNote(panel, "Tick at least one repeater in the list first.", "warn"); return; }
    if (gate()) { panelNote(panel, `${esc(gate().text)}. Check now is off.`, gateKind(gate())); return; }
    panelNote(panel, "<strong>Check which repeaters?</strong>");
    panel.insertAdjacentHTML("beforeend", '<div class="cp-choice">'
      + '<button type="button" class="button ghost small" data-check="stale">Only stale <span class="muted small">(no fresh reading from anyone; backups left alone)</span></button>'
      + '<button type="button" class="button ghost small" data-check="all">All of them <span class="muted small">(within CoreScope\'s request limit)</span></button></div>');
  }

  async function runCheck(scope) {
    const nTargets = (config.targets || []).length;
    const v = liveView($("check-progress"));
    checking = true;
    $("check-now").disabled = true;
    // Drawn from the plugin's snapshot: a row per repeater actually polled,
    // filled in (with CoreScope's confirmation) as each streams back.
    const draw = (a) => {
      const prog = a.progress || [];
      const total = a.total != null ? a.total : (scope === "stale" ? "the stale" : nTargets);
      const nSkipped = prog.filter((p) => p.skipped).length;
      v.title(`<strong>Checking ${total} repeater${total === 1 ? "" : "s"}…</strong> <span class="muted small">${prog.length - nSkipped} asked · ${nSkipped} skipped · up to a minute each</span>`);
      v.body(prog.map((p) => {
        if (p.skipped) return progressRow(p.name || nodeName(p.node_id), `Skipped: ${esc(skipText(p.message || p.skipped))}`, "skip");
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
      v.title(a.skipped && !prog.length ? esc(skipText(a.message))
        : `<strong>Checked ${prog.length} repeater${prog.length === 1 ? "" : "s"}.</strong> <span class="muted small">${prog.length - nSkipped} asked · ${nSkipped} skipped · ${esc(skipText(a.message))}</span>`);
      v.finish(a.ok === false ? (a.skipped ? gateKind({ reason: a.skipped }) : "bad") : "");
    } catch (e) {
      v.title(e.aborted ? "Stopped watching. Repeaters already queued finish in the background." : esc(e.message));
      v.finish(e.aborted ? "warn" : "bad");
    } finally {
      checking = false;
      $("check-now").disabled = !!gate();
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
    if ((p.excluded_nodes || []).includes(nodeId)) return "Excluded by CoreScope";
    return "";
  }

  const ROLE_TEXT = {
    polling: ["Polling", "good"], standby: ["Backup", "neutral"], independent: ["Independent", "neutral"],
    excluded: ["Excluded", "neutral"], paused: ["Paused", "bad"], version_disabled: ["Version disabled", "warn"],
    blocked: ["Blocked", "bad"], stale: ["Stale claim", "neutral"],
  };
  let monitorsRun = 0;

  // The panel's Who polls it list: every observer that claims this repeater,
  // from CoreScope's /api/nodes/{pk}/monitors (asked through the plugin, like
  // the CoreScope search).
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
    body.innerHTML = '<span class="muted small">Asking CoreScope…</span>';
    note.textContent = "";
    try {
      const id = await request("monitors", nodeId);
      const a = await waitForAction("monitors", id, 30000, null, () => run !== monitorsRun);
      if (run !== monitorsRun || detailsTarget !== nodeId) return;
      if (a.ok === false) { body.innerHTML = `<span class="muted small">${esc(a.message)}</span>`; return; }
      renderMonitors(nodeId, a.monitors || {});
    } catch (e) {
      if (run !== monitorsRun || e.aborted) return;
      body.innerHTML = `<span class="muted small">${esc(e.message)}</span>`;
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
      return `<div class="obs"><span>${mine ? "This station" : esc(r.name || String(r.observer_id || "").slice(0, 8))} <span class="muted small">${route}</span>
        <span class="sub">Last reading ${last}</span></span><span class="pill ${kind}">${esc(label)}</span></div>`;
    }).join("") || '<span class="muted small">No observer claims this repeater yet.</span>';
    const p = community().policy || {};
    const notes = [];
    const meRow = rows.find((r) => me && String(r.observer_id || "").toLowerCase() === me.toLowerCase());
    const poller = rows.find((r) => r.role === "polling");
    if (meRow && meRow.role === "standby" && poller) {
      const w = (m.window_s || 0) + (p.grace_s || 0);
      notes.push(`This observer steps in if ${poller.name || "the poller"}'s newest reading is older than ${dur(w)} (window plus backup wait), or once ${poller.name || "it"} misses ${p.takeover_after || 2} windows in a row.`);
    } else if (meRow && meRow.role === "polling") {
      notes.push("This observer polls it; any others are backups.");
    }
    if (m.paused) notes.push("Polling is paused by CoreScope.");
    if (m.excluded) notes.push("CoreScope excludes this repeater.");
    (community().clamped || []).filter((c) => c.node_id === nodeId).forEach((c) => notes.push(clampText(c) + "."));
    $("monitors-note").textContent = notes.join(" ");
  }

  // What the panel reports, refreshed with each runtime read. The form below it
  // is left alone, so a refresh can't touch a half-made change.
  function fillFacts(n) {
    const best = orderedRoutes(n)[0];
    const notes = [];
    if (n.retried && n.last_route_label && n.code === "ok") notes.push(`last answered ${esc(n.last_route_label)}`);
    const alt = orderedRoutes(n).length - 1;
    if (alt > 0) notes.push(`${alt} other route${alt > 1 ? "s" : ""}`);
    const facts = [["Battery", batteryFact(n)], ["Temperature", tempText(n)],
      ["Route", routeText(n) + (notes.length ? ` · ${notes.join(" · ")}` : "")]];
    if (best && snrText(best.snr)) facts.push(["Signal", `${Number(best.snr).toFixed(1)} dB SNR`]);
    facts.push(["Login", loginText(n)], ["Last heard", ago(n.last_seen)]);
    if (n.uptime_secs) facts.push(["Uptime", `${Math.floor(n.uptime_secs / 86400)} d`]);
    $("details-facts").innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    const [kind, tag, line] = whoLine(n);
    $("details-who").innerHTML = `<span class="pill ${kind}">${esc(tag)}</span><span class="sub">${esc(line)}</span>`;
    $("node-packets").innerHTML = packetItems(n.packets);
    $("routes").innerHTML = orderedRoutes(n).map((r, i) => `<div class="obs"><span>${i + 1}. ${routeName(r)}
        <span class="sub">${r.adverts} advert${r.adverts === 1 ? "" : "s"}${r.snr == null ? "" : ` · ${Number(r.snr).toFixed(1)} dB SNR`}</span></span>
      <span>${routeResult(n, r, i)}</span></div>`).join("") || '<span class="muted small">No usable route.</span>';
  }

  function guestStateText(n) {
    const el = $("guest-state");
    if (pwSetFor(n)) el.textContent = "Off while a password is saved here.";
    else if (n.shared_admin_seen) el.textContent = "Stopped: it answered as admin.";
    else if (guestUseDefault || (config.guest_key_overrides || {})[n.node_id] === undefined) el.textContent = "Following the default.";
    else el.textContent = "Overridden. The default no longer changes this repeater.";
  }

  // The panel's form, from what's saved. Used on opening, and after Apply or
  // Clear overrides changed what's saved.
  function fillForm(n) {
    const nodeId = n.node_id;
    $("login-password").value = "";
    $("remove-password").checked = false;
    const pending = passwordUpdates[nodeId];
    const pwSet = pwSetFor(n);
    $("remove-password").disabled = !pwSet;
    // Show whether a password is saved: the field is write-only (always blank),
    // so without this the operator can't tell a save worked. "" pending is a
    // queued removal; a non-empty string is a queued new password.
    const pwState = $("password-state");
    if (pending === "") { pwState.textContent = "Removing the password didn't save — press Apply to retry."; pwState.className = "password-state small"; }
    else if (pending) { pwState.textContent = "The new password didn't save — press Apply to retry."; pwState.className = "password-state small"; }
    else if (pwSet) { pwState.textContent = "✓ A password is saved. Leave blank to keep it, type to replace it, or tick Remove."; pwState.className = "password-state small saved"; }
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
      o.textContent = o.dataset.label + (tooFast ? " (faster than allowed)" : "");
    });
    const blocked = testBlock(nodeId);
    $("test-now").disabled = !!blocked || testBusy;
    $("test-gate").hidden = !blocked;
    $("test-gate").textContent = blocked ? `Test is off: ${blocked}.` : "";
    $("temp-last").textContent = n.temperature_c != null ? `Last: ${Number(n.temperature_c).toFixed(1)} °C, ${ago(n.temp_at)}.`
      : n.temp_misses >= 3 ? "None reported so far." : "";

    // Shared-key opt-in: offered whenever a CoreScope address is set. The key
    // is only fetched once a repeater is opted in, so this must not wait for a
    // fetched key — otherwise the first repeater could never be opted in. A
    // saved password blocks it; an earlier admin answer stops it until cleared.
    $("shared-block").hidden = !(runtime.corescope && runtime.corescope.url);
    $("shared-this").checked = guestFor(nodeId);
    guestShown = guestFor(nodeId);
    guestUseDefault = false;
    $("guest-use-default").hidden = (config.guest_key_overrides || {})[nodeId] === undefined;
    guestStateText(n);
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
      adminWarn.textContent = `This repeater answered the guest key as ADMIN. The guest key is stopped here and the key's access was ${n.shared_admin_removed ? "removed automatically" : "NOT removed — ask the owner to run setperm 0"}.`;
    } else {
      adminWarn.hidden = true;
    }
  }

  function openDetails(nodeId) {
    const n = (runtime.nodes || []).find((x) => x.node_id === nodeId);
    if (!n) return;
    detailsTarget = nodeId;
    testRun++;  // a Test watched in the panel stops with the panel's repeater
    testBusy = false;
    $("details-title").textContent = n.name;
    $("details-id").textContent = `${nodeId.slice(0, 8)}…`;
    $("test-status").hidden = true;  // clear any result from a previous test
    applyStatus("");
    $("work").classList.add("open");
    $("details").hidden = false;
    if (window.innerWidth <= 900) $("details").scrollIntoView({ block: "start" });  // it sits below the list here
    fillForm(n);
    fillFacts(n);
    loadMonitors(nodeId);
    renderRepeaters();
    $("details").scrollTop = 0;
    // Stacked on a phone, the panel is below a list that may be long.
    if (window.matchMedia("(max-width: 900px)").matches) $("details").scrollIntoView({ block: "start" });
    $("details-title").focus({ preventScroll: true });
  }

  // Closing the panel abandons a Test that is still being watched. Unapplied
  // changes in it are dropped, as Close always did.
  function closeDetails() {
    const id = detailsTarget;
    testRun++;
    monitorsRun++;
    testBusy = false;
    detailsTarget = null;
    $("details").hidden = true;
    $("work").classList.remove("open");
    renderRepeaters();
    const row = id && document.querySelector(`[data-details="${id}"]`);
    if (row) row.focus();
  }

  function applyStatus(text, kind) {
    const el = $("apply-status");
    el.textContent = text || "";
    el.className = "small " + (kind || "");
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
    ev.preventDefault();
    // Enter in the password box submits with the first button, which is Apply.
    const action = (ev.submitter && ev.submitter.value) || "save";
    if (!detailsTarget) return;
    if (action === "test") { test(detailsTarget); return; }
    const id = detailsTarget;
    const node = (runtime.nodes || []).find((x) => x.node_id === id) || {};
    if (action === "remove") {
      if (!node.manual) return;  // only repeaters you added can be removed
      config.manual_repeaters = manualList().filter((m) => m.node_id !== id);
      const left = new Set(chosenSet());
      left.delete(id);
      pendingTargets = left;
      closeDetails();
    } else {  // Apply: stage this repeater's password / temperature / shared-key choices
      const guest = !$("shared-this").disabled && $("shared-this").checked;
      if (guest) {
        // Under the guest key the password is the guest one (blank or hello),
        // a plain setting; the companion password box is hidden and ignored.
        const gps = { ...(config.guest_passwords || {}) };
        const v = $("guest-password").value;
        if (v === "auto") delete gps[id]; else gps[id] = v;
        config.guest_passwords = gps;
      } else {
        const pw = $("login-password").value;
        if ($("remove-password").checked) passwordUpdates[id] = "";
        else if (pw) passwordUpdates[id] = pw;
      }
      const pis = { ...(config.poll_intervals || {}) };
      const iv = $("node-interval").value;
      if (iv === "default") delete pis[id]; else pis[id] = Number(iv);
      config.poll_intervals = pis;
      const temps = new Set(tempSet());
      if ($("temp-this").checked) temps.add(id); else temps.delete(id);
      tempTargets = temps;
      // Only a choice made here becomes an override; opening and applying
      // the panel leaves a repeater following the default.
      const ovs = { ...(config.guest_key_overrides || {}) };
      if (guestUseDefault) delete ovs[id];
      else if (!$("shared-this").disabled && $("shared-this").checked !== guestShown) ovs[id] = $("shared-this").checked;
      config.guest_key_overrides = ovs;
      showClearOverrides();
      applyStatus("Saving…");
    }
    // Save straight away — the repeater's own panel shouldn't need a second
    // trip to the Save bar. The panel stays open; a failure keeps the edits
    // staged (dirty) and says so here and in the Save bar.
    saveAll().then(() => {
      const open = (runtime.nodes || []).find((x) => x.node_id === id);
      if (detailsTarget !== id || !open) return;
      fillForm(open);
      fillFacts(open);
      applyStatus("Saved.", "good");
    }).catch((e) => {
      dirty = true;
      saveStatus(`Not saved: ${e.message}`, "bad");
      if (detailsTarget === id) applyStatus(`Not saved: ${e.message}`, "bad");
    });
  });
  // A guest-key repeater uses the guest password (blank or hello); the
  // companion-key password only matters when the guest key is off.
  function showPasswordBlock() {
    const guest = !$("shared-block").hidden && $("shared-this").checked && !$("shared-this").disabled;
    $("guest-pw-block").hidden = !guest;
    $("companion-pw-block").hidden = guest;
  }
  $("shared-this").addEventListener("change", () => { guestUseDefault = false; showPasswordBlock(); });
  function showClearOverrides() {
    const n = Object.keys(config.guest_key_overrides || {}).length;
    $("clear-overrides").hidden = !n;
    $("clear-overrides").textContent = `Clear overrides (${n})`;
    $("ov-text").textContent = n ? `${n} repeater${n === 1 ? "" : "s"} overridden.` : "No repeaters overridden.";
  }
  $("clear-overrides").addEventListener("click", () => {
    config.guest_key_overrides = {};
    showClearOverrides();
    saveAll().then(() => {
      const open = (runtime.nodes || []).find((x) => x.node_id === detailsTarget);
      if (open) fillForm(open);
    }).catch((err) => { dirty = true; saveStatus(`Not saved: ${err.message}`, "bad"); });
  });
  $("guest-use-default").addEventListener("click", () => {
    guestUseDefault = true;
    $("shared-this").checked = guestDefault();
    $("guest-use-default").hidden = true;
    const open = (runtime.nodes || []).find((x) => x.node_id === detailsTarget);
    if (open) guestStateText(open);
    showPasswordBlock();
  });
  $("details-close").addEventListener("click", closeDetails);
  $("details").addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDetails(); });

  function showView(name) {
    view = name;
    document.querySelectorAll("[data-view]").forEach((b) => {
      const on = b.dataset.view === name;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      $(`v-${b.dataset.view}`).hidden = !on;
    });
    if (saveMsg.kind === "good") saveMsg = {};
    updateSaveBar();
  }
  document.querySelector("nav.views").addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
    const tabs = [...document.querySelectorAll("[data-view]")];
    const at = tabs.findIndex((b) => b.dataset.view === view);
    const next = tabs[(at + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    showView(next.dataset.view);
    next.focus();
  });
  $("add-toggle").addEventListener("click", () => {
    const form = $("add-form");
    form.hidden = !form.hidden;
    $("add-toggle").setAttribute("aria-expanded", String(!form.hidden));
    if (!form.hidden) $("add-query").focus();
  });
  $("search").addEventListener("input", () => { query = $("search").value; renderRepeaters(); });

  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-details], [data-add], [data-f], [data-view]");
    if (!t) return;
    if (t.dataset.details) { if (t.dataset.details === detailsTarget) closeDetails(); else openDetails(t.dataset.details); }
    else if (t.dataset.add) addManual(t.dataset.add, t.dataset.name);
    else if (t.dataset.f) { filter = t.dataset.f; renderRepeaters(); }
    else showView(t.dataset.view);
  });
  // The panel saves by itself (Apply), and the two search boxes aren't settings.
  function noteEdit(ev) {
    const el = ev.target;
    if (el.closest("#details, #add-query, #search") || !el.matches("input, select")) return;
    dirty = true;
    if (el.dataset.node) {
      const picked = new Set(chosenSet());
      if (el.checked) picked.add(el.dataset.node); else picked.delete(el.dataset.node);
      pendingTargets = picked;
      renderRepeaters();
    } else if (el.id === "max_hops") {
      render();
    }
    updateSaveBar();
  }
  document.addEventListener("input", noteEdit);
  document.addEventListener("change", noteEdit);
  function addManual(nodeId, name) {
    nodeId = String(nodeId || "").toLowerCase();
    if (!KEY_RE.test(nodeId)) { notice("That isn't a 64-character repeater key.", "warn"); return; }
    if (!manualList().some((m) => m.node_id === nodeId)) config.manual_repeaters = [...manualList(), { node_id: nodeId, name: name || nodeId.slice(0, 8) }];
    pendingTargets = new Set(chosenSet()).add(nodeId);
    $("add-results").innerHTML = "";
    $("add-query").value = "";
    dirty = true;
    updateSaveBar();
    notice(`Save to add ${name || nodeId.slice(0, 8)}. It is treated as heard direct; its details show its routes once saved.`, "good");
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
  $("connect-top").addEventListener("click", connect);
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
  // unlike the notice at the top of the page. Off the Settings tab the bar shows
  // only while there's something to save or to say.
  function saveStatus(text, kind) {
    saveMsg = { text, kind };
    clearTimeout(saveMsgTimer);
    if (kind === "good") {
      saveMsgTimer = setTimeout(() => {
        if (view !== "settings" && saveMsg.kind === "good") { saveMsg = {}; updateSaveBar(); }
      }, 4000);
    }
    updateSaveBar();
  }
  function updateSaveBar() {
    const busy = saveMsg.kind === "busy";
    const said = saveMsg.text && (busy || saveMsg.kind === "bad" || !dirty);
    const el = $("save-status");
    el.textContent = said ? saveMsg.text : dirty ? "Unsaved changes" : "No unsaved changes";
    el.className = `small ${said ? saveMsg.kind : "muted"}`;
    $("save").disabled = busy || (!dirty && saveMsg.kind !== "bad");
    $("savebar").hidden = view !== "settings" && !dirty && !saveMsg.text;
  }

  // saveAll persists everything pending (page settings + any staged per-repeater
  // password/temperature/shared changes). Used by the page's Save bar and by
  // the details panel's Apply.
  async function saveAll() {
    collect();
    const extra = Object.keys(passwordUpdates).length ? { password_updates: passwordUpdates } : {};
    saveStatus("Saving…", "busy");
    await saveConfig(extra);
    const at = Date.now();
    Object.entries(passwordUpdates).forEach(([id, pw]) => { pwApplied[id] = { set: !!pw, at }; });
    passwordUpdates = {};
    tempTargets = null;
    pendingTargets = null;
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
      // The plugin picks a saved password up shortly; give it a minute.
      Object.keys(pwApplied).forEach((id) => {
        const node = (runtime.nodes || []).find((x) => x.node_id === id);
        if (!node || !!node.password_set === pwApplied[id].set || Date.now() - pwApplied[id].at > 60000) delete pwApplied[id];
      });
      render();
      // Keep an open repeater panel current; drop it if the repeater has gone.
      if (detailsTarget) {
        const open = (runtime.nodes || []).find((x) => x.node_id === detailsTarget);
        if (open) fillFacts(open); else closeDetails();
      }
    } catch (e) {
      notice(e.message, "bad");
    }
  }

  refresh();
  setInterval(refresh, 15000);
})();
