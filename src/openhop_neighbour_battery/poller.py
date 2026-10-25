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

from .companion import CompanionClient, CompanionError, FloodSent, lpp_temperature, lpp_voltage_mv
from .openhop import Neighbour, Route

OK = "ok"
NO_BATTERY = "no_battery"
NEEDS_PASSWORD = "needs_password"
PASSWORD_REJECTED = "password_rejected"
NO_REPLY = "no_reply"
NOT_DIRECT = "not_direct"
ERROR = "error"

LOGIN_SETTLE_SECONDS = 1.0
# Stock repeaters don't answer a wrong password at all, so a "login failed"
# push is usually a reply misread by openHop. Only this many in a row count.
LOGIN_FAILS_BEFORE_BLOCK = 3
RETRY_GAP_SECONDS = 8.0
# Tries per request (status, telemetry) within one poll, all direct.
REQUEST_ATTEMPTS = 3
# A repeater that ignores status but answers telemetry is asked by telemetry
# first; status is tried again after this long.
STATUS_RECHECK = 24 * 3600
# After this many polls in a row with no answer at all, the last login is
# assumed lost (a rebooted repeater forgets guests): the next poll logs in
# before asking anything.
FORGET_LOGIN_AFTER = 3
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
    method: str = "status"  # what the battery came from: "status" or "telemetry"


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
             retry_gap: float = RETRY_GAP_SECONDS, attempts: int = REQUEST_ATTEMPTS) -> PollResult:
    """Read one repeater's battery with as few transmissions as will do.

    Every request goes direct. A request that gets no answer is followed by
    a login, at most one per poll: it costs the repeater nothing when this
    node is already in its ACL, puts it back after a reboot or eviction, and
    tells a dead link (no answer either) from a lost reply. A route whose
    login goes unanswered gets no more tries this poll. Each request is
    tried up to `attempts` times, going round the routes when alternative
    routes are on.

    The status reply is about four times the airtime of a guest's telemetry
    reply, so over a marginal link it can be lost every time while
    telemetry gets through. A repeater one hop away is asked by telemetry
    only. Telemetry carries the battery voltage too: when
    status goes unanswered the battery comes from telemetry, that repeater
    is asked by telemetry only, and status is tried again once a day.
    """
    now = now or time.time()
    key = bytes.fromhex(n.node_id)
    ordered = ordered_routes(routes, node_state.get("last_route"))
    if not ordered:
        return PollResult(NOT_DIRECT, detail="no direct or one-hop route heard")
    attempts = max(1, attempts)

    ctx = {"role": node_state.get("role", ""), "pinned": None, "pinned_any": False, "login_sent": False,
           "refused": "", "dead": set(), "tries": 0, "blocked": None}

    def pin(route: Route) -> bool:
        if ctx["pinned"] == route.key:
            return True
        if not client.pin_route(key, n.name, route.path, route.hash_size):
            ctx["pinned"] = None
            return False
        ctx["pinned"], ctx["pinned_any"] = route.key, True
        return True

    def login(route: Route, probe: bool = True) -> str:
        """"ok", "silent" or "refused"; at most once per poll.

        Only a probe's silence marks the route dead: the reply to a first
        login is flooded by the repeater and can arrive late or not at all
        while the route itself works.
        """
        ctx["login_sent"] = True
        node_state["login_attempt_at"] = now
        result = client.login(key, password)
        if result.ok:
            node_state["logged_in_at"] = now
            node_state["login_fails"] = 0
            ctx["role"] = result.role
            sleep(LOGIN_SETTLE_SECONDS)
            return "ok"
        if result.timed_out:
            if probe:
                ctx["dead"].add(route.key)
            return "silent"
        fails = int(node_state.get("login_fails") or 0) + 1
        node_state["login_fails"] = fails
        if fails >= LOGIN_FAILS_BEFORE_BLOCK:
            ctx["blocked"] = PollResult(PASSWORD_REJECTED if password else NEEDS_PASSWORD, role=ctx["role"], route=route)
        ctx["refused"] = f"login refused ({fails} of {LOGIN_FAILS_BEFORE_BLOCK})"
        return "refused"

    def next_route(i: int) -> Route | None:
        """The route for try i: the best live one, or round the live ones."""
        live = [r for r in ordered if r.key not in ctx["dead"]]
        if not live:
            return None
        return live[i % len(live)] if alternative_routes else live[0]

    def ask(request: Callable[[], object]):
        """Up to `attempts` sends; the answer and the route it came by, or (None, None)."""
        sends = 0
        while sends < attempts and not ctx["blocked"]:
            route = next_route(sends)
            if route is None:
                break
            if sends:
                sleep(retry_gap)
            if not pin(route):
                ctx["dead"].add(route.key)
                continue
            ctx["tries"] = max(ctx["tries"], sends)
            sends += 1
            answer = request()
            if answer is not None:
                return answer, route
            if not ctx["login_sent"] and login(route) == "ok":
                answer = request()
                if answer is not None:
                    return answer, route
        return None, None

    # Through a relay every byte costs airtime twice, so a repeater one hop
    # away is asked by telemetry only (battery and temperature, no stats).
    one_hop = all(r.hops > 0 for r in ordered)
    status_ignored = bool(node_state.get("status_ignored")) and \
        now - float(node_state.get("status_checked_at") or 0) < STATUS_RECHECK
    telemetry_only = one_hop or status_ignored
    stats = tel = None
    used = None
    try:
        # A repeater ignores requests from a client it doesn't know, so log in
        # first until one login has worked.
        if not node_state.get("logged_in_at") and pin(ordered[0]):
            login(ordered[0], probe=False)
        if not telemetry_only:
            stats, used = ask(lambda: client.status(key))
            node_state["status_checked_at"] = now
        want_temp = read_temperature and temperature_due(node_state, now)
        if stats is None or want_temp:
            tel, tel_route = ask(lambda: client.telemetry(key))
            if stats is None:
                used = tel_route
    except FloodSent as exc:
        return PollResult(NOT_DIRECT, detail=str(exc))
    except (CompanionError, OSError) as exc:
        return PollResult(ERROR, detail=str(exc))

    if ctx["blocked"]:
        return ctx["blocked"]
    if stats is not None or tel is not None:
        # Any answer proves the repeater knows this node.
        node_state.setdefault("logged_in_at", now)
    if not ctx["pinned_any"]:
        return PollResult(NOT_DIRECT, detail="the companion would not keep the route")
    role = ctx["role"]
    retried = ctx["tries"] > 0 or ctx["login_sent"] and used is not None
    temp = lpp_temperature(tel) if tel is not None else None
    temp_code = ("ok" if temp is not None else "none") if tel is not None else \
        ("no_reply" if want_temp else "")

    if stats is not None:
        node_state["status_ignored"] = False
        mv = int(stats.get("battery_mv") or 0)
        return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, role=role, stats=stats, route=used,
                          retried=retried, temperature_c=temp, temperature_code=temp_code)
    mv = lpp_voltage_mv(tel) if tel is not None else None
    if mv is None:
        if tel is not None:
            detail = "telemetry answered without a voltage"
        elif ctx["dead"] and len(ctx["dead"]) >= len(ordered):
            detail = "no answer to requests or login: the link looks down"
        else:
            detail = ctx["refused"] or "no answer to status or telemetry"
        return PollResult(NO_REPLY, role=role, retried=True, detail=detail,
                          temperature_c=temp, temperature_code=temp_code)
    if not telemetry_only:
        # Status went unanswered while telemetry came back.
        node_state["status_ignored"] = True
    return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, role=role, route=used, retried=retried,
                      temperature_c=temp, temperature_code=temp_code, method="telemetry")


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
        node_state["method"] = result.method
        if result.route is not None:
            node_state["last_route"] = result.route.key
            node_state["last_route_label"] = result.route.label
        if "uptime_secs" in result.stats:
            node_state["uptime_secs"] = result.stats["uptime_secs"]
    elif result.code == NO_REPLY:
        node_state["no_reply_count"] = int(node_state.get("no_reply_count") or 0) + 1
        if node_state["no_reply_count"] >= FORGET_LOGIN_AFTER:
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
        "method": result.method,
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
