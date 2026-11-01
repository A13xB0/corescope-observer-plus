"""Community polling: CoreScope's policy (limits and switches), the freshness
check, standby, the request cap, claims v2, and how each fails safe."""

import json
import time

import pytest

from fake_companion import FakeCompanion
from openhop_corescope_observer_plus import app as app_mod
from openhop_corescope_observer_plus import community as cm
from openhop_corescope_observer_plus.app import App
from openhop_corescope_observer_plus.openhop import Neighbour, Route, load_openhop_config
from openhop_corescope_observer_plus.poller import OK, PollResult, utc_iso
from openhop_corescope_observer_plus.settings import parse
from test_app import NODE, make_app

OTHER = "0aa0" + "cd" * 30
RELAY = "4128a6" + "11" * 29


# -- helpers -------------------------------------------------------------------


@pytest.fixture
def fake():
    f = FakeCompanion()
    yield f
    f.close()


@pytest.fixture
def sent(monkeypatch):
    """Every poll that would go on air, by node id (the radio is faked)."""
    calls = []

    def fake_poll(client, n, *a, **k):
        calls.append(n.node_id)
        return PollResult(OK, battery_mv=4000, route=(k.get("routes") or [None])[0])

    monkeypatch.setattr(app_mod, "poll_one", fake_poll)
    monkeypatch.setattr(app_mod, "poll_one_shared", fake_poll)
    monkeypatch.setattr(app_mod, "CORESCOPE_SETTLE_S", 0)
    monkeypatch.setattr(app_mod, "corescope_battery_snapshot", lambda *a, **k: None)
    return calls


def ready(tmp_path, fake, monkeypatch, **cfg):
    cfg.setdefault("targets", [NODE])
    cfg.setdefault("corescope_url", "https://cs.example")
    a, data = make_app(tmp_path, fake, **cfg)
    monkeypatch.setattr(a, "refresh", lambda: None)
    a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
    a.check = {"companion": {"ok": True}, "publish": {"ok": True}}
    a.candidates = [
        Neighbour(NODE, "West Lomond", 6.5, -100, 0, True),
        Neighbour(
            OTHER,
            "Lucklaw Hill",
            None,
            None,
            0,
            False,
            routes=[
                Route(relay_id=RELAY, relay_name="West Lomond", hash_size=3, adverts=4, snr=2.0)
            ],
        ),
    ]
    a.settings.max_hops = 1
    a.settings.spacing_seconds = 0  # no on-air spacing to wait out in tests
    return a, data


def serve_policy(monkeypatch, policy=None, status=200, calls=None):
    def fake(url, obs, ver, **k):
        if calls is not None:
            calls.append(ver)
        return (status, dict(policy or {}) if status == 200 else None)

    monkeypatch.setattr(app_mod, "corescope_policy", fake)


def serve_latest(monkeypatch, ts, by="Kirkcaldy East", reachable=True):
    monkeypatch.setattr(
        app_mod,
        "corescope_battery_check",
        lambda url, nid, **k: (reachable, {"latest_ts": ts, "by": by} if reachable else {}),
    )


def serve_owners(monkeypatch, nodes, policy=None, calls=None):
    def fake(url, obs, **k):
        if calls is not None:
            calls.append(k.get("plugin_version"))
        body = {"observer": obs, "nodes": nodes}
        if policy is not None:
            body["policy"] = policy
        return body

    monkeypatch.setattr(app_mod, "corescope_owners", fake)


def requests_for(a, *reqs):
    a.state.value["pending"] = list(reqs)
    a.run_requests()


# -- clamp and parsing -----------------------------------------------------------


def test_clamp_keeps_every_limit_in_range_and_notes_it():
    p, notes = cm.clamp(
        {
            "direct_s": 900,
            "one_hop_s": 999999,
            "grace_s": 60,
            "max_requests": 9,
            "takeover_after": 0,
            "version": "a1b2c3d4",
            "paused": "yes",
            "excluded_nodes": [NODE.upper(), "short"],
            "node_windows": {OTHER: 600, NODE: 14400, "bad": 5},
        },
        fast=False,
    )
    assert (p["direct_s"], p["one_hop_s"], p["grace_s"]) == (1800, 86400, 600)
    assert (p["max_requests"], p["takeover_after"]) == (3, 1)
    assert p["paused"] is False  # only a real true pauses
    assert p["excluded_nodes"] == [NODE]
    assert p["node_windows"] == {OTHER: 1800, NODE: 14400}
    asked = {(n["field"], n.get("node_id")): (n["asked"], n["using"]) for n in notes}
    assert asked[("direct_s", None)] == (900, 1800)
    assert asked[("max_requests", None)] == (9, 3)
    assert asked[("node_windows", OTHER)] == (600, 1800)


def test_clamp_defaults_for_nothing_or_garbage():
    for raw in (None, {}, {"direct_s": "soon", "max_requests": None}, "nope"):
        p, notes = cm.clamp(raw, fast=False)
        assert (
            p["direct_s"],
            p["one_hop_s"],
            p["grace_s"],
            p["max_requests"],
            p["takeover_after"],
        ) == (3600, 7200, 1800, 2, 2)
        assert p["paused"] is False and p["excluded_nodes"] == [] and notes == []


