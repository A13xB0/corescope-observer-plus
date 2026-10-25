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
        assert a.actions["check_now"]["readings"] == 1
        assert a.actions["check_now"]["message"] == "1 of 1 read"
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
