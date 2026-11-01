"""Readings waiting to be published."""

from __future__ import annotations

from .models import JsonDict, Neighbour
from .poll_result import PollResult
from .timeutil import utc_iso

# Everything in the status reply but the battery, which has its own field.
PUBLISHED_STATS = (
    "uptime_secs",
    "noise_floor",
    "last_rssi",
    "last_snr",
    "tx_queue",
    "packets_recv",
    "packets_sent",
    "recv_flood",
    "recv_direct",
    "sent_flood",
    "sent_direct",
    "tx_air_secs",
    "rx_air_secs",
    "flood_dups",
    "direct_dups",
    "recv_errors",
    "err_events",
)


def reading(n: Neighbour, result: PollResult, sampled_at: float) -> JsonDict:
    """One payload reading (the CoreScope /battery contract, v1)."""
    route = result.route
    out: JsonDict = {
        "node_id": n.node_id,
        "battery_mv": int(result.battery_mv or 0),
        "source": "poll",
        "reply_hops": route.hops if route else 0,
        "method": result.method,
        "sampled_at": utc_iso(sampled_at),
    }
    if result.key_mode == "shared":
        out["key"] = "shared"
    if route is not None and route.relay_id:
        out["via"] = route.relay_id
        snr = route.snr
    else:
        snr = n.snr
    if snr is not None:
        out["snr"] = round(snr, 2)
    if n.rssi is not None and not (route and route.relay_id):
        out["rssi"] = n.rssi
    if result.temperature_c is not None:
        out["temperature_c"] = round(result.temperature_c, 1)
    stats = {k: result.stats[k] for k in PUBLISHED_STATS if k in result.stats}
    if stats:
        out["stats"] = stats
    return out