def test_fast_mode_is_off_by_default_and_only_the_env_turns_it_on(monkeypatch, tmp_path, fake):
    assert cm.fast_mode() is False and cm.timings() is cm.NORMAL
    assert cm.NORMAL.scheduled_gap_s == 60 and cm.NORMAL.no_reply_retry_s == 20 * 60
    assert (
        cm.NORMAL.refresh_s == 600
        and cm.NORMAL.policy_cache_s == 60
        and cm.NORMAL.claims_every_s == 1800
    )
    # No setting turns it on.
    a, _ = make_app(tmp_path, fake, fast=True, test_fast=True, OPENHOP_OBSERVER_PLUS_TEST_FAST="1")
    assert cm.fast_mode() is False and a.community.view(time.time())["fast_mode"] is False
    assert cm.clamp({"direct_s": 120})[0]["direct_s"] == 1800
    monkeypatch.setenv(cm.FAST_ENV, "1")
    assert cm.fast_mode() is True and cm.timings() is cm.FAST
    p, _ = cm.clamp({"direct_s": 120, "one_hop_s": 240, "grace_s": 60})
    assert (p["direct_s"], p["one_hop_s"], p["grace_s"]) == (120, 240, 60)


def test_fast_mode_uses_the_policy_window_as_the_interval(monkeypatch, tmp_path, fake):
    monkeypatch.setenv(cm.FAST_ENV, "1")
    a, _ = ready(tmp_path, fake, monkeypatch, interval_minutes=60)
    a.community.apply({"direct_s": 120, "one_hop_s": 240, "grace_s": 60}, time.time())
    assert a.interval_s(NODE, time.time()) == 120
    assert a.interval_s(OTHER, time.time()) == 240


def test_community_setting_defaults_on_and_parses_off():
    assert parse({}).community is True
    assert parse({"community": False}).community is False
    assert parse({"community": "off"}).community is False


def test_openhop_config_path_env_override(monkeypatch):
    assert parse({"openhop_config_path": "/x/config.yaml"}).openhop_config_path == "/x/config.yaml"
    monkeypatch.setenv("OPENHOP_CONFIG_PATH", "/bench/config.yaml")
    assert (
        parse({"openhop_config_path": "/x/config.yaml"}).openhop_config_path == "/bench/config.yaml"
    )


def test_version_compare():
    assert cm.version_at_least("0.8.0", "") is True
    assert cm.version_at_least("0.8.0", "0.8.0") is True
    assert cm.version_at_least("0.8.10", "0.8.2") is True  # numeric, not string
    assert cm.version_at_least("0.8.1", "0.8.2") is False
    assert cm.version_at_least("garbage", "0.0.1") is False


# -- interval floor -----------------------------------------------------------------


def test_interval_floor_follows_the_window_for_the_route(tmp_path, fake, monkeypatch):
    a, _ = ready(tmp_path, fake, monkeypatch, interval_minutes=30, poll_intervals={OTHER: 30})
    now = time.time()
    assert a.interval_s(NODE, now) == 3600  # 30 min asked, direct window 1 h
    assert a.interval_s(OTHER, now) == 7200  # through a relay: the 1-hop window
    a.settings.poll_intervals[NODE] = 360
    assert a.interval_s(NODE, now) == 6 * 3600  # slower than the window: the operator's choice
    a.community.apply({"node_windows": {OTHER: 4 * 3600}}, now)
    assert a.interval_s(OTHER, now) == 4 * 3600  # CoreScope's own figure for the repeater


def test_stale_policy_doubles_the_window_only_when_corescope_is_unreachable(
    tmp_path, fake, monkeypatch
):
    a, _ = ready(tmp_path, fake, monkeypatch)
    now = time.time()
    a.community.apply({"direct_s": 3600}, now - 7 * 3600)
    assert a.window_s(NODE, now) == 3600  # last pull worked: as it is
    a.community.note_failure(0, now)
    assert a.window_s(NODE, now) == 7200  # unreachable for 7 h: twice the window
    a.community.note_failure(404, now)
    assert a.window_s(NODE, now) == 3600  # answers, just has no policy: not doubled


# -- hard gates -------------------------------------------------------------------------


def test_pause_stops_scheduled_test_and_check_now(tmp_path, fake, monkeypatch, sent):
    serve_policy(monkeypatch, {"paused": True, "paused_reason": "Airtime survey", "version": "p1"})
    a, _ = ready(tmp_path, fake, monkeypatch, enabled=True)
    assert a.poll([NODE]) == [] and sent == []
    assert a.state.node(NODE)["skip"]["reason"] == "paused"
    requests_for(
        a,
        {"id": "t1", "type": "test_login", "node_id": NODE},
        {"id": "c1", "type": "check_now", "scope": "all"},
    )
    assert sent == []
    assert (
        a.actions["test_login"]["ok"] is False
        and "Airtime survey" in a.actions["test_login"]["message"]
    )
    assert a.actions["check_now"]["ok"] is False and a.actions["check_now"]["skipped"] == "paused"


