"""The parts of openHop's config.yaml the plugin needs, read-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import JsonDict, OpenHopError

MC2MQTT_FORMATS = {"letsmesh", "meshcoretomqtt", "mc2mqtt"}


@dataclass
class Broker:
    name: str
    host: str
    port: int
    transport: str = "websockets"
    tls: bool = False
    tls_insecure: bool = False
    use_jwt_auth: bool = False
    audience: str = ""
    username: str | None = None
    password: str | None = None
    format: str = "letsmesh"
    base_topic: str | None = None
    enabled: bool = False
    ws_path: str = "/mqtt"


@dataclass
class OpenHopConfig:
    node_name: str
    identity_key: bytes
    iata_code: str
    brokers: list[Broker] = field(default_factory=list)
    companions: dict[str, JsonDict] = field(default_factory=dict)  # name -> entry


def _key_bytes(raw: Any) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return bytes.fromhex(raw.strip())
    raise OpenHopError("identity key has an unexpected type")


def _identity_key(repeater: JsonDict) -> Any:
    key = repeater.get("identity_key")
    if key is None and repeater.get("identity_file"):
        try:
            key = Path(repeater["identity_file"]).read_bytes()
        except OSError as exc:
            raise OpenHopError(f"cannot read the repeater identity file: {exc}") from exc
    if key is None:
        raise OpenHopError("the repeater identity key is not in openHop's config")
    return key


def _brokers(mqtt: JsonDict) -> list[Broker]:
    brokers = []
    for b in mqtt.get("brokers") or []:
        if not isinstance(b, dict) or "host" not in b or "name" not in b:
            continue  # preset references and partial overrides are skipped
        tls = b.get("tls") or {}
        brokers.append(
            Broker(
                name=str(b["name"]),
                host=str(b["host"]),
                port=int(b.get("port") or 1883),
                transport=str(b.get("transport") or "websockets"),
                tls=bool(tls.get("enabled")),
                tls_insecure=bool(tls.get("insecure")),
                use_jwt_auth=bool(b.get("use_jwt_auth")),
                audience=str(b.get("audience") or ""),
                username=b.get("username"),
                password=b.get("password"),
                format=str(b.get("format") or "letsmesh"),
                base_topic=b.get("base_topic"),
                enabled=bool(b.get("enabled")),
            )
        )
    return brokers


def load_openhop_config(path: Path) -> OpenHopConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise OpenHopError(f"cannot read {path}: {exc.strerror or exc}") from exc
    repeater = raw.get("repeater") or {}
    key = _identity_key(repeater)
    mqtt = raw.get("mqtt_brokers") or {}
    brokers = _brokers(mqtt)
    companions = {}
    for c in (raw.get("identities") or {}).get("companions") or []:
        if isinstance(c, dict) and c.get("name"):
            companions[str(c["name"])] = c
    return OpenHopConfig(
        node_name=str(repeater.get("node_name") or "openHop"),
        identity_key=_key_bytes(key),
        iata_code=str(mqtt.get("iata_code") or "TEST"),
        brokers=brokers,
        companions=companions,
    )


def companion_port(cfg: OpenHopConfig, name: str) -> int | None:
    entry = cfg.companions.get(name)
    if not entry:
        return None
    port = (entry.get("settings") or {}).get("tcp_port")
    return int(port) if port else None
