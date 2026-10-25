"""One poll of one repeater, and the rules for when to poll it next.

Every request and every retry is sent direct: with an empty path to a
repeater heard at zero hops, or along a one-relay path through a repeater
heard direct. If the path can't be pinned, nothing is sent. Nothing is ever
flooded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .companion import CompanionClient, CompanionError, FloodSent, lpp_temperature
from .openhop import Neighbour, Route

OK = "ok"
NO_BATTERY = "no_battery"
NEEDS_PASSWORD = "needs_password"
PASSWORD_REJECTED = "password_rejected"
NO_REPLY = "no_reply"
NOT_DIRECT = "not_direct"
ERROR = "error"

# A logged-in client stays in the repeater's ACL, so status is asked first and
# a login is sent only when status gets no answer, at most this often.
LOGIN_MIN_GAP = 3600
LOGIN_SETTLE_SECONDS = 1.0
# Stock repeaters don't answer a wrong password at all, so a "login failed"
# push is usually a reply misread by openHop. Only this many in a row count.
LOGIN_FAILS_BEFORE_BLOCK = 3
RETRY_GAP_SECONDS = 8.0
NO_BATTERY_RECHECK = 24 * 3600
# A repeater that didn't answer is tried again sooner: every 20 minutes (3 an
# hour) for its first misses in a row, then at the normal interval. It is
# never given up on.
NO_REPLY_RETRY = 20 * 60
NO_REPLY_FAST_TRIES = 9
TEMP_MISSES_BEFORE_DAILY = 3
TEMP_DAILY = 24 * 3600


@dataclass
class PollResult:
    code: str
    battery_mv: int | None = None
    role: str = ""
    detail: str = ""
    stats: dict = field(default_factory=dict)
    route: Route | None = None
    retried: bool = False
    temperature_c: float | None = None
    temperature_code: str = ""  # "" not asked, "ok", "none" (answered without one), "no_reply"


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ordered_routes(routes: list[Route], last_key: str | None) -> list[Route]:
    """Best first, with the route that last answered moved to the top."""
    out = list(routes)
    for i, r in enumerate(out):
        if r.key == last_key:
            out.insert(0, out.pop(i))
            break
    return out


def temperature_due(node_state: dict, now: float) -> bool:
    if int(node_state.get("temp_misses") or 0) < TEMP_MISSES_BEFORE_DAILY:
        return True
    return now - float(node_state.get("temp_attempt") or 0) >= TEMP_DAILY


def poll_one(client: CompanionClient, n: Neighbour, password: str, node_state: dict, *,
             routes: list[Route], alternative_routes: bool = False, read_temperature: bool = False,
             now: float | None = None, sleep: Callable[[float], None] = time.sleep,
             retry_gap: float = RETRY_GAP_SECONDS) -> PollResult:
    """Pin a route, ask for status (logging in only if needed), then temperature."""
    now = now or time.time()
    key = bytes.fromhex(n.node_id)
    ordered = ordered_routes(routes, node_state.get("last_route"))
    if not ordered:
        return PollResult(NOT_DIRECT, detail="no direct or one-hop route heard")
    tries = [ordered[0], ordered[1] if alternative_routes and len(ordered) > 1 else ordered[0]]

    role = node_state.get("role", "")
    login_sent = False
    pinned_any = False
    refused = ""
    stats = None
    used = None
    try:
        for i, route in enumerate(tries):
            if i:
                sleep(retry_gap)
            if not client.pin_route(key, n.name, route.path, route.hash_size):
                continue
            pinned_any = True
            stats = client.status(key)
            if stats is None and not login_sent and \
                    now - float(node_state.get("login_attempt_at") or 0) >= LOGIN_MIN_GAP:
                login_sent = True
                node_state["login_attempt_at"] = now
                login = client.login(key, password)
                if login.ok:
                    node_state["logged_in_at"] = now
                    node_state["login_fails"] = 0
                    role = login.role
                    sleep(LOGIN_SETTLE_SECONDS)
                    stats = client.status(key)
                elif not login.timed_out:
                    fails = int(node_state.get("login_fails") or 0) + 1
                    node_state["login_fails"] = fails
                    if fails >= LOGIN_FAILS_BEFORE_BLOCK:
                        return PollResult(PASSWORD_REJECTED if password else NEEDS_PASSWORD, role=role,
                                          route=route, retried=i > 0)
                    refused = f"login refused ({fails} of {LOGIN_FAILS_BEFORE_BLOCK})"
            if stats is not None:
                used = route
                break
    except FloodSent as exc:
        return PollResult(NOT_DIRECT, detail=str(exc))
    except (CompanionError, OSError) as exc:
        return PollResult(ERROR, detail=str(exc))

    if not pinned_any:
        return PollResult(NOT_DIRECT, detail="the companion would not keep the route")
    if stats is None:
        return PollResult(NO_REPLY, role=role, retried=True,
                          detail=refused or "no answer to the status request")

    mv = int(stats.get("battery_mv") or 0)
    result = PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, role=role, stats=stats,
                        route=used, retried=i > 0)
    if read_temperature and temperature_due(node_state, now):
        result.temperature_c, result.temperature_code = _read_temperature(client, key, sleep, retry_gap)
    return result


def _read_temperature(client: CompanionClient, key: bytes, sleep, retry_gap) -> tuple[float | None, str]:
    """Telemetry on the route that just answered, with one direct retry."""
    try:
        for attempt in range(2):
            if attempt:
                sleep(retry_gap)
            data = client.telemetry(key)
            if data is not None:
                temp = lpp_temperature(data)
                return temp, "ok" if temp is not None else "none"
    except (CompanionError, OSError):
        pass
    return None, "no_reply"


def record(node_state: dict, result: PollResult, password_set: bool, now: float) -> None:
    node_state["last_attempt"] = now
    node_state["code"] = result.code
    node_state["detail"] = result.detail
    node_state["retried"] = result.retried
    if result.role:
        node_state["role"] = result.role
    if result.code in (OK, NO_BATTERY):
        node_state["last_success"] = now
        node_state["battery_mv"] = result.battery_mv
        node_state["no_reply_count"] = 0
        if result.route is not None:
            node_state["last_route"] = result.route.key
            node_state["last_route_label"] = result.route.label
        if "uptime_secs" in result.stats:
            node_state["uptime_secs"] = result.stats["uptime_secs"]
    elif result.code == NO_REPLY:
        node_state["no_reply_count"] = int(node_state.get("no_reply_count") or 0) + 1
        node_state.pop("logged_in_at", None)
    if result.code in (NEEDS_PASSWORD, PASSWORD_REJECTED):
        node_state.pop("logged_in_at", None)
        node_state["login_fails"] = 0
        # Don't retry the same credential until the operator changes it.
        node_state["blocked_password_set"] = password_set
    if result.temperature_code:
        node_state["temp_attempt"] = now
        node_state["temp_code"] = result.temperature_code
        if result.temperature_code == "ok":
            node_state["temperature_c"] = result.temperature_c
            node_state["temp_at"] = now
            node_state["temp_misses"] = 0
        else:
            node_state["temp_misses"] = int(node_state.get("temp_misses") or 0) + 1


def is_due(node_state: dict, interval_s: float, password_set: bool, now: float) -> bool:
    code = node_state.get("code")
    last = float(node_state.get("last_attempt") or 0)
    if code in (NEEDS_PASSWORD, PASSWORD_REJECTED):
        # Retry only once the credential changed (set, replaced or removed).
        if node_state.get("blocked_password_set") == password_set and not node_state.get("password_changed"):
            return False
    if code == NO_BATTERY:
        return now - last >= max(interval_s, NO_BATTERY_RECHECK)
    if code == NO_REPLY:
        fast = int(node_state.get("no_reply_count") or 0) < NO_REPLY_FAST_TRIES
        return now - last >= (min(interval_s, NO_REPLY_RETRY) if fast else interval_s)
    return now - last >= interval_s


# Stats published with a reading: everything in the status reply but the
# battery, which has its own field.
PUBLISHED_STATS = (
    "uptime_secs", "noise_floor", "last_rssi", "last_snr", "tx_queue", "packets_recv",
    "packets_sent", "recv_flood", "recv_direct", "sent_flood", "sent_direct", "tx_air_secs",
    "rx_air_secs", "flood_dups", "direct_dups", "recv_errors", "err_events",
)


def reading(n: Neighbour, result: PollResult, sampled_at: float) -> dict:
    """One payload reading (the CoreScope /battery contract, v1)."""
    route = result.route
    out = {
        "node_id": n.node_id,
        "battery_mv": int(result.battery_mv or 0),
        "source": "poll",
        "reply_hops": route.hops if route else 0,
        "method": "status",
        "sampled_at": utc_iso(sampled_at),
    }
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
    # Every status reply carries these, so they always go out with the battery.
    stats = {k: result.stats[k] for k in PUBLISHED_STATS if k in result.stats}
    if stats:
        out["stats"] = stats
    return out
