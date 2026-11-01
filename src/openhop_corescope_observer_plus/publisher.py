"""Publishing as the repeater's own observer, over a connection of the plugin's own.

No status, no retain and no last will, so this connection can't make the observer look offline.
"""

from __future__ import annotations

import base64
import binascii
import json
import ssl
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

import paho.mqtt.client as mqtt
from openhop_core import LocalIdentity
from paho.mqtt.enums import CallbackAPIVersion

from . import __version__
from .models import Claim, JsonDict
from .openhop_config import MC2MQTT_FORMATS, Broker, OpenHopConfig
from .timeutil import utc_iso

PLUGIN_TAG = f"openhop.corescope_observer_plus/{__version__}"
JWT_MINUTES = 10


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(identity: LocalIdentity, audience: str, now: datetime | None = None) -> str:
    """The MeshCore-style Ed25519 JWT openHop sends brokers (mqtt_handler._generate_jwt)."""
    now = now or datetime.now(timezone.utc)
    header = {"alg": "Ed25519", "typ": "JWT"}
    payload = {
        "publicKey": identity.get_public_key().hex().upper(),
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_MINUTES)).timestamp()),
        "email": "",
        "owner": "",
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = identity.sign(signing_input.encode())
    return f"{signing_input}.{binascii.hexlify(signature).decode()}"


def _topic(broker: Broker, cfg: OpenHopConfig, public_key_hex: str, leaf: str) -> str:
    if broker.base_topic:
        return f"{broker.base_topic.rstrip('/')}/{leaf}"
    if broker.format == "mqtt":
        return f"meshcore/repeater/{cfg.node_name}/{leaf}"
    return f"meshcore/{cfg.iata_code}/{public_key_hex}/{leaf}"


def topic_for(broker: Broker, cfg: OpenHopConfig, public_key_hex: str) -> str:
    return _topic(broker, cfg, public_key_hex, "battery")


def build_payload(cfg: OpenHopConfig, public_key_hex: str, readings: list[JsonDict]) -> JsonDict:
    return {
        "v": 1,
        "origin": cfg.node_name,
        "origin_id": public_key_hex,
        "timestamp": utc_iso(),
        "plugin": PLUGIN_TAG,
        "readings": readings,
    }


def build_claim(
    cfg: OpenHopConfig,
    public_key_hex: str,
    claims: list[Claim],
    *,
    policy_version: str = "",
    community: bool = True,
) -> JsonDict:
    """The claim v2 payload (topic meshcore/<region>/<observer>/polling)."""
    return {
        "v": 2,
        "origin": cfg.node_name,
        "origin_id": public_key_hex,
        "timestamp": utc_iso(),
        "plugin": PLUGIN_TAG,
        "policy_version": policy_version,
        "community": bool(community),
        "nodes": [
            {
                "node_id": c["node_id"],
                "reply_hops": int(c.get("reply_hops") or 0),
                "via": c.get("via") or None,
                "key": c.get("key") or "shared",
                "snr": c.get("snr"),
            }
            for c in claims
        ],
    }


class Publisher:
    def __init__(self, cfg: OpenHopConfig, *, timeout: float = 20.0):
        self.cfg = cfg
        self.identity = LocalIdentity(cfg.identity_key)
        self.public_key_hex = self.identity.get_public_key().hex().upper()
        self.timeout = timeout

    def client_id(self, broker: Broker, role: str = "battery") -> str:
        # Differs from openHop's own, and by role: a broker drops the older
        # session of a client ID, so a shared one would kick the other off.
        return f"meshcore_{self.public_key_hex}_{broker.host}_{role}"

    def publish(self, broker: Broker, readings: list[JsonDict]) -> None:
        """Connect, publish one battery batch at QoS 1, wait for the ack, disconnect."""
        payload = json.dumps(build_payload(self.cfg, self.public_key_hex, readings))
        self._send(broker, topic_for(broker, self.cfg, self.public_key_hex), payload)

    def publish_claim(
        self,
        broker: Broker,
        claims: list[Claim],
        *,
        policy_version: str = "",
        community: bool = True,
    ) -> None:
        """Claim v2: lets CoreScope pick one poller per repeater and credit a decoded
        reply with the right hops, which it can't see from the overheard reply."""
        self._send(
            broker,
            _topic(broker, self.cfg, self.public_key_hex, "polling"),
            json.dumps(
                build_claim(
                    self.cfg,
                    self.public_key_hex,
                    claims,
                    policy_version=policy_version,
                    community=community,
                )
            ),
            role="claim",
        )

    def _send(self, broker: Broker, topic: str, payload: str, role: str = "battery") -> None:
        """Connect, publish one message at QoS 1, wait for the ack, disconnect."""
        if broker.format not in MC2MQTT_FORMATS and broker.format != "mqtt":
            raise RuntimeError(f"unsupported broker format {broker.format!r}")

        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self.client_id(broker, role),
            transport=cast(Literal["tcp", "websockets", "unix"], broker.transport),
        )
        if broker.transport == "websockets":
            client.ws_set_options(path=broker.ws_path)
        if broker.tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.tls_insecure_set(broker.tls_insecure)
        if broker.use_jwt_auth:
            client.username_pw_set(
                f"v1_{self.public_key_hex}", make_jwt(self.identity, broker.audience or broker.host)
            )
        elif broker.username and broker.password:
            client.username_pw_set(broker.username, broker.password)

        connected = threading.Event()
        result: JsonDict = {}

        def on_connect(
            _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None
        ) -> None:
            result["rc"] = reason_code
            connected.set()

        client.on_connect = on_connect
        client.connect_async(broker.host, broker.port, keepalive=30)
        client.loop_start()
        try:
            if not connected.wait(self.timeout):
                raise RuntimeError("timed out connecting")
            rc = result.get("rc")
            if rc is None or getattr(rc, "is_failure", False):
                raise RuntimeError(f"broker refused the connection ({rc})")
            info = client.publish(topic, payload, qos=1, retain=False)
            info.wait_for_publish(self.timeout)
            if not info.is_published():
                raise RuntimeError("broker did not acknowledge the publish")
        finally:
            client.disconnect()
            client.loop_stop()
