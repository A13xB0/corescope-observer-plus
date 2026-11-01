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
        return {
            "key": self.key,
            "hops": self.hops,
            "relay_id": self.relay_id,
            "relay_name": self.relay_name,
            "label": self.label,
            "adverts": self.adverts,
            "snr": None if self.snr is None else round(self.snr, 1),
        }


@dataclass
class Neighbour:
    node_id: str  # lowercase 64-hex
    name: str
    snr: float | None
    rssi: int | None
    last_seen: float
    zero_hop: bool
    routes: list[Route] = field(default_factory=list)  # best first
    manual: bool = False  # added by the operator, not found from an advert

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
            "/api/filtered_packets",
            type=ADVERT_PACKET_TYPE,
            limit=ADVERT_PACKET_LIMIT,
            start_timestamp=int(time.time() - max_age_hours * 3600),
        )
        rows = body.get("data", []) if isinstance(body, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def repeaters(self, max_age_hours: int, manual: list[dict] | None = None) -> list[Neighbour]:
        """Repeaters heard in the window, each with its direct and one-relay routes.

        manual lists repeaters the operator added by key; each counts as heard
        direct, whether or not one of its adverts arrived.
        """
        neighbours = self.zero_hop_repeaters(max_age_hours)
        by_id = {n.node_id: n for n in neighbours}
        for m in manual or []:
            n = by_id.get(m["node_id"])
            if n is None:
                n = Neighbour(m["node_id"], m["name"], None, None, 0, True)
                neighbours.append(n)
                by_id[n.node_id] = n
            n.zero_hop = True
            n.manual = True
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
            routes.append(
                Route(
                    relay_id=relay,
                    relay_name=by_id[relay].name if relay else "",
                    hash_size=size,
                    adverts=count,
                    snr=snr_sum / snr_n if snr_n else None,
                )
            )
        if n.zero_hop and not any(r.hops == 0 for r in routes):
            routes.append(Route(adverts=0, snr=n.snr))
        routes.sort(key=lambda r: (-r.adverts, -(r.snr if r.snr is not None else -99)))
        n.routes = routes


def search_corescope(base_url: str, query: str, *, timeout: float = 10.0) -> list[dict]:
    """Repeaters and room servers in CoreScope whose name or key matches query."""
    if not base_url:
        raise OpenHopError("no CoreScope address set")
    try:
        r = requests.get(
            f"{base_url}/api/nodes", params={"search": query, "limit": 20}, timeout=timeout
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise OpenHopError(f"CoreScope search failed: {exc}") from exc
    rows = body.get("nodes", []) if isinstance(body, dict) else body
    out = []
    for n in rows if isinstance(rows, list) else []:
        key = str(n.get("public_key") or "").lower()
        if len(key) != 64 or n.get("role") not in ("repeater", "room"):
            continue
        out.append(
            {
                "node_id": key,
                "name": str(n.get("name") or key[:8]),
                "role": n.get("role"),
                "last_seen": n.get("last_heard") or n.get("last_seen"),
                "lat": n.get("lat"),
                "lon": n.get("lon"),
            }
        )
    return out


def fetch_shared_guest_key(base_url: str, *, timeout: float = 5.0) -> dict | None:
    """The shared guest key CoreScope serves, or None.

    CoreScope holds the key; the plugin asks for it here. A key coming back
    both provides the key and signals the feature is live on this CoreScope.
    None on any error or an empty response, so the plugin simply has no shared
    key and polls with its own.
    """
    if not base_url:
        return None
    try:
        r = requests.get(f"{base_url}/api/shared-guest/key", timeout=timeout)
        if r.status_code != 200:
            return None
        body = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(body, dict) or not body.get("public_key") or not body.get("private_key"):
        return None
    return body


def corescope_latest_battery_ts(base_url: str, node_id: str, *, timeout: float = 5.0) -> str | None:
    """CoreScope's newest battery timestamp for a node (ISO), or None.

    None on any error, so the caller falls back to retrying rather than
    skipping. The timestamp is whatever observer reported last, which is the
    point: another observer's reading counts.
    """
    if not base_url:
        return None
    try:
        r = requests.get(
            f"{base_url}/api/nodes/{node_id}/battery", params={"days": 1}, timeout=timeout
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
        return None
    ts = body.get("latest_ts") if isinstance(body, dict) else None
    return ts if isinstance(ts, str) and ts else None


def corescope_battery_snapshot(base_url: str, node_id: str, *, timeout: float = 5.0) -> dict | None:
    """CoreScope's current battery view of a node, for live confirmation that a
    reading landed — the only "did it work" signal for a shared-key poll, whose
    reading the plugin never publishes (CoreScope decodes it from an observer's
    overheard reply). Returns {latest_ts, latest_mv, status, by, key_mode}, or
    None on any error.
    """
    if not base_url:
        return None
    try:
        r = requests.get(
            f"{base_url}/api/nodes/{node_id}/battery", params={"days": 1}, timeout=timeout
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    sel = body.get("selected") if isinstance(body.get("selected"), dict) else {}
    return {
        "latest_ts": body.get("latest_ts") or None,
        "latest_mv": body.get("latest_mv"),
        "status": body.get("status"),
        "by": sel.get("observer_name") or "",
        "key_mode": sel.get("key_mode") or "",
    }


def corescope_responses(
    base_url: str, since: str, *, timeout: float = 5.0, limit: int = 2000
) -> list[bytes] | None:
    """Every RESPONSE packet any CoreScope observer heard since ``since`` (ISO
    UTC), as raw wire bytes, or None on any error.

    This is the poller's wider antenna: a reply we missed ourselves but another
    observer heard is in here, still encrypted, and the poller decrypts it with
    its own key exactly as it would a frame off its own radio. Nothing about
    the key goes to CoreScope; it is a plain read of public packets.
    """
    if not base_url:
        return None
    try:
        r = requests.get(
            f"{base_url}/api/packets",
            params={"type": 1, "since": since, "limit": limit},
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
        return None
    rows = body.get("packets") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        raw = row.get("raw_hex") if isinstance(row, dict) else None
        try:
            out.append(bytes.fromhex(raw))
        except (TypeError, ValueError):
            continue
    return out


def register_guest_key(base_url: str, body: dict, *, timeout: float = 10.0) -> tuple[int, dict]:
    """POST this observer's polling key to CoreScope. Returns (HTTP status,
    JSON body); status 0 when CoreScope couldn't be reached. 404 means this
    CoreScope doesn't take per-observer keys (the caller uses the shared key)."""
    if not base_url:
        return 0, {"error": "no CoreScope address set"}
    try:
        r = requests.post(f"{base_url}/api/guest-keys", json=body, timeout=timeout)
    except requests.RequestException as exc:
        return 0, {"error": str(exc)}
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    return r.status_code, payload if isinstance(payload, dict) else {}


def corescope_owners(
    base_url: str, observer_id: str, plugin_version: str = "", *, timeout: float = 5.0
) -> dict | None:
    """Which observer CoreScope has chosen to poll each repeater this one
    claims: {"nodes": {node_id: {"owner", "owner_name", "mine", and on a
    CoreScope with community polling "role", "owner_hops", "latest_ts",
    "window_s", "next_due", "standby_due", "excluded"}}, "policy": {...}}.
    None on any error, so the caller polls everything itself (fails open)."""
    if not base_url:
        return None
    params = {"observer": observer_id}
    if plugin_version:
        params["plugin_version"] = plugin_version
    try:
        r = requests.get(f"{base_url}/api/guest-polling/owners", params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        body = r.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), dict):
        return None
    return body


# -- community polling (CoreScope policy, owners with roles, monitors) --------
#
# Each helper returns (status, body): status is the HTTP status, or 0 when
# CoreScope couldn't be reached (or answered something that isn't JSON). The
# caller tells "this CoreScope doesn't have it" (404) from "CoreScope is down"
# (0) because they fail safe differently.


def corescope_get(
    base_url: str, path: str, params: dict | None = None, *, timeout: float = 5.0
) -> tuple[int, Any]:
    if not base_url:
        return 0, None
    try:
        r = requests.get(f"{base_url}{path}", params=params or {}, timeout=timeout)
    except requests.RequestException:
        return 0, None
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except ValueError:
        return 0, None


def corescope_policy(
    base_url: str, observer_id: str, plugin_version: str, *, timeout: float = 5.0
) -> tuple[int, dict | None]:
    """GET /api/guest-polling/policy for this observer and plugin version."""
    params = {"plugin_version": plugin_version}
    if observer_id:
        params["observer"] = observer_id
    status, body = corescope_get(base_url, "/api/guest-polling/policy", params, timeout=timeout)
    if status == 200 and not isinstance(body, dict):
        return 0, None
    return status, body


def corescope_monitors(
    base_url: str, node_id: str, plugin_version: str, *, timeout: float = 5.0
) -> tuple[int, dict | None]:
    """GET /api/nodes/{pk}/monitors: every observer that claims the repeater."""
    status, body = corescope_get(
        base_url,
        f"/api/nodes/{node_id}/monitors",
        {"plugin_version": plugin_version},
        timeout=timeout,
    )
    if status == 200 and not isinstance(body, dict):
        return 0, None
    return status, body


def corescope_battery_check(
    base_url: str, node_id: str, *, timeout: float = 5.0
) -> tuple[bool, dict]:
    """The freshness check: (reachable, {latest_ts, by}). Unlike
    corescope_latest_battery_ts this tells "no reading yet" (reachable, no
    latest_ts) from "CoreScope is down" (not reachable)."""
    status, body = corescope_get(
        base_url, f"/api/nodes/{node_id}/battery", {"days": 1}, timeout=timeout
    )
    if status == 404:
        return True, {"latest_ts": None, "by": ""}  # CoreScope has no such node: nothing fresh
    if status != 200 or not isinstance(body, dict):
        return False, {}
    # latest_ts is the newest reading of the source CoreScope shows (the best
    # heard), not the newest reading: that is the newest of all the sources.
    sel = body.get("selected") if isinstance(body.get("selected"), dict) else {}
    newest = (body.get("latest_ts"), sel.get("observer_name"))
    for src in body.get("sources") or []:
        ts = src.get("latest_ts") if isinstance(src, dict) else None
        if isinstance(ts, str) and ts and (not isinstance(newest[0], str) or ts > newest[0]):
            newest = (ts, src.get("observer_name"))
    ts, by = newest
    return True, {"latest_ts": ts if isinstance(ts, str) and ts else None, "by": str(by or "")}
