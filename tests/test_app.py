import json
import os
import stat

import yaml

from fake_companion import FakeCompanion
from openhop_corescope_observer_plus import app as app_mod
from openhop_corescope_observer_plus.app import App
from openhop_corescope_observer_plus.openhop import Neighbour, load_openhop_config
from openhop_corescope_observer_plus.settings import parse

NODE = "02f1" + "ab" * 30


def _expanded_key() -> bytes:
    """A MeshCore-style 64-byte repeater key: SHA-512 of a seed with the scalar
    clamped, as real repeaters have (random bytes alone don't sign consistently)."""
    import hashlib

    k = bytearray(hashlib.sha512(os.urandom(32)).digest())
    k[0] &= 248
    k[31] &= 63
    k[31] |= 64
    return bytes(k)


def write_openhop_config(tmp_path, port):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "repeater": {"node_name": "Cadham Village", "identity_key": _expanded_key()},
                "mqtt_brokers": {
                    "iata_code": "EDI",
                    "brokers": [
                        {
                            "name": "ScotMesh",
                            "host": "mqtt.example",
                            "port": 443,
                            "format": "meshcoretomqtt",
                            "use_jwt_auth": True,
                            "audience": "mqtt.example",
                            "enabled": True,
                            "tls": {"enabled": True, "insecure": True},
                        },
                        {"preset": "letsmesh"},
                    ],
                },
                "identities": {
                    "companions": [
                        {
                            "name": "NeighbourBattery",
                            "identity_key": os.urandom(32).hex(),
                            "settings": {"tcp_port": port, "bind_address": "127.0.0.1"},
                        },
                    ]
                },
            }
        )
    )
    return path


def make_app(tmp_path, fake, **cfg):
    data = tmp_path / "data"
    data.mkdir()
    conf = {"openhop_config_path": str(write_openhop_config(tmp_path, fake.port)), **cfg}
    # Tests name the repeaters on the guest key; everything else stays off it.
    conf.setdefault("guest_key_default", False)
    conf["guest_key_overrides"] = dict.fromkeys(conf.pop("shared_key_targets", []), True)
    (data / "config.json").write_text(json.dumps(conf))
    a = App(data)
    a.load_settings()
    return a, data


def test_secrets_leave_config_and_are_private(tmp_path):
    fake = FakeCompanion()
    try:
        a, data = make_app(
            tmp_path, fake, api_token_input="tok-123", password_updates={NODE: "pw-456"}
        )
        cfg = json.loads((data / "config.json").read_text())
        assert "api_token_input" not in cfg and "password_updates" not in cfg
        assert a.secrets.api_token == "tok-123" and a.secrets.password(NODE) == "pw-456"
        mode = stat.S_IMODE(os.stat(data / "secrets.json").st_mode)
        assert mode == 0o600
        a.write_runtime()
        runtime = (data / "runtime.json").read_text()
        assert "tok-123" not in runtime and "pw-456" not in runtime
    finally:
        fake.close()


def test_poll_cycle_queues_and_publishes(tmp_path, monkeypatch):
    fake = FakeCompanion()
    published = []

    class P:
        def __init__(self, cfg):
            pass

        def publish(self, broker, readings):
            published.append((broker.name, list(readings)))

    monkeypatch.setattr(app_mod, "Publisher", P)
    try:
        a, data = make_app(
            tmp_path, fake, enabled=True, targets=[NODE], brokers=["ScotMesh"], spacing_seconds=5
        )
        a.secrets.set_api_token("t")
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}, "publish": {"ok": True}}
        a.candidates = [Neighbour(NODE, "West Lomond", -5.0, -119, 0, True)]
        a.tick()
        assert published and published[0][0] == "ScotMesh"
        assert published[0][1][0]["node_id"] == NODE and published[0][1][0]["battery_mv"] == 3940
        # Not due again within the interval.
        fake.commands.clear()
        a.tick()
        assert fake.commands == []
        runtime = json.loads((data / "runtime.json").read_text())
        assert runtime["nodes"][0]["battery_mv"] == 3940
        assert runtime["brokers"][0]["last_count"] == 1
    finally:
        fake.close()


def test_non_direct_targets_are_never_polled(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        a, _ = make_app(tmp_path, fake, enabled=True, targets=[NODE])
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}}
        a.candidates = [Neighbour(NODE, "Far Away", 3.0, -90, 0, False)]
        assert a.poll([NODE]) == []
        assert fake.commands == []
        assert a.state.node(NODE)["code"] == "not_direct"
    finally:
        fake.close()


