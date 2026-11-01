"""Readings waiting to be published."""

from __future__ import annotations

import logging
import time

from . import publisher
from .models import JsonDict, Neighbour
from .poll_result import PollResult
from .station import Station
from .timeutil import iso_ts, utc_iso

LOG = logging.getLogger("openhop.corescope_observer_plus")

QUEUE_MAX = 200
QUEUE_MAX_AGE = 2 * 3600  # CoreScope drops readings older than this

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


class Outbox:
    def __init__(self, station: Station):
        self.station = station

    def add(self, readings: list[JsonDict]) -> None:
        for name in self.station.settings.brokers:
            self.station.state.broker(name).setdefault("queue", []).extend(readings)

    def flush(self) -> None:
        station = self.station
        if not station.ohcfg or not station.check_ok("publish"):
            return
        publish = publisher.Publisher(station.ohcfg)
        brokers = {b.name: b for b in station.ohcfg.brokers}
        now = time.time()
        for name in station.settings.brokers:
            bs = station.state.broker(name)
            kept = [
                r
                for r in bs.get("queue", [])
                if now - (iso_ts(r["sampled_at"]) or 0.0) < QUEUE_MAX_AGE
            ][-QUEUE_MAX:]
            # One reading per node per message: keep the newest.
            bs["queue"] = list({r["node_id"]: r for r in kept}.values())
            if not bs["queue"]:
                continue
            broker = brokers.get(name)
            if broker is None:
                bs["last_error"] = "not in openHop's broker list"
                continue
            try:
                publish.publish(broker, bs["queue"])
                bs["last_ok"] = now
                bs["last_count"] = len(bs["queue"])
                bs["last_error"] = ""
                bs["queue"] = []
                LOG.info("published %d reading(s) to %s", bs["last_count"], name)
            except Exception as exc:  # noqa: BLE001 - kept and retried next tick
                bs["last_error"] = str(exc)
                LOG.warning("publish to %s failed: %s", name, exc)
        station.state.save()