def test_gates_persist_through_an_outage_and_a_restart(tmp_path, fake, monkeypatch, sent):
    serve_policy(
        monkeypatch,
        {
            "paused": True,
            "observer_blocked": True,
            "version_ok": False,
            "min_plugin_version": "9.9.9",
        },
    )
    a, data = ready(tmp_path, fake, monkeypatch)
    a.refresh_policy(time.time(), 0)
    assert a.community.gate(time.time())["reason"] == "blocked"
    # CoreScope goes away; the plugin restarts.
    serve_policy(monkeypatch, status=0)
    b = App(data)
    b.load_settings()
    b.refresh_policy(time.time() + 3600, 0)
    view = b.community.view(time.time())
    assert view["blocked"] and view["paused"] and not view["version_ok"]
    assert view["last_error"] == "CoreScope unreachable"
    # A 404 (feature gone, or an old CoreScope) doesn't clear them either.
    serve_policy(monkeypatch, status=404)
    b.refresh_policy(time.time() + 7200, 0)
    assert b.community.gate(time.time())["reason"] == "blocked"
    # Only a fetched policy clears them.
    serve_policy(monkeypatch, {"paused": False, "observer_blocked": False, "version_ok": True})
    b.refresh_policy(time.time() + 9000, 0)
    assert b.community.gate(time.time()) is None


def test_version_gate(tmp_path, fake, monkeypatch, sent):
    serve_policy(
        monkeypatch,
        {
            "min_plugin_version": "9.9.9",
            "version_ok": False,
            "min_version_reason": "resends logins",
        },
    )
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.refresh_policy(time.time(), 0)
    g = a.community.gate(time.time())
    assert g["reason"] == "version_disabled"
    assert f"This version ({app_mod.__version__}) is disabled by CoreScope" in g["text"]
    assert "Upgrade to 9.9.9 or later" in g["text"] and "resends logins" in g["text"]
    assert a.poll([NODE], mode="test") == [] and sent == []
    # After an upgrade the saved answer (about the old version) is judged afresh.
    a.community.plugin_version = "9.9.9"
    assert a.community.gate(time.time()) is None


def test_pause_lifts_itself_when_paused_until_passes(tmp_path, fake, monkeypatch, sent):
    now = time.time()
    serve_policy(monkeypatch, {"paused": True, "paused_until": utc_iso(now + 600)})
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.refresh_policy(now, 0)
    serve_policy(monkeypatch, status=0)  # and CoreScope is down when it ends
    assert a.community.paused(now) is True
    assert a.community.paused(now + 601) is False and a.community.gate(now + 601) is None


def test_excluded_repeater_is_never_sent_to(tmp_path, fake, monkeypatch, sent):
    serve_policy(monkeypatch, {"excluded_nodes": [NODE]})
    a, _ = ready(tmp_path, fake, monkeypatch, enabled=True)
    for mode in ("scheduled", "stale", "all", "test"):
        assert a.poll([NODE], mode=mode) == []
    assert sent == [] and a.state.node(NODE)["skip"]["reason"] == "excluded"
    assert all(c["node_id"] != NODE for c in a.claim_entries())


# -- freshness check and standby ----------------------------------------------------


def test_poller_skips_a_fresh_reading_and_reschedules(tmp_path, fake, monkeypatch, sent):
    now = time.time()
    serve_latest(monkeypatch, utc_iso(now - 600))
    a, _ = ready(tmp_path, fake, monkeypatch)
    assert a.poll([NODE]) == [] and sent == []
    st = a.state.node(NODE)
    assert (
        st["skip"]["reason"] == "fresh"
        and "Kirkcaldy East sent a reading 10 min ago" in st["skip"]["text"]
    )
    assert abs(st["next_due_at"] - (now - 600 + 3600)) < 5  # latest + window
    # Not asked again before then, not even CoreScope.
    serve_latest(monkeypatch, None)
    a._last_scheduled = 0
    assert a.poll([NODE]) == [] and sent == []
    # Once it's due and nothing newer arrived: polled.
    st["next_due_at"] = now - 1
    a._last_scheduled = 0
    a.poll([NODE])
    assert sent == [NODE] and "next_due_at" not in st and "skip" not in st


def test_poller_fails_open_when_corescope_is_unreachable(tmp_path, fake, monkeypatch, sent):
    serve_latest(monkeypatch, None, reachable=False)
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.poll([NODE])
    assert sent == [NODE]


def test_standby_waits_window_plus_grace_then_steps_in(tmp_path, fake, monkeypatch, sent):
    now = time.time()
    serve_owners(
        monkeypatch,
        {
            OTHER: {
                "owner": "ee" * 32,
                "owner_name": "Montrave",
                "mine": False,
                "role": "standby",
                "owner_hops": 0,
            }
        },
    )
    a, _ = ready(tmp_path, fake, monkeypatch, targets=[OTHER])  # an own-key target
    # 2 h 10 min old: past the 2 h one-hop window, inside window + 30 min grace.
    serve_latest(monkeypatch, utc_iso(now - 7800), by="Montrave")
    assert a.poll([OTHER]) == [] and sent == []
    st = a.state.node(OTHER)
    assert st["skip"]["reason"] == "standby" and "Montrave polls it (direct)" in st["skip"]["text"]
    assert abs(st["next_due_at"] - (now - 7800 + 7200 + 1800)) < 5
    # 2 h 40 min old: the poller missed; standby steps in.
    st.pop("next_due_at")
    serve_latest(monkeypatch, utc_iso(now - 9600), by="Montrave")
    a._last_scheduled = 0
    a.poll([OTHER])
    assert sent == [OTHER]


