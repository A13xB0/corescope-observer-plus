import base64
import json
import os

import pytest
from nacl.signing import VerifyKey
from openhop_core import LocalIdentity

from openhop_corescope_observer_plus import publisher as pub
from openhop_corescope_observer_plus.openhop import Broker, OpenHopConfig

KEY = os.urandom(32)


def cfg():
    return OpenHopConfig(node_name="Cadham Village", identity_key=KEY, iata_code="EDI")


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_is_signed_by_the_repeater_key():
    ident = LocalIdentity(KEY)
    token = pub.make_jwt(ident, "scotmesh-mqtt.example")
    head, body, sig = token.split(".")
    claims = json.loads(b64d(body))
    assert json.loads(b64d(head)) == {"alg": "Ed25519", "typ": "JWT"}
    assert claims["publicKey"] == ident.get_public_key().hex().upper()
    assert claims["aud"] == "scotmesh-mqtt.example"
    assert claims["exp"] > claims["iat"]
    VerifyKey(ident.get_public_key()).verify(f"{head}.{body}".encode(), bytes.fromhex(sig))


def test_firmware_expanded_key_signs_too():
    # openHop stores MeshCore 64-byte expanded keys ([clamped scalar][nonce]).
    scalar = bytearray(os.urandom(32))
    scalar[0] &= 248
    scalar[31] = (scalar[31] & 63) | 64
    ident = LocalIdentity(bytes(scalar) + os.urandom(32))
    token = pub.make_jwt(ident, "aud")
    head, body, sig = token.split(".")
    VerifyKey(ident.get_public_key()).verify(f"{head}.{body}".encode(), bytes.fromhex(sig))


def test_topic_and_payload():
    p = pub.Publisher(cfg())
    b = Broker(name="ScotMesh", host="h", port=443, format="meshcoretomqtt")
    assert pub.topic_for(b, p.cfg, p.public_key_hex) == f"meshcore/EDI/{p.public_key_hex}/battery"
    custom = Broker(name="Home", host="h", port=1883, format="mqtt", base_topic="home/obs/")
    assert pub.topic_for(custom, p.cfg, p.public_key_hex) == "home/obs/battery"
    payload = pub.build_payload(p.cfg, p.public_key_hex, [{"node_id": "x"}])
    assert payload["v"] == 1 and payload["origin_id"] == p.public_key_hex
    assert payload["origin"] == "Cadham Village"
    # The claim topic shares the structure, with a polling leaf.
    assert (
        pub._topic(b, p.cfg, p.public_key_hex, "polling")
        == f"meshcore/EDI/{p.public_key_hex}/polling"
    )
    assert pub._topic(custom, p.cfg, p.public_key_hex, "polling") == "home/obs/polling"


def test_client_id_differs_from_openhops():
    p = pub.Publisher(cfg())
    b = Broker(name="ScotMesh", host="mqtt.example", port=443, format="meshcoretomqtt")
    assert p.client_id(b) == f"meshcore_{p.public_key_hex}_mqtt.example_battery"
    assert p.client_id(b) != f"meshcore_{p.public_key_hex}_mqtt.example_{b.format}"


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.published = []
        self.will = None
        self.on_connect = None
        FakeClient.instances.append(self)

    def ws_set_options(self, path):
        self.ws_path = path

    def tls_set(self, **kw):
        self.tls = True

    def tls_insecure_set(self, v):
        self.insecure = v

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def will_set(self, *a, **k):
        self.will = (a, k)

    def connect_async(self, host, port, keepalive):
        self.addr = (host, port)

    def loop_start(self):
        self.on_connect(self, None, None, 0)

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, json.loads(payload), qos, retain))

        class Info:
            def wait_for_publish(self, t):
                pass

            def is_published(self):
                return True

        return Info()

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        pass


def test_publish_never_retains_and_sets_no_last_will(monkeypatch):
    monkeypatch.setattr(pub.mqtt, "Client", FakeClient)
    p = pub.Publisher(cfg())
    b = Broker(
        name="ScotMesh",
        host="mqtt.example",
        port=443,
        transport="websockets",
        tls=True,
        use_jwt_auth=True,
        audience="mqtt.example",
        format="meshcoretomqtt",
    )
    p.publish(b, [{"node_id": "x", "battery_mv": 1}])
    fc = FakeClient.instances[-1]
    assert fc.will is None
    assert len(fc.published) == 1
    topic, payload, qos, retain = fc.published[0]
    assert topic.endswith("/battery") and qos == 1 and retain is False
    assert fc.auth[0] == f"v1_{p.public_key_hex}"
    assert fc.disconnected


def test_unknown_broker_format_refused(monkeypatch):
    monkeypatch.setattr(pub.mqtt, "Client", FakeClient)
    with pytest.raises(RuntimeError):
        pub.Publisher(cfg()).publish(Broker(name="x", host="h", port=1, format="weird"), [])


def test_publish_claim_sends_v2_nodes(monkeypatch):
    p = pub.Publisher(cfg())
    b = Broker(name="ScotMesh", host="h", port=443, format="meshcoretomqtt")
    captured = {}
    monkeypatch.setattr(
        p,
        "_send",
        lambda broker, topic, payload, role="battery": captured.update(
            topic=topic, role=role, payload=json.loads(payload)
        ),
    )
    p.publish_claim(
        b,
        [
            {"node_id": "ab" * 32, "reply_hops": 0, "via": None, "key": "own", "snr": 6.5},
            {"node_id": "cd" * 32, "reply_hops": 1, "via": "ef" * 32, "key": "shared", "snr": None},
        ],
        policy_version="a1b2c3d4",
        community=True,
    )
    assert captured["topic"] == f"meshcore/EDI/{p.public_key_hex}/polling"
    payload = captured["payload"]
    assert payload["nodes"][0] == {
        "node_id": "ab" * 32,
        "reply_hops": 0,
        "via": None,
        "key": "own",
        "snr": 6.5,
    }
    assert payload["nodes"][1] == {
        "node_id": "cd" * 32,
        "reply_hops": 1,
        "via": "ef" * 32,
        "key": "shared",
        "snr": None,
    }
    assert payload["v"] == 2 and payload["origin_id"] == p.public_key_hex and payload["origin"]
    assert payload["plugin"] == f"openhop.corescope_observer_plus/{pub.__version__}"
    assert payload["policy_version"] == "a1b2c3d4" and payload["community"] is True
    assert set(payload) == {
        "v",
        "origin",
        "origin_id",
        "timestamp",
        "plugin",
        "policy_version",
        "community",
        "nodes",
    }
    assert captured["role"] == "claim"
