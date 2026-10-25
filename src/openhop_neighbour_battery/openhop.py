"""Everything the plugin reads from a stock openHop: its API and its config file."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

DEFAULT_CONFIG_PATH = Path("/etc/openhop_repeater/config.yaml")
MC2MQTT_FORMATS = {"letsmesh", "meshcoretomqtt", "mc2mqtt"}


class OpenHopError(Exception):
    pass


ADVERT_PACKET_TYPE = 4
ADVERT_PACKET_LIMIT = 10000


@dataclass
class Route:
    """One way to reach a repeater: direct, or through one relay heard direct."""

    relay_id: str = ""  # lowercase 64-hex of the relay; "" for direct
    relay_name: str = ""
    hash_size: int = 1  # bytes per hop hash on this route
    adverts: int = 0  # the repeater's adverts heard this way in the window
    snr: float | None = None  # their average SNR

    @property
    def hops(self) -> int:
        return 1 if self.relay_id else 0

    @property
    def key(self) -> str:
        return self.relay_id or "direct"

    @property
    def path(self) -> bytes:
        return bytes.fromhex(self.relay_id)[: self.hash_size] if self.relay_id else b""

    @property
    def label(self) -> str:
        return f"via {self.relay_name or self.relay_id[:8]}" if self.relay_id else "direct"

    def as_dict(self) -> dict:
        return {"key": self.key, "hops": self.hops, "relay_id": self.relay_id,
                "relay_name": self.relay_name, "label": self.label, "adverts": self.adverts,
                "snr": None if self.snr is None else round(self.snr, 1)}


@dataclass
class Neighbour:
    node_id: str  # lowercase 64-hex
    name: str
    snr: float | None
    rssi: int | None
    last_seen: float
    zero_hop: bool
    routes: list[Route] = field(default_factory=list)  # best first

    def __post_init__(self):
        if self.zero_hop and not self.routes:
            self.routes = [Route(snr=self.snr)]

    @property
    def hops(self) -> int | None:
        """Fewest hops of any known route; None when it was only heard further away."""
        return min((r.hops for r in self.routes), default=0 if self.zero_hop else None)


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

    def advert_packets(self, max_age_hours: int) -> list[dict]:
        body = self._get(
            "/api/filtered_packets", type=ADVERT_PACKET_TYPE, limit=ADVERT_PACKET_LIMIT,
            start_timestamp=int(time.time() - max_age_hours * 3600),
        )
        rows = body.get("data", []) if isinstance(body, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def repeaters(self, max_age_hours: int) -> list[Neighbour]:
        """Repeaters heard in the window, each with its direct and one-relay routes."""
        neighbours = self.zero_hop_repeaters(max_age_hours)
        try:
            packets = self.advert_packets(max_age_hours)
        except OpenHopError:
            packets = []
        attach_routes(neighbours, packets)
        return neighbours

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




def _path_hashes(row: dict) -> list[str] | None:
    """The hop hashes an advert came through, as lowercase hex; None if unreadable."""
    raw = row.get("original_path")
    if raw in (None, "", "null"):
        return []
    try:
        hops = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(hops, list):
        return None
    out = [str(h).lower() for h in hops]
    return out if all(len(h) in (2, 4, 6) for h in out) else None


def attach_routes(neighbours: list[Neighbour], packets: list[dict]) -> None:
    """Fill each neighbour's routes from recorded advert packets, best first.

    A route through a relay is kept only when the relay's hash matches exactly
    one repeater heard direct, so a request can be addressed to it. The hash
    width is the one the relay itself forwarded the advert with, so the relay
    is known to understand it.
    """
    by_id = {n.node_id: n for n in neighbours}
    heard_direct = {n.node_id for n in neighbours if n.zero_hop}
    for row in packets:
        key = str(row.get("payload") or "")[:64].lower()
        if key in by_id and _path_hashes(row) == []:
            heard_direct.add(key)
    direct = [by_id[k] for k in heard_direct]
    counts: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, 0, 1])
    for row in packets:
        key = str(row.get("payload") or "")[:64].lower()
        if key not in by_id:
            continue
        hops = _path_hashes(row)
        if hops is None or len(hops) > 1:
            continue
        relay = ""
        size = 1
        if hops:
            matches = [n for n in direct if n.node_id.startswith(hops[0]) and n.node_id != key]
            if len(matches) != 1:
                continue
            relay = matches[0].node_id
            size = len(hops[0]) // 2
        c = counts[(key, relay)]
        c[0] += 1
        if isinstance(row.get("snr"), (int, float)):
            c[1] += float(row["snr"])
            c[2] += 1
        c[3] = max(c[3], size)
    for n in neighbours:
        routes = []
        for (key, relay), (count, snr_sum, snr_n, size) in counts.items():
            if key != n.node_id:
                continue
            routes.append(Route(
                relay_id=relay, relay_name=by_id[relay].name if relay else "", hash_size=size,
                adverts=count, snr=snr_sum / snr_n if snr_n else None,
            ))
        if n.zero_hop and not any(r.hops == 0 for r in routes):
            routes.append(Route(adverts=0, snr=n.snr))
        routes.sort(key=lambda r: (-r.adverts, -(r.snr if r.snr is not None else -99)))
        n.routes = routes
