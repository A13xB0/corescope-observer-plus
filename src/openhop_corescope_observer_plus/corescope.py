"""CoreScope's HTTP API.

Where a helper returns (status, body), status 0 means CoreScope couldn't be
reached: callers tell that from 404 (no such feature) because they fail safe
differently.
"""

from __future__ import annotations

from typing import Any, TypedDict

import requests

from .models import JsonDict, OpenHopError

TIMEOUT = 5.0


class BatteryCheck(TypedDict, total=False):
    latest_ts: str | None
    by: str


def get(
    base_url: str, path: str, params: JsonDict | None = None, *, timeout: float = TIMEOUT
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


def _json(base_url: str, path: str, params: JsonDict, timeout: float) -> Any:
    """The JSON body; raises on an HTTP error, a dead link or a body that isn't JSON."""
    r = requests.get(f"{base_url}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def search_nodes(base_url: str, query: str, *, timeout: float = 10.0) -> list[JsonDict]:
    """Repeaters and room servers whose name or key matches query."""
    if not base_url:
        raise OpenHopError("no CoreScope address set")
    try:
        body = _json(base_url, "/api/nodes", {"search": query, "limit": 20}, timeout)
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


def shared_guest_key(base_url: str) -> JsonDict | None:
    """The shared guest key CoreScope serves, or None (the plugin then polls with its own)."""
    status, body = get(base_url, "/api/shared-guest/key")
    if status != 200 or not isinstance(body, dict):
        return None
    if not body.get("public_key") or not body.get("private_key"):
        return None
    return body


def register_guest_key(
    base_url: str, body: JsonDict, *, timeout: float = 10.0
) -> tuple[int, JsonDict]:
    """POST this observer's polling key. 404 means this CoreScope doesn't take them."""
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


def owners(base_url: str, observer_id: str, plugin_version: str = "") -> JsonDict | None:
    """Who polls each repeater this observer claims, with the policy alongside.

    None on any error, so the caller polls everything itself (fails open).
    """
    params = {"observer": observer_id}
    if plugin_version:
        params["plugin_version"] = plugin_version
    status, body = get(base_url, "/api/guest-polling/owners", params)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("nodes"), dict):
        return None
    return body


def policy(base_url: str, observer_id: str, plugin_version: str) -> tuple[int, JsonDict | None]:
    params = {"plugin_version": plugin_version}
    if observer_id:
        params["observer"] = observer_id
    status, body = get(base_url, "/api/guest-polling/policy", params)
    if status == 200 and not isinstance(body, dict):
        return 0, None
    return status, body


def monitors(base_url: str, node_id: str, plugin_version: str) -> tuple[int, JsonDict | None]:
    """Every observer that claims the repeater."""
    status, body = get(
        base_url, f"/api/nodes/{node_id}/monitors", {"plugin_version": plugin_version}
    )
    if status == 200 and not isinstance(body, dict):
        return 0, None
    return status, body


def battery_check(base_url: str, node_id: str) -> tuple[bool, BatteryCheck]:
    """The freshness check: (reachable, newest reading of any source).

    Tells "no reading yet" (reachable, no latest_ts) from "CoreScope is down".
    """
    status, body = get(base_url, f"/api/nodes/{node_id}/battery", {"days": 1})
    if status == 404:
        return True, {"latest_ts": None, "by": ""}  # no such node: nothing fresh
    if status != 200 or not isinstance(body, dict):
        return False, {}
    # latest_ts is the newest of the source CoreScope shows (the best heard),
    # not the newest overall: that is the newest of all the sources.
    sel: JsonDict = body["selected"] if isinstance(body.get("selected"), dict) else {}
    newest = (body.get("latest_ts"), sel.get("observer_name"))
    for src in body.get("sources") or []:
        ts = src.get("latest_ts") if isinstance(src, dict) else None
        if isinstance(ts, str) and ts and (not isinstance(newest[0], str) or ts > newest[0]):
            newest = (ts, src.get("observer_name"))
    ts, by = newest
    return True, {"latest_ts": ts if isinstance(ts, str) and ts else None, "by": str(by or "")}


def battery_snapshot(base_url: str, node_id: str) -> JsonDict | None:
    """CoreScope's current battery view of a node, or None on any error.

    The only "did it land?" signal for a guest-key poll, whose reading the
    plugin never publishes.
    """
    if not base_url:
        return None
    try:
        body = _json(base_url, f"/api/nodes/{node_id}/battery", {"days": 1}, TIMEOUT)
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    sel: JsonDict = body["selected"] if isinstance(body.get("selected"), dict) else {}
    return {
        "latest_ts": body.get("latest_ts") or None,
        "latest_mv": body.get("latest_mv"),
        "status": body.get("status"),
        "by": sel.get("observer_name") or "",
        "key_mode": sel.get("key_mode") or "",
    }


def responses(base_url: str, since: str, *, limit: int = 2000) -> list[bytes] | None:
    """Every RESPONSE packet any observer heard since ``since``, as raw wire bytes.

    The poller's wider antenna: a reply we missed but another observer heard is
    in here, still encrypted, and is decrypted locally. None on any error.
    """
    if not base_url:
        return None
    try:
        body = _json(base_url, "/api/packets", {"type": 1, "since": since, "limit": limit}, TIMEOUT)
    except (requests.RequestException, ValueError):
        return None
    rows = body.get("packets") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        raw: Any = row.get("raw_hex") if isinstance(row, dict) else None
        try:
            out.append(bytes.fromhex(raw))
        except (TypeError, ValueError):
            continue
    return out