def test_standby_holds_when_corescope_is_unreachable(tmp_path, fake, monkeypatch, sent):
    serve_owners(
        monkeypatch,
        {
            NODE: {
                "owner": "ee" * 32,
                "owner_name": "Montrave",
                "mine": False,
                "role": "standby",
                "owner_hops": 0,
            }
        },
    )
    serve_latest(monkeypatch, None, reachable=False)
    a, _ = ready(tmp_path, fake, monkeypatch)
    assert a.poll([NODE]) == [] and sent == []
    st = a.state.node(NODE)
    assert st["skip"]["reason"] == "standby" and st["next_due_at"] > time.time()


def test_owner_logic_applies_to_own_key_and_shared_key_targets(tmp_path, fake, monkeypatch, sent):
    from test_app import _serve_key

    _serve_key(monkeypatch)
    monkeypatch.setattr(app_mod, "register_guest_key", lambda url, body, **k: (404, {}))
    serve_latest(monkeypatch, None)  # CoreScope reachable, nothing fresh
    owners = {
        NODE: {
            "owner": "ee" * 32,
            "owner_name": "Montrave",
            "mine": False,
            "role": "standby",
            "owner_hops": 0,
            "latest_ts": None,
        },
        OTHER: {
            "owner": "ee" * 32,
            "owner_name": "Montrave",
            "mine": False,
            "role": "standby",
            "owner_hops": 0,
        },
    }
    serve_owners(monkeypatch, owners)
    a, _ = ready(tmp_path, fake, monkeypatch, targets=[NODE, OTHER], shared_key_targets=[NODE])
    assert a.uses_shared_key(NODE) and not a.uses_shared_key(OTHER)
    now = time.time()
    assert (
        a.role_for(NODE, now)["role"] == "standby" and a.role_for(OTHER, now)["role"] == "standby"
    )
    # Mine (polling) for both: poller. Independent counts as poller too.
    owners[NODE].update(mine=True, role="polling")
    owners[OTHER].update(mine=True, role="independent")
    a._owners["at"] = 0
    assert a.role_for(NODE, time.time())["role"] == "poller"
    assert a.role_for(OTHER, time.time()) == {"role": "poller", "independent": True}
    # Excluded / paused / disabled roles mean no sending.
    owners[OTHER].update(mine=False, role="excluded")
    a._owners["at"] = 0
    assert a.poll([OTHER]) == [] and a.state.node(OTHER)["skip"]["reason"] == "excluded"


def test_owners_call_sends_the_version_and_its_policy_is_kept(tmp_path, fake, monkeypatch):
    calls = []
    serve_owners(monkeypatch, {}, policy={"version": "v7", "direct_s": 5400}, calls=calls)
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.owners(time.time())
    assert calls == [app_mod.__version__]
    assert a.community.policy()[0]["version"] == "v7" and a.window_s(NODE, time.time()) == 5400


def test_community_off_ignores_owners_but_keeps_limits(tmp_path, fake, monkeypatch, sent):
    calls = []
    serve_owners(
        monkeypatch,
        {NODE: {"owner": "ee" * 32, "owner_name": "Montrave", "mine": False, "role": "standby"}},
        calls=calls,
    )
    now = time.time()
    serve_latest(monkeypatch, utc_iso(now - 600))
    a, _ = ready(tmp_path, fake, monkeypatch, community=False)
    assert a.role_for(NODE, now) == {"role": "poller", "independent": True} and calls == []
    # The freshness check still applies...
    assert a.poll([NODE]) == [] and a.state.node(NODE)["skip"]["reason"] == "fresh"
    # ...and so do pause and exclusion.
    a.community.apply({"excluded_nodes": [NODE]}, now)
    assert a.skip_reason(a._neighbour(NODE), now, "all")["reason"] == "excluded"
    a.community.apply({"paused": True}, now)
    assert a.skip_reason(a._neighbour(NODE), now, "test")["reason"] == "paused"
    assert sent == []


# -- request cap ------------------------------------------------------------------------


def test_request_cap_per_window(tmp_path, fake, monkeypatch, sent):
    a, _ = ready(tmp_path, fake, monkeypatch)
    now = time.time()
    a.state.node(NODE)["sends"] = [now - 1200, now - 600]  # two already this window (cap 2)
    assert a.poll([NODE], mode="all") == [] and sent == []
    st = a.state.node(NODE)
    assert st["skip"]["reason"] == "cap" and abs(st["next_due_at"] - (now - 1200 + 3600)) < 5
    # Test is exempt from the cap.
    a.poll([NODE], mode="test")
    assert sent == [NODE] and len(st["sends"]) == 3
    # One of them outside the window: room for one more.
    st["sends"] = [now - 4000, now - 600]
    a.poll([NODE], mode="all")
    assert sent == [NODE, NODE]