def test_preset_broker_entries_are_skipped(tmp_path):
    cfg = load_openhop_config(write_openhop_config(tmp_path, 1))
    assert [b.name for b in cfg.brokers] == ["ScotMesh"]
    assert cfg.iata_code == "EDI" and len(cfg.identity_key) == 64


def test_settings_validation():
    s = parse(
        {"targets": [NODE.upper(), "short", NODE], "interval_minutes": 5, "brokers": ["A", 3]}
    )
    assert s.targets == [NODE]
    assert s.interval_minutes == 30
    assert s.brokers == ["A"]
    assert s.enabled is False


def test_check_now_polls_every_target_and_publishes(tmp_path, monkeypatch):
    fake = FakeCompanion()
    published = []

    class P:
        def __init__(self, cfg):
            pass

        def publish(self, broker, readings):
            published.append(list(readings))

    monkeypatch.setattr(app_mod, "Publisher", P)
    # No network and no delay in the CoreScope confirmation pass.
    monkeypatch.setattr(app_mod, "CORESCOPE_SETTLE_S", 0)
    monkeypatch.setattr(
        app_mod,
        "corescope_battery_snapshot",
        lambda url, nid, **k: {
            "latest_ts": "2026-10-26T09:00:00Z",
            "latest_mv": 3240,
            "status": "ok",
            "by": "West Lomond",
            "key_mode": "",
        },
    )
    try:
        a, _ = make_app(
            tmp_path,
            fake,
            targets=[NODE],
            brokers=["ScotMesh"],
            requests=[{"id": "c1", "type": "check_now"}],
        )
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}, "publish": {"ok": True}}
        a.candidates = [Neighbour(NODE, "West Lomond", -5.0, -119, 0, True)]
        # Polled now although polling is off and the node was just read.
        a.state.node(NODE)["last_attempt"] = 10**12
        a.run_requests()
        assert published and published[0][0]["node_id"] == NODE
        act = a.actions["check_now"]
        assert act["readings"] == 1
        assert act["message"] == "1 of 1 read"
        assert act["running"] is False
        # The live view got a per-node row with its CoreScope confirmation.
        assert len(act["progress"]) == 1 and act["progress"][0]["node_id"] == NODE
        assert act["progress"][0]["corescope"]["reachable"] is True
    finally:
        fake.close()


def test_one_hop_targets_need_max_hops(tmp_path, monkeypatch):
    from openhop_corescope_observer_plus.openhop import Route

    fake = FakeCompanion()
    try:
        a, _ = make_app(tmp_path, fake, enabled=True, targets=[NODE])
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}}
        via = Route(
            relay_id="4128a6" + "11" * 29, relay_name="sco-edi-garvald", hash_size=3, adverts=3
        )
        a.candidates = [Neighbour(NODE, "Dalgety Bay", None, None, 0, False, routes=[via])]
        assert a.poll([NODE]) == []
        assert fake.commands == []
        a.settings.max_hops = 1
        out = a.poll([NODE])
        assert out and out[0]["reply_hops"] == 1 and out[0]["via"] == via.relay_id
        assert fake.contacts[bytes.fromhex(NODE)] == (0x81, bytes.fromhex("4128a6"))
    finally:
        fake.close()


def test_settings_new_options():
    s = parse({"max_hops": 3, "temperature_targets": [NODE, "x"], "alternative_routes": True})
    assert s.max_hops == 1 and s.temperature_targets == [NODE]
    assert s.alternative_routes


def test_manual_repeaters_and_corescope_url_settings():
    s = parse(
        {
            "manual_repeaters": [
                {"node_id": NODE.upper(), "name": "Redstone"},
                {"node_id": "bad"},
                "x",
            ],
            "corescope_url": "https://cs.example/",
        }
    )
    assert s.manual_repeaters == [{"node_id": NODE, "name": "Redstone"}]
    assert s.corescope_url == "https://cs.example"
    assert parse({"corescope_url": "ftp://nope"}).corescope_url == "https://corescope.scotmesh.net"


def _serve_key(monkeypatch):
    """Make CoreScope serve a usable shared key to the app under test."""
    import pytest

    pytest.importorskip("openhop_core")
    from openhop_core.protocol.identity import LocalIdentity

    while True:
        lid = LocalIdentity()
        if lid.get_public_key()[0] not in (0x00, 0xFF):
            break
    key = {
        "name": "CoreScope shared guest",
        "version": 1,
        "public_key": lid.get_public_key().hex(),
        "private_key": lid.get_signing_key_bytes().hex(),
    }
    key["_calls"] = calls = []

    def fake_fetch(url, **k):
        calls.append(url)
        return {k2: v for k2, v in key.items() if k2 != "_calls"}

    monkeypatch.setattr(app_mod, "fetch_shared_guest_key", fake_fetch)
    return key


