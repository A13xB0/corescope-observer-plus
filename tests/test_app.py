import json
import os
import stat

import yaml

from fake_companion import FakeCompanion
from openhop_neighbour_battery import app as app_mod
from openhop_neighbour_battery.app import App
from openhop_neighbour_battery.openhop import Neighbour, load_openhop_config
from openhop_neighbour_battery.settings import parse

NODE = "02f1" + "ab" * 30


def write_openhop_config(tmp_path, port):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "repeater": {"node_name": "Cadham Village", "identity_key": os.urandom(64)},
        "mqtt_brokers": {"iata_code": "EDI", "brokers": [
            {"name": "ScotMesh", "host": "mqtt.example", "port": 443, "format": "meshcoretomqtt",
             "use_jwt_auth": True, "audience": "mqtt.example", "enabled": True,
             "tls": {"enabled": True, "insecure": True}},
            {"preset": "letsmesh"},
        ]},
        "identities": {"companions": [
            {"name": "NeighbourBattery", "identity_key": os.urandom(32).hex(),
             "settings": {"tcp_port": port, "bind_address": "127.0.0.1"}},
        ]},
    }))
    return path


def make_app(tmp_path, fake, **cfg):
    data = tmp_path / "data"
    data.mkdir()
    conf = {"openhop_config_path": str(write_openhop_config(tmp_path, fake.port)), **cfg}
    (data / "config.json").write_text(json.dumps(conf))
    a = App(data)
    a.load_settings()
    return a, data


def test_secrets_leave_config_and_are_private(tmp_path):
    fake = FakeCompanion()
    try:
        a, data = make_app(tmp_path, fake, api_token_input="tok-123", password_updates={NODE: "pw-456"})
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
        a, data = make_app(tmp_path, fake, enabled=True, targets=[NODE], brokers=["ScotMesh"],
                           spacing_seconds=5)
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
    s = parse({"targets": [NODE.upper(), "short", NODE], "interval_minutes": 5, "brokers": ["A", 3]})
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
    monkeypatch.setattr(app_mod, "corescope_battery_snapshot",
                        lambda url, nid, **k: {"latest_ts": "2026-10-26T09:00:00Z", "latest_mv": 3240,
                                               "status": "ok", "by": "West Lomond", "key_mode": ""})
    try:
        a, _ = make_app(tmp_path, fake, targets=[NODE], brokers=["ScotMesh"],
                        requests=[{"id": "c1", "type": "check_now"}])
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}, "publish": {"ok": True}}
        a.candidates = [Neighbour(NODE, "West Lomond", -5.0, -119, 0, True)]
        # Polled now although polling is off and the node was just read.
        a.state.node(NODE)["last_attempt"] = 10 ** 12
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
    from openhop_neighbour_battery.openhop import Route
    fake = FakeCompanion()
    try:
        a, _ = make_app(tmp_path, fake, enabled=True, targets=[NODE])
        monkeypatch.setattr(a, "refresh", lambda: None)
        a.ohcfg = load_openhop_config(tmp_path / "config.yaml")
        a.check = {"companion": {"ok": True}}
        via = Route(relay_id="4128a6" + "11" * 29, relay_name="sco-edi-garvald", hash_size=3, adverts=3)
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
    s = parse({"manual_repeaters": [{"node_id": NODE.upper(), "name": "Redstone"}, {"node_id": "bad"}, "x"],
               "corescope_url": "https://cs.example/"})
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
    key = {"name": "CoreScope shared guest", "version": 1,
           "public_key": lid.get_public_key().hex(),
           "private_key": lid.get_signing_key_bytes().hex()}
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
        a, _ = make_app(tmp_path, fake, shared_key_targets=[NODE],
                        password_updates={NODE: "a-real-secret"})
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


def test_corescope_covers_skips_a_fresh_reading(tmp_path, monkeypatch):
    fake = FakeCompanion()
    try:
        import time as _t
        from openhop_neighbour_battery.poller import utc_iso
        a, _ = make_app(tmp_path, fake)
        now = _t.time()
        # A reading 5 minutes old is within a 60-minute window -> covered.
        monkeypatch.setattr(app_mod, "corescope_latest_battery_ts", lambda url, nid, **k: utc_iso(now - 300))
        assert a._corescope_covers(NODE, 3600, now) is True
        # Two hours old -> not covered.
        monkeypatch.setattr(app_mod, "corescope_latest_battery_ts", lambda url, nid, **k: utc_iso(now - 7200))
        assert a._corescope_covers(NODE, 3600, now) is False
        # Lookup failure fails open (not covered -> plugin retries).
        monkeypatch.setattr(app_mod, "corescope_latest_battery_ts", lambda url, nid, **k: None)
        assert a._corescope_covers(NODE, 3600, now) is False
    finally:
        fake.close()