def test_quick_retries_count_toward_the_cap(tmp_path, fake, monkeypatch):
    calls = []

    def no_reply(client, n, *a, **k):
        calls.append(1)
        return PollResult("no_reply", route=(k.get("routes") or [None])[0])

    monkeypatch.setattr(app_mod, "poll_one", no_reply)
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.community.apply({"max_requests": 1}, time.time())
    st = a.state.node(NODE)
    st["last_success"] = time.time() - 3600  # answered recently: quick retries apply
    a.poll([NODE])
    assert len(calls) == 1
    # 20 min later the quick retry is due, but it would be a second request
    # inside the window.
    st["last_attempt"] -= 1201
    st["sends"] = [x - 1201 for x in st["sends"]]
    a._last_scheduled = 0
    a.poll([NODE])
    assert len(calls) == 1 and st["skip"]["reason"] == "cap"


def test_fast_mode_quick_retry_is_30_s(tmp_path, fake, monkeypatch):
    from openhop_corescope_observer_plus.poller import is_due

    st = {"code": "no_reply", "last_attempt": 1000.0, "last_success": 900.0, "no_reply_count": 1}
    assert is_due(st, 3600, False, 1031) is False  # normal: 20 min
    assert is_due(st, 3600, False, 1031, retry_s=cm.FAST.no_reply_retry_s) is True


# -- Check now and Test ---------------------------------------------------------------


def test_check_now_stale_respects_freshness_and_standby_all_does_not(
    tmp_path, fake, monkeypatch, sent
):
    now = time.time()
    serve_owners(
        monkeypatch,
        {
            OTHER: {
                "owner": "ee" * 32,
                "owner_name": "Montrave",
                "mine": False,
                "role": "standby",
                "owner_hops": 0,
            }
        },
    )
    serve_latest(monkeypatch, utc_iso(now - 600), by="Kirkcaldy East")
    monkeypatch.setattr(
        app_mod, "Publisher", lambda cfg: type("P", (), {"publish": lambda *a: None})()
    )
    a, _ = ready(tmp_path, fake, monkeypatch, targets=[NODE, OTHER])
    requests_for(a, {"id": "c1", "type": "check_now", "scope": "stale"})
    act = a.actions["check_now"]
    assert sent == []
    reasons = {p["node_id"]: p["skipped"] for p in act["progress"]}
    assert reasons == {NODE: "fresh", OTHER: "standby"}
    texts = {p["node_id"]: p["message"] for p in act["progress"]}
    assert "Kirkcaldy East sent a reading 10 min ago" in texts[NODE]
    assert "Montrave polls it (direct); you're standby" in texts[OTHER]
    assert act["message"] == "nothing to ask: 2 skipped"
    requests_for(a, {"id": "c2", "type": "check_now", "scope": "all"})
    assert sorted(sent) == sorted([NODE, OTHER])
    assert a.actions["check_now"]["message"] == "2 of 2 read"


def test_check_now_all_still_skips_excluded_and_capped(tmp_path, fake, monkeypatch, sent):
    monkeypatch.setattr(
        app_mod, "Publisher", lambda cfg: type("P", (), {"publish": lambda *a: None})()
    )
    a, _ = ready(tmp_path, fake, monkeypatch, targets=[NODE, OTHER])
    a.community.apply({"excluded_nodes": [OTHER]}, time.time())
    a.state.node(NODE)["sends"] = [time.time() - 60, time.time() - 30]
    requests_for(a, {"id": "c1", "type": "check_now", "scope": "all"})
    reasons = {p["node_id"]: p["skipped"] for p in a.actions["check_now"]["progress"]}
    assert reasons == {NODE: "cap", OTHER: "excluded"} and sent == []


def test_test_ignores_freshness_standby_and_cap(tmp_path, fake, monkeypatch, sent):
    now = time.time()
    serve_owners(
        monkeypatch,
        {NODE: {"owner": "ee" * 32, "owner_name": "Montrave", "mine": False, "role": "standby"}},
    )
    serve_latest(monkeypatch, utc_iso(now - 60))
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.state.node(NODE)["sends"] = [now - 300, now - 200, now - 100]
    requests_for(a, {"id": "t1", "type": "test_login", "node_id": NODE})
    assert sent == [NODE] and a.actions["test_login"].get("ok") is not False
    # ...but its 60 s cooldown stays.
    requests_for(a, {"id": "t2", "type": "test_login", "node_id": NODE})
    assert sent == [NODE] and "Wait" in a.actions["test_login"]["message"]


def test_test_refused_for_an_excluded_repeater(tmp_path, fake, monkeypatch, sent):
    serve_policy(monkeypatch, {"excluded_nodes": [NODE]})
    a, _ = ready(tmp_path, fake, monkeypatch)
    requests_for(a, {"id": "t1", "type": "test_login", "node_id": NODE})
    act = a.actions["test_login"]
    assert sent == [] and act["ok"] is False and act["skipped"] == "excluded"


# -- claims v2 ----------------------------------------------------------------------------


class Claims:
    def __init__(self):
        self.sent = []

    def __call__(self, cfg):
        outer = self

        class P:
            def publish_claim(self, broker, claims, *, policy_version="", community=True):
                outer.sent.append(
                    {"claims": claims, "policy_version": policy_version, "community": community}
                )

            def publish(self, broker, readings):
                pass

        return P()