def test_shared_key_fetched_from_corescope(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        key = _serve_key(monkeypatch)
        a, data = make_app(tmp_path, fake, shared_key_targets=[NODE])
        got = a.shared_key()
        assert got is not None and got.public_key.hex() == key["public_key"]
        assert a.uses_shared_key(NODE) is True
        a.write_runtime()
        rt = json.loads((data / "runtime.json").read_text())
        assert rt["shared_key"]["configured"] is True
        assert rt["shared_key"]["public"] == key["public_key"]
        # The private half is public by design, but config.json never stores the key.
        assert "shared_key_private" not in (data / "config.json").read_text()
    finally:
        fake.close()


def test_no_shared_key_when_corescope_serves_none(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        monkeypatch.setattr(app_mod, "fetch_shared_guest_key", lambda url, **k: None)
        a, _ = make_app(tmp_path, fake, shared_key_targets=[NODE])
        assert a.shared_key() is None
        assert a.uses_shared_key(NODE) is False  # opted in, but no key to use
    finally:
        fake.close()


def test_shared_key_never_used_with_a_password(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        _serve_key(monkeypatch)
        a, _ = make_app(
            tmp_path, fake, shared_key_targets=[NODE], password_updates={NODE: "a-real-secret"}
        )
        assert a.secrets.has_password(NODE)
        assert a.uses_shared_key(NODE) is False  # a saved password blocks the shared key
    finally:
        fake.close()


def test_shared_key_blocked_after_an_admin_answer(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        _serve_key(monkeypatch)
        a, _ = make_app(tmp_path, fake, shared_key_targets=[NODE])
        assert a.uses_shared_key(NODE) is True
        a.state.node(NODE)["shared_admin_seen"] = True
        assert a.uses_shared_key(NODE) is False
    finally:
        fake.close()


def test_shared_key_off_when_not_opted_in(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        key = _serve_key(monkeypatch)
        a, _ = make_app(tmp_path, fake)  # key available but no repeater opted in
        # With nothing opted in the plugin never calls CoreScope for the key.
        assert a.shared_key() is None
        assert a.uses_shared_key(NODE) is False
        assert key["_calls"] == [], "fetched the key with no shared targets"
    finally:
        fake.close()


def test_test_login_reports_live_steps(tmp_path, monkeypatch):
    # The Test action records a step for each stage so the dialog shows a
    # live console of what the poll is doing (login, status…).
    fake = FakeCompanion()
    monkeypatch.setattr(app_mod, "CORESCOPE_SETTLE_S", 0)
    monkeypatch.setattr(app_mod, "corescope_battery_snapshot", lambda *a, **k: None)
    try:
        a, _ = make_app(
            tmp_path,
            fake,
            targets=[NODE],
            requests=[{"id": "t1", "type": "test_login", "node_id": NODE}],
        )
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}}
        a.candidates = [Neighbour(NODE, "West Lomond", -5.0, -119, 0, True)]
        a.run_requests()
        act = a.actions["test_login"]
        assert act["running"] is False
        assert act["steps"], "no live steps recorded"
        labels = " ".join(s["label"].lower() for s in act["steps"])
        assert "login" in labels or "status" in labels

    finally:
        fake.close()


def test_check_now_stale_scope_skips_fresh(tmp_path, monkeypatch):
    # scope "stale" polls only targets with no reading within the window.
    import time as _t

    fake = FakeCompanion()
    monkeypatch.setattr(app_mod, "CORESCOPE_SETTLE_S", 0)
    monkeypatch.setattr(app_mod, "corescope_battery_snapshot", lambda *a, **k: None)

    class P:
        def __init__(self, cfg):
            pass

        def publish(self, broker, readings):
            pass

    monkeypatch.setattr(app_mod, "Publisher", P)
    other = "0aa0" + "cd" * 30
    try:
        a, _ = make_app(
            tmp_path,
            fake,
            targets=[NODE, other],
            brokers=["ScotMesh"],
            requests=[{"id": "c1", "type": "check_now", "scope": "stale"}],
        )
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}, "publish": {"ok": True}}
        a.candidates = [
            Neighbour(NODE, "West Lomond", -5.0, -119, 0, True),
            Neighbour(other, "Bowden", -6.0, -118, 0, True),
        ]
        # NODE read 2 minutes ago (fresh); `other` never read (stale).
        a.state.node(NODE)["last_success"] = _t.time() - 120
        a.run_requests()
        act = a.actions["check_now"]
        assert act["scope"] == "stale"
        polled = [p["node_id"] for p in act["progress"] if not p.get("skipped")]
        assert other in polled and NODE not in polled, polled
        # The skipped one is listed too, with why.
        skipped = [p for p in act["progress"] if p.get("skipped")]
        assert [p["node_id"] for p in skipped] == [NODE] and skipped[0]["skipped"] == "fresh_own"
        assert act["message"].endswith("· 1 skipped")
    finally:
        fake.close()


def test_failed_request_is_not_left_running(tmp_path, monkeypatch):
    # An exception mid-Test must still clear "running", or the page waits forever.
    fake = FakeCompanion()
    try:
        a, _ = make_app(
            tmp_path,
            fake,
            targets=[NODE],
            requests=[{"id": "t1", "type": "test_login", "node_id": NODE}],
        )

        def boom():
            raise RuntimeError("companion gone")

        monkeypatch.setattr(a, "refresh", boom)
        a.run_requests()
        act = a.actions["test_login"]
        assert act["ok"] is False and act["running"] is False
        assert "companion gone" in act["message"]
    finally:
        fake.close()


def _app_with_shared_target(tmp_path, monkeypatch, fake):
    a, data = make_app(
        tmp_path,
        fake,
        targets=[NODE],
        shared_key_targets=[NODE],
        corescope_url="https://corescope.example",
    )
    a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
    return a, data


def test_own_key_is_registered_signed_and_used(tmp_path, monkeypatch):
    from openhop_core import LocalIdentity
    from openhop_core.protocol.identity import Identity

    from openhop_corescope_observer_plus import guest_key

    fake = FakeCompanion()
    sent = []
    monkeypatch.setattr(
        app_mod,
        "register_guest_key",
        lambda url, body, **k: (
            sent.append(body),
            (200, {"ok": True, "expires_at": "2026-10-27T12:00:00Z"}),
        )[1],
    )
    try:
        a, data = _app_with_shared_target(tmp_path, monkeypatch, fake)
        key = a.polling_key(now=1_000_000.0)
        assert key is not None and key.public_key == a.own_key().public_key
        body = sent[0]
        observer = LocalIdentity(a.ohcfg.identity_key)
        assert body["observer_id"] == observer.get_public_key().hex()
        msg = guest_key.registration_message(
            body["observer_id"], body["public_key"], body["timestamp"]
        )
        assert Identity(observer.get_public_key()).verify(msg, bytes.fromhex(body["signature"]))
        assert stat.S_IMODE(os.stat(data / guest_key.KEY_FILE).st_mode) == 0o600
        # Re-registered only every 6 h, and the same key survives a restart.
        a.polling_key(now=1_000_000.0 + 3600)
        assert len(sent) == 1
        a.polling_key(now=1_000_000.0 + 6 * 3600 + 1)
        assert len(sent) == 2 and sent[1]["public_key"] == body["public_key"]
        assert guest_key.load_or_create(data).public_key.hex() == body["public_key"]
        rt = a._shared_key_runtime()
        assert rt["kind"] == "own"
    finally:
        fake.close()


def test_shared_key_when_corescope_takes_no_own_keys(tmp_path, monkeypatch):
    fake = FakeCompanion()
    key = _serve_key(monkeypatch)
    monkeypatch.setattr(app_mod, "register_guest_key", lambda url, body, **k: (404, {}))
    try:
        a, _ = _app_with_shared_target(tmp_path, monkeypatch, fake)
        got = a.polling_key(now=1_000_000.0)
        assert got is not None and got.public_key.hex() == key["public_key"]
        assert a._shared_key_runtime()["kind"] == "shared"
    finally:
        fake.close()


def test_repeater_owned_by_another_observer_is_left_to_them(tmp_path, monkeypatch):
    fake = FakeCompanion()
    monkeypatch.setattr(app_mod, "register_guest_key", lambda url, body, **k: (200, {"ok": True}))
    monkeypatch.setattr(
        app_mod,
        "corescope_owners",
        lambda url, obs, **k: {
            "nodes": {NODE: {"owner": "cc" * 32, "owner_name": "Cadham Village", "mine": False}}
        },
    )
    calls = []

    def fake_poll(*a, **k):
        calls.append(1)
        from openhop_corescope_observer_plus.poller import OK, PollResult

        return PollResult(OK, battery_mv=4000, key_mode="shared")

    monkeypatch.setattr(app_mod, "poll_one_shared", fake_poll)
    try:
        a, _ = _app_with_shared_target(tmp_path, monkeypatch, fake)
        a.check = {"companion": {"ok": True}}
        a.candidates = [Neighbour(NODE, "West Lomond", -5.0, -119, 0, True)]
        a.poll([NODE])
        # An older CoreScope (no roles): not mine means standby, which holds
        # while CoreScope can't say whether the owner's reading is fresh.
        skip = a.state.node(NODE)["skip"]
        assert calls == [] and skip["reason"] == "standby" and skip["by"] == "Cadham Village"
        a.poll([NODE], force=True)  # Test / Check now still poll
        assert calls == [1]
    finally:
        fake.close()


def test_never_uploads_the_repeaters_own_key():
    import pytest
    from openhop_core import LocalIdentity

    from openhop_corescope_observer_plus import guest_key
    from openhop_corescope_observer_plus import shared_key as sk

    ident = LocalIdentity()
    same = sk.SharedKey("x", 1, ident.get_public_key(), ident.get_signing_key_bytes())
    with pytest.raises(ValueError):
        guest_key.registration_body(ident, same)


GK_OTHER = "cd" * 32


def test_guest_key_default_covers_new_repeaters_unless_overridden():
    s = parse(
        {
            "targets": [NODE, GK_OTHER],
            "guest_key_default": True,
            "guest_key_overrides": {GK_OTHER: False},
        }
    )
    assert s.shared_key_targets == [NODE]
    s = parse(
        {
            "targets": [NODE, GK_OTHER],
            "guest_key_default": False,
            "guest_key_overrides": {GK_OTHER: True},
        }
    )
    assert s.shared_key_targets == [GK_OTHER]
    assert parse({"targets": [NODE]}).shared_key_targets == [NODE]  # on by default


def test_an_old_opt_in_list_moves_everything_to_the_default(tmp_path):
    fake = FakeCompanion()
    try:
        a, data = make_app(tmp_path, fake, targets=[NODE, GK_OTHER])
        saved = {
            k: v
            for k, v in json.loads((data / "config.json").read_text()).items()
            if not k.startswith("guest_key")
        }
        (data / "config.json").write_text(json.dumps({**saved, "shared_key_targets": [NODE]}))
        a.load_settings()
        assert a.settings.shared_key_targets == sorted([NODE, GK_OTHER])
        now = json.loads((data / "config.json").read_text())
        assert "shared_key_targets" not in now and now["guest_key_default"] is True
        assert now["guest_key_overrides"] == {}
    finally:
        fake.close()


def test_the_default_never_restarts_a_repeater_that_answered_as_admin(tmp_path):
    fake = FakeCompanion()
    try:
        a, _ = make_app(tmp_path, fake, targets=[NODE], guest_key_default=True)
        a.state.node(NODE)["shared_admin_seen"] = True
        a.load_settings()
        assert a.state.node(NODE)["shared_admin_seen"] and a.uses_shared_key(NODE) is False
    finally:
        fake.close()


def test_an_admin_answer_stays_off_with_the_default_on(tmp_path):
    from openhop_corescope_observer_plus.poller import PollResult

    fake = FakeCompanion()
    try:
        a, data = make_app(tmp_path, fake, targets=[NODE], guest_key_default=True)
        assert a.settings.shared_key_targets == [NODE]
        a._disable_shared_key(
            Neighbour(NODE, "West Lomond", 1.0, -100, 0, True), PollResult("admin_stop")
        )
        a.load_settings()
        assert a.settings.shared_key_targets == [] and a.settings.guest_key_overrides == {
            NODE: False
        }
    finally:
        fake.close()


def test_activity_counts_answers_no_answers_and_floods_by_day():
    from openhop_corescope_observer_plus import packet_log as pl

    stats = {}
    log = pl.PacketLog([], stats=stats)
    n = log.for_node(NODE, "West Lomond", "guest key")
    a = n.sent("telemetry request", "direct")
    n.answered(a, "radio", flooded=False)
    b = n.sent("telemetry request", "direct")
    n.answered(b, "radio", flooded=True)
    c = n.sent("telemetry request", "direct")
    n.unanswered(c)
    n.none_expected(n.sent("route", "direct"))
    # A reply CoreScope finds later turns the no answer into a success, once.
    n.answered(c, "CoreScope", flooded=False)
    n.answered(c, "CoreScope", flooded=False)
    t = log.summary()["today"]
    assert (t["answered"], t["no_answer"], t["flooded"]) == (3, 0, 1)
    assert t["flooded_by"] == [{"name": "West Lomond", "count": 1}]
    assert pl.PacketLog([], stats=stats).summary()["week"]["answered"] == 3  # survives a restart
