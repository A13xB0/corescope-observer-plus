"""openHop's HTTP API, and working out routes from the adverts it recorded."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import requests

from .models import JsonDict, ManualRepeater, Neighbour, OpenHopError, Route

ADVERT_PACKET_TYPE = 4
ADVERT_PACKET_LIMIT = 10000


class OpenHopAPI:
    """openHop's HTTP API with an API token (X-API-Key)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, **params: Any) -> Any:
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

    def _post(self, path: str, body: JsonDict) -> Any:
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
        latest: dict[str, JsonDict] = {}
        for row in rows:
            key = str(row.get("pubkey") or "").lower()
            if len(key) != 64:
                continue
            seen = float(row.get("last_seen") or row.get("timestamp") or 0)
            if key not in latest or seen > float(latest[key].get("_seen", 0)):
                latest[key] = {**row, "_seen": seen}
        out = [
            Neighbour(
                node_id=key,
                name=str(row.get("node_name") or key[:8]),
                snr=_float(row.get("snr")),
                rssi=_int(row.get("rssi")),
                last_seen=float(row["_seen"]),
                zero_hop=bool(row.get("zero_hop")),
            )
            for key, row in latest.items()
        ]
        out.sort(key=lambda n: (not n.zero_hop, -(n.snr if n.snr is not None else -99)))
        return out

    def advert_packets(self, max_age_hours: int) -> list[JsonDict]:
        body = self._get(
            "/api/filtered_packets",
            type=ADVERT_PACKET_TYPE,
            limit=ADVERT_PACKET_LIMIT,
            start_timestamp=int(time.time() - max_age_hours * 3600),
        )
        rows = body.get("data", []) if isinstance(body, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def repeaters(
        self, max_age_hours: int, manual: list[ManualRepeater] | None = None
    ) -> list[Neighbour]:
        """Repeaters heard in the window, each with its direct and one-relay routes.

        A manual repeater counts as heard direct, advert or not.
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

    def mqtt_status(self) -> dict[str, JsonDict]:
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


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _float(v)
    return None if n is None else int(n)


def _path_hashes(row: JsonDict) -> list[str] | None:
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


def _advertised_key(row: JsonDict) -> str:
    return str(row.get("payload") or "")[:64].lower()


@dataclass
class _RouteTally:
    adverts: int = 0
    snr_sum: float = 0.0
    snr_count: int = 0
    hash_size: int = 1


def _tally_routes(
    by_id: dict[str, Neighbour], direct: list[Neighbour], packets: list[JsonDict]
) -> dict[tuple[str, str], _RouteTally]:
    tallies: dict[tuple[str, str], _RouteTally] = defaultdict(_RouteTally)
    for row in packets:
        key = _advertised_key(row)
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
        tally = tallies[(key, relay)]
        tally.adverts += 1
        if isinstance(row.get("snr"), (int, float)):
            tally.snr_sum += float(row["snr"])
            tally.snr_count += 1
        tally.hash_size = max(tally.hash_size, size)
    return tallies


def attach_routes(neighbours: list[Neighbour], packets: list[JsonDict]) -> None:
    """Fill each neighbour's routes from recorded advert packets, best first.

    A relay route is kept only when the relay's hash matches exactly one
    repeater heard direct, so a request can be addressed to it. The hash width
    is the one the relay forwarded the advert with, so it is known to understand it.
    """
    by_id = {n.node_id: n for n in neighbours}
    heard_direct = {n.node_id for n in neighbours if n.zero_hop}
    for row in packets:
        key = _advertised_key(row)
        if key in by_id and _path_hashes(row) == []:
            heard_direct.add(key)
    tallies = _tally_routes(by_id, [by_id[k] for k in heard_direct], packets)
    for n in neighbours:
        routes = [
            Route(
                relay_id=relay,
                relay_name=by_id[relay].name if relay else "",
                hash_size=t.hash_size,
                adverts=t.adverts,
                snr=t.snr_sum / t.snr_count if t.snr_count else None,
            )
            for (key, relay), t in tallies.items()
            if key == n.node_id
        ]
        if n.zero_hop and not any(r.hops == 0 for r in routes):
            routes.append(Route(adverts=0, snr=n.snr))
        routes.sort(key=lambda r: (-r.adverts, -(r.snr if r.snr is not None else -99)))
        n.routes = routes