def test_claims_cover_every_target_with_key_route_and_snr(tmp_path, fake, monkeypatch):
    from test_app import _serve_key

    _serve_key(monkeypatch)
    monkeypatch.setattr(app_mod, "register_guest_key", lambda url, body, **k: (404, {}))
    third = "77" * 32
    claims = Claims()
    monkeypatch.setattr(app_mod, "Publisher", claims)
    a, _ = ready(
        tmp_path,
        fake,
        monkeypatch,
        targets=[NODE, OTHER, third],
        shared_key_targets=[NODE],
        brokers=["ScotMesh"],
        password_updates={third: "secret"},
    )
    a.candidates.append(Neighbour(third, "Redstone", -2.0, -110, 0, True))
    a.community.apply({"version": "a1b2c3d4"}, time.time())
    now = time.time()
    a.publish_claims(now)
    assert len(claims.sent) == 1
    got = {c["node_id"]: c for c in claims.sent[0]["claims"]}
    assert got[NODE] == {"node_id": NODE, "reply_hops": 0, "via": None, "key": "shared", "snr": 6.5}
    assert got[OTHER] == {"node_id": OTHER, "reply_hops": 1, "via": RELAY, "key": "own", "snr": 2.0}
    assert got[third]["key"] == "password"
    assert claims.sent[0]["policy_version"] == "a1b2c3d4" and claims.sent[0]["community"] is True
    # Not again within 30 min...
    a.publish_claims(now + 600)
    assert len(claims.sent) == 1
    # ...unless a route's hops change.
    a.candidates[1] = Neighbour(OTHER, "Lucklaw Hill", 1.0, -110, 0, True)
    a._claims_tried = 0
    a.publish_claims(now + 700)
    assert len(claims.sent) == 2
    assert {c["node_id"]: c for c in claims.sent[1]["claims"]}[OTHER]["reply_hops"] == 0
    # And every 30 min regardless.
    a._claims_tried = 0
    a.publish_claims(now + 700 + 1800)
    assert len(claims.sent) == 3


def test_no_claims_with_community_off_after_one_saying_so(tmp_path, fake, monkeypatch):
    claims = Claims()
    monkeypatch.setattr(app_mod, "Publisher", claims)
    a, _ = ready(tmp_path, fake, monkeypatch, brokers=["ScotMesh"], community=False)
    a.publish_claims(time.time())
    assert claims.sent == []  # never claimed: nothing to take back
    a.settings.community = True
    a.publish_claims(time.time())
    assert claims.sent[-1]["community"] is True
    a.settings.community = False
    a._claims_tried = 0
    a.publish_claims(time.time())
    assert claims.sent[-1]["community"] is False and len(claims.sent) == 2
    a._claims_tried = 0
    a.publish_claims(time.time() + 3600)
    assert len(claims.sent) == 2


def test_claims_still_go_out_while_paused(tmp_path, fake, monkeypatch):
    claims = Claims()
    monkeypatch.setattr(app_mod, "Publisher", claims)
    a, _ = ready(tmp_path, fake, monkeypatch, brokers=["ScotMesh"])
    a.community.apply({"paused": True}, time.time())
    a.publish_claims(time.time())
    assert len(claims.sent) == 1


# -- fail safe: CoreScope without the feature, or unreachable -------------------------


def test_policy_404_uses_defaults_and_still_polls(tmp_path, fake, monkeypatch, sent):
    calls = []
    serve_policy(monkeypatch, status=404, calls=calls)
    monkeypatch.setattr(app_mod, "corescope_owners", lambda url, obs, **k: None)  # 404 -> None
    serve_latest(monkeypatch, None)
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.poll([NODE])
    assert calls and sent == [NODE]
    v = a.community.view(time.time())
    assert v["has_policy"] is False and v["gate"] is None and v["stale"] is False
    assert v["policy"]["direct_s"] == 3600 and "404" in v["last_error"]


def test_policy_is_refreshed_before_a_send_but_not_every_tick(tmp_path, fake, monkeypatch, sent):
    calls = []
    serve_policy(monkeypatch, {"version": "v1"}, calls=calls)
    a, _ = ready(tmp_path, fake, monkeypatch)
    now = time.time()
    a.refresh_policy(now - 30, 0)
    assert len(calls) == 1
    a.poll([NODE], mode="all")  # cached copy 30 s old: used as it is
    assert len(calls) == 1
    a.community.s["fetched_at"] = now - 61
    a._policy_tried = 0
    a.poll([NODE], mode="all")
    assert len(calls) == 2


def test_policy_unreachable_retries_no_more_than_the_cache_age(tmp_path, fake, monkeypatch):
    calls = []
    serve_policy(monkeypatch, status=0, calls=calls)
    a, _ = ready(tmp_path, fake, monkeypatch)
    now = time.time()
    a.refresh_policy(now, 60)
    a.refresh_policy(now + 10, 60)
    assert len(calls) == 1
    a.refresh_policy(now + 61, 60)
    assert len(calls) == 2


