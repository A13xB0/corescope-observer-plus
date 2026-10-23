"""Everything the plugin reads from a stock openHop: its API and its config file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

DEFAULT_CONFIG_PATH = Path("/etc/openhop_repeater/config.yaml")
MC2MQTT_FORMATS = {"letsmesh", "meshcoretomqtt", "mc2mqtt"}


class OpenHopError(Exception):
    pass


@dataclass
class Neighbour:
    node_id: str  # lowercase 64-hex
    name: str
    snr: float | None
    rssi: int | None
    last_seen: float
    zero_hop: bool


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
    email: str = ""
    owner: str = ""
    brokers: list[Broker] = field(default_factory=list)
    companions: dict[str, dict] = field(default_factory=dict)  # name -> entry


class OpenHopAPI:
    """openHop's HTTP API with an API token (X-API-Key)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, **params) -> Any:
        try:
            r = requests.get(
                self.base_url + path,
                params=params,
                headers={"X-API-Key": self.token},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OpenHopError(f"openHop API unreachable: {exc}") from exc
        return self._unwrap(r)

    def _post(self, path: str, body: dict) -> Any:
        try:
            r = requests.post(
                self.base_url + path,
                json=body,
                headers={"X-API-Key": self.token},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OpenHopError(f"openHop API unreachable: {exc}") from exc
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: requests.Response) -> Any:
        if r.status_code == 401:
            raise OpenHopError("openHop rejected the API token")
        try:
            body = r.json()
        except ValueError as exc:
            raise OpenHopError(f"openHop answered HTTP {r.status_code} without JSON") from exc
        if isinstance(body, dict) and body.get("success") is False:
            raise OpenHopError(str(body.get("error") or "openHop reported an error"))
        return body

    def zero_hop_repeaters(self, max_age_hours: int) -> list[Neighbour]:
        """Repeaters whose latest advert in the window reached us at zero hops."""
        body = self._get(
            "/api/adverts_by_contact_type", contact_type="Repeater", hours=max_age_hours, limit=500
        )
        rows = body.get("data", []) if isinstance(body, dict) else []
        latest: dict[str, dict] = {}
        for row in rows:
            key = str(row.get("pubkey") or "").lower()
            if len(key) != 64:
                continue
            seen = float(row.get("last_seen") or row.get("timestamp") or 0)
            if key not in latest or seen > float(latest[key].get("_seen", 0)):
                latest[key] = {**row, "_seen": seen}
        out = []
        for key, row in latest.items():
            out.append(
                Neighbour(
                    node_id=key,
                    name=str(row.get("node_name") or key[:8]),
                    snr=_num(row.get("snr")),
                    rssi=_int(row.get("rssi")),
                    last_seen=float(row["_seen"]),
                    zero_hop=bool(row.get("zero_hop")),
                )
            )
        out.sort(key=lambda n: (not n.zero_hop, -(n.snr if n.snr is not None else -99)))
        return out

    def mqtt_status(self) -> dict[str, dict]:
        """Broker name -> {connected, enabled, format} from openHop's own connections."""
        body = self._get("/api/mqtt_status")
        data = body.get("data", body) if isinstance(body, dict) else {}
        brokers = data.get("brokers", []) if isinstance(data, dict) else []
        return {str(b.get("name")): b for b in brokers if isinstance(b, dict) and b.get("name")}

    def create_companion(self, name: str, port: int) -> None:
        self._post(
            "/api/create_identity",
            {
                "name": name,
                "type": "companion",
                "settings": {"node_name": name, "tcp_port": port, "bind_address": "127.0.0.1"},
            },
        )


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _key_bytes(raw) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return bytes.fromhex(raw.strip())
    raise OpenHopError("identity key has an unexpected type")


def load_openhop_config(path: Path = DEFAULT_CONFIG_PATH) -> OpenHopConfig:
    """Read the parts of openHop's config.yaml the plugin needs (read-only)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise OpenHopError(f"cannot read {path}: {exc.strerror or exc}") from exc
    repeater = raw.get("repeater") or {}
    key = repeater.get("identity_key")
    if key is None and repeater.get("identity_file"):
        try:
            key = Path(repeater["identity_file"]).read_bytes()
        except OSError as exc:
            raise OpenHopError(f"cannot read the repeater identity file: {exc}") from exc
    if key is None:
        raise OpenHopError("the repeater identity key is not in openHop's config")
    mqtt = raw.get("mqtt_brokers") or {}
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
    companions = {}
    for c in (raw.get("identities") or {}).get("companions") or []:
        if isinstance(c, dict) and c.get("name"):
            companions[str(c["name"])] = c
    return OpenHopConfig(
        node_name=str(repeater.get("node_name") or "openHop"),
        identity_key=_key_bytes(key),
        iata_code=str(mqtt.get("iata_code") or "TEST"),
        email=str(mqtt.get("email") or ""),
        owner=str(mqtt.get("owner") or ""),
        brokers=brokers,
        companions=companions,
    )


def companion_port(cfg: OpenHopConfig, name: str) -> int | None:
    entry = cfg.companions.get(name)
    if not entry:
        return None
    port = (entry.get("settings") or {}).get("tcp_port")
    return int(port) if port else None