def test_owners_unreachable_forgets_roles_after_a_while(tmp_path, fake, monkeypatch):
    serve_owners(
        monkeypatch,
        {NODE: {"owner": "ee" * 32, "owner_name": "Montrave", "mine": False, "role": "standby"}},
    )
    a, _ = ready(tmp_path, fake, monkeypatch)
    now = time.time()
    assert a.role_for(NODE, now)["role"] == "standby"
    monkeypatch.setattr(app_mod, "corescope_owners", lambda url, obs, **k: None)
    assert a.role_for(NODE, now + 601)["role"] == "standby"  # one miss: kept
    assert (
        a.role_for(NODE, now + 1900)["role"] == "poller"
    )  # 30 min without an answer: poll ourselves


def test_monitors_request_proxies_corescope(tmp_path, fake, monkeypatch):
    body = {
        "node_id": NODE,
        "window_s": 3600,
        "monitors": [{"observer_id": "ee" * 32, "name": "Montrave", "role": "polling"}],
    }
    seen = []
    monkeypatch.setattr(
        app_mod,
        "corescope_monitors",
        lambda url, nid, ver, **k: (seen.append((url, nid, ver)), (200, body))[1],
    )
    a, _ = ready(tmp_path, fake, monkeypatch)
    requests_for(a, {"id": "m1", "type": "monitors", "node_id": NODE})
    act = a.actions["monitors"]
    assert act["ok"] and act["monitors"] == body and act["node_id"] == NODE
    assert seen == [("https://cs.example", NODE, app_mod.__version__)]
    monkeypatch.setattr(app_mod, "corescope_monitors", lambda url, nid, ver, **k: (404, None))
    requests_for(a, {"id": "m2", "type": "monitors", "node_id": NODE})
    assert (
        a.actions["monitors"]["ok"] is False
        and "doesn't list monitors" in a.actions["monitors"]["message"]
    )
    monkeypatch.setattr(app_mod, "corescope_monitors", lambda url, nid, ver, **k: (0, None))
    requests_for(a, {"id": "m3", "type": "monitors", "node_id": NODE})
    assert a.actions["monitors"]["message"] == "CoreScope unreachable"


def test_runtime_shows_community_state(tmp_path, fake, monkeypatch):
    now = time.time()
    a, data = ready(tmp_path, fake, monkeypatch)
    a.community.apply(
        {"version": "v7", "direct_s": 900, "paused": True, "paused_reason": "survey"}, now
    )
    a.state.node(NODE).update(
        next_due_at=now + 600,
        skip={"reason": "fresh", "text": "x", "by": "Kirkcaldy East"},
        fresh_at=now - 300,
        fresh_by="Kirkcaldy East",
    )
    a.write_runtime()
    rt = json.loads((data / "runtime.json").read_text())
    c = rt["community"]
    assert c["enabled"] is True and c["version"] == "v7" and c["paused"] is True
    assert c["gate"]["reason"] == "paused" and "survey" in c["gate"]["text"]
    assert c["clamped"] == [{"field": "direct_s", "asked": 900, "using": 1800}]
    assert c["fast_mode"] is False and rt["observer_id"] == a.observer_id
    node = next(n for n in rt["nodes"] if n["node_id"] == NODE)
    assert node["community"]["who"] == "covered" and node["community"]["window_s"] == 1800
    other = next(n for n in rt["nodes"] if n["node_id"] == OTHER)
    assert other["community"] is None  # not selected


def test_battery_check_helper_tells_down_from_empty(monkeypatch):
    from openhop_corescope_observer_plus import openhop

    monkeypatch.setattr(openhop, "corescope_get", lambda *a, **k: (0, None))
    assert openhop.corescope_battery_check("https://cs", NODE) == (False, {})
    monkeypatch.setattr(openhop, "corescope_get", lambda *a, **k: (404, None))
    assert openhop.corescope_battery_check("https://cs", NODE) == (
        True,
        {"latest_ts": None, "by": ""},
    )
    monkeypatch.setattr(
        openhop,
        "corescope_get",
        lambda *a, **k: (
            200,
            {"latest_ts": "2026-11-01T10:00:00Z", "selected": {"observer_name": "Montrave"}},
        ),
    )
    assert openhop.corescope_battery_check("https://cs", NODE) == (
        True,
        {"latest_ts": "2026-11-01T10:00:00Z", "by": "Montrave"},
    )


# -- found on the MeshBench bench ----------------------------------------------


def test_standby_with_no_reading_anywhere_gives_the_poller_a_window_first(
    tmp_path, fake, monkeypatch, sent
):
    # CoreScope has no reading yet (a new repeater): a standby must not step
    # in at once, or every standby polls alongside the poller.
    serve_owners(
        monkeypatch,
        {
            NODE: {
                "owner": "ee" * 32,
                "owner_name": "Montrave",
                "mine": False,
                "role": "standby",
                "owner_hops": 0,
            }
        },
    )
    serve_latest(monkeypatch, None)
    a, _ = ready(tmp_path, fake, monkeypatch)
    assert a.poll([NODE]) == [] and sent == []
    st = a.state.node(NODE)
    assert st["skip"]["reason"] == "standby"
    assert abs(st["next_due_at"] - (st["standby_since"] + 3600 + 1800)) < 1
    # Window plus grace on standby and still nothing: step in.
    st["standby_since"] -= 3600 + 1800 + 1
    st.pop("next_due_at")
    a._last_scheduled = 0
    a.poll([NODE])
    assert sent == [NODE]


def test_owners_latest_reading_counts_when_the_battery_lookup_has_none(
    tmp_path, fake, monkeypatch, sent
):
    now = time.time()
    serve_owners(
        monkeypatch,
        {
            NODE: {
                "owner": "ee" * 32,
                "owner_name": "Montrave",
                "mine": False,
                "role": "standby",
                "owner_hops": 0,
                "latest_ts": utc_iso(now - 600),
            }
        },
    )
    serve_latest(monkeypatch, None)
    a, _ = ready(tmp_path, fake, monkeypatch)
    a.state.node(NODE)["standby_since"] = now - 10 * 3600
    assert a.poll([NODE]) == [] and sent == []
    assert abs(a.state.node(NODE)["next_due_at"] - (now - 600 + 3600 + 1800)) < 5


@pytest.mark.startup_hold
def test_after_a_start_scheduled_polls_wait_for_the_claim_and_owners(
    tmp_path, fake, monkeypatch, sent
):
    owners_calls = []
    serve_owners(monkeypatch, {}, calls=owners_calls)
    serve_latest(monkeypatch, None)
    a, _ = ready(tmp_path, fake, monkeypatch)
    assert a.poll([NODE]) == [] and sent == []
    assert a.state.node(NODE)["skip"]["reason"] == "starting"
    # The claim goes out and CoreScope answers after it: polling starts.
    a._claims_sent_at = time.time()
    a._owners["at"] = 0.0
    a.owners(time.time() + 1)
    a._last_scheduled = 0
    a.poll([NODE])
    assert sent == [NODE]


@pytest.mark.startup_hold
def test_the_startup_hold_runs_out_if_corescope_never_answers(tmp_path, fake, monkeypatch, sent):
    serve_latest(monkeypatch, None, reachable=False)
    a, _ = ready(tmp_path, fake, monkeypatch)
    a._started_at -= a._startup_hold_s() + 1
    a.poll([NODE])
    assert sent == [NODE]


def test_no_sensor_temperature_is_no_reading():
    import struct

    from openhop_corescope_observer_plus.companion import lpp_temperature

    lpp = lambda c: bytes([1, 103]) + struct.pack(">h", int(c * 10))
    assert lpp_temperature(lpp(-128.0)) is None
    assert lpp_temperature(lpp(16.5)) == 16.5


def test_freshness_uses_the_newest_reading_of_any_source(monkeypatch):
    # CoreScope's latest_ts is the shown (best heard) source's; a newer
    # reading from another observer is what counts for the window.
    from openhop_corescope_observer_plus import openhop as oh

    body = {
        "latest_ts": "2026-11-01T15:24:13Z",
        "selected": {"observer_name": "CP-Station-C"},
        "sources": [
            {"observer_name": "CP-Station-C", "latest_ts": "2026-11-01T15:24:13Z"},
            {"observer_name": "CP-Station-A", "latest_ts": "2026-11-01T15:28:03Z"},
        ],
    }
    monkeypatch.setattr(oh, "corescope_get", lambda *a, **k: (200, body))
    assert oh.corescope_battery_check("https://cs.example", NODE) == (
        True,
        {"latest_ts": "2026-11-01T15:28:03Z", "by": "CP-Station-A"},
    )


def test_standbys_queue_so_only_the_first_steps_in(tmp_path, fake, monkeypatch, sent):
    now = time.time()
    node = {
        "owner": "ee" * 32,
        "owner_name": "Montrave",
        "mine": False,
        "role": "standby",
        "owner_hops": 0,
    }
    serve_owners(monkeypatch, {NODE: {**node, "standby_rank": 2}})
    # 1 h 40 min old: past window + one grace, inside window + two graces.
    serve_latest(monkeypatch, utc_iso(now - 6000), by="Montrave")
    a, _ = ready(tmp_path, fake, monkeypatch)
    assert a.poll([NODE]) == [] and sent == []
    assert abs(a.state.node(NODE)["next_due_at"] - (now - 6000 + 3600 + 2 * 1800)) < 5
    # The first in the queue goes at that age.
    serve_owners(monkeypatch, {NODE: {**node, "standby_rank": 1}})
    a._owners["at"] = 0.0
    a.state.node(NODE).pop("next_due_at")
    a._last_scheduled = 0
    a.poll([NODE])
    assert sent == [NODE]


@pytest.mark.startup_hold
def test_a_standbys_wait_restarts_when_a_pause_lifts(tmp_path, fake, monkeypatch, sent):
    now = time.time()
    serve_owners(
        monkeypatch,
        {
            NODE: {
                "owner": "ee" * 32,
                "owner_name": "Montrave",
                "mine": False,
                "role": "standby",
                "owner_hops": 0,
                "standby_rank": 1,
            }
        },
    )
    serve_latest(
        monkeypatch, utc_iso(now - 20 * 3600), by="Montrave"
    )  # long past, during the pause
    a, _ = ready(tmp_path, fake, monkeypatch)
    monkeypatch.setattr(App, "_starting", lambda self, t: False)
    a._started_at = now - 24 * 3600
    a._gated_at = now - 60  # the pause lifted a minute ago
    assert a.poll([NODE]) == [] and sent == []
    assert abs(a.state.node(NODE)["next_due_at"] - (now - 60 + 3600 + 1800)) < 5
