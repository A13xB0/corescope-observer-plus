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

from . import shared_key as sk
from .companion import (CompanionClient, CompanionError, FloodSent, parse_stats, path_len_byte,
                        lpp_temperature, lpp_voltage_mv)
from .openhop import Neighbour, Route

OK = "ok"
NO_BATTERY = "no_battery"
NEEDS_PASSWORD = "needs_password"
PASSWORD_REJECTED = "password_rejected"
NO_REPLY = "no_reply"
NOT_DIRECT = "not_direct"
ERROR = "error"
# The shared key was answered as admin; it is disabled on that repeater and the
# removal drill has run. Never retried under the shared key automatically.
ADMIN_STOP = "admin_stop"

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
# A repeater that has answered nothing for this long is checked only this often
# until it is heard again — no point polling a long-dead one every interval.
UNHEARD_BACKOFF_AFTER = 24 * 3600
UNHEARD_BACKOFF_INTERVAL = 24 * 3600
# When we don't hear a shared-key reply ourselves, wait this long for another
# observer's overhear to reach CoreScope before checking whether it landed.
COVERED_SETTLE_SECONDS = 3.0
TEMP_MISSES_BEFORE_DAILY = 3
TEMP_DAILY = 24 * 3600

# Shared-key poll: how long to listen off the air for a reply after each raw
# send, and how many data requests to try within one poll.
SHARED_REPLY_WINDOW = 20.0
SHARED_ATTEMPTS = 2
# The firmware keeps the shared key in its (RAM, 32-entry) guest ACL after a
# login, so most polls can just send the request and save the login's airtime.
# A login reply is the only thing that reveals a key quietly made admin, so one
# is still forced this often to re-check, even while the ACL entry holds.
SHARED_ADMIN_RECHECK_SECONDS = 6 * 3600
# Admin removal drill: after an admin answer, how long to let the setperm take
# before the verifying login, and how many verify logins to try.
ADMIN_REMOVE_SETTLE = 10.0
ADMIN_REMOVE_TRIES = 3


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
    key_mode: str = "own"  # "own" (this observer's key) or "shared" (the guest key)
    admin_seen: bool = False  # the shared key was answered as admin
    admin_removed: bool | None = None  # removal drill result: True ok, False failed, None not run
    covered: bool = False  # we didn't hear it, but CoreScope did (another observer overheard)


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
             retry_gap: float = RETRY_GAP_SECONDS, attempts: int = REQUEST_ATTEMPTS,
             on_step: Callable[[str], None] | None = None) -> PollResult:
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
    step = on_step or (lambda *_a: None)
    key = bytes.fromhex(n.node_id)
    ordered = ordered_routes(routes, node_state.get("last_route"))
    if not ordered:
        step("No route heard")
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
        step(f"Logging in {route.label}" + (" (verifying)" if not probe else ""))
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
            step("Requesting status")
            stats, used = ask(lambda: client.status(key))
            node_state["status_checked_at"] = now
        want_temp = read_temperature and temperature_due(node_state, now)
        if stats is None or want_temp:
            step("Requesting telemetry")
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


def poll_one_shared(client: CompanionClient, n: Neighbour, shared: sk.SharedKey, node_state: dict, *,
                    routes: list[Route], read_temperature: bool = False, now: float | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    reply_window: float = SHARED_REPLY_WINDOW, attempts: int = SHARED_ATTEMPTS,
                    on_step: Callable[[str], None] | None = None,
                    covered: Callable[[], bool] | None = None) -> PollResult:
    """Poll one repeater under the shared guest key, as raw packets.

    Sends a blank login then a status request (direct) or telemetry request
    (one hop), both built and encrypted with the shared key, and reads the
    replies straight off the air. The route is carried in the packet, so
    nothing is pinned in the companion's contacts and nothing is ever flooded.

    Safety is enforced here and in the packet builder together: the login is
    always blank (``build_login`` refuses anything else), and if the repeater
    answers the login as admin the poll stops, runs the removal drill
    (``_admin_removal_drill``) and returns ADMIN_STOP so the caller disables the
    shared key on that repeater and alerts. The battery is never read in that
    case; a repeater that hands the public key admin is not one to keep polling
    under it.
    """
    now = now or time.time()
    step = on_step or (lambda *_a: None)
    reply = None
    admin = False
    try:
        key = bytes.fromhex(n.node_id)
        live = ordered_routes([r for r in routes], node_state.get("last_route"))
        if not live:
            step("No route heard")
            return PollResult(NOT_DIRECT, key_mode="shared", detail="no direct or one-hop route heard")
        route = live[0]
        out_path = route.path
        plen = path_len_byte(out_path, route.hash_size)
        one_hop = route.hops > 0

        def collect(data_tag: int) -> tuple[sk.Reply | None, sk.LoginResult | None]:
            """One window: the data reply (matched by our tag) and the login
            answer (told by shape — its own tag is the repeater's clock, not
            ours). Stops early on an admin answer or once both are in hand."""
            data = login = None
            for frame in client.collect_rx(reply_window):
                r = sk.decode_rx(frame, shared, [key])
                if r is None:
                    continue
                if r.tag == data_tag and data is None:
                    data = r
                elif login is None:
                    lr = sk.login_result(r)
                    if lr is not None:
                        login = lr
                        if lr.is_admin:
                            break
                if data is not None and login is not None:
                    break
            return data, login

        # Status direct; telemetry only through a relay (same airtime reasoning
        # as the own-key poll).
        kind = "telemetry" if one_hop else "status"
        teach = not node_state.get("logged_in_at")
        tried = 0

        def send_request() -> tuple[sk.Reply | None, sk.LoginResult | None]:
            step(f"Requesting {kind}, waiting for a reply")
            body, data_tag = (sk.build_status_request if kind == "status"
                              else sk.build_telemetry_request)(shared, key, out_path, plen)
            client.send_raw_packet(body)
            return collect(data_tag)

        def do_login(pw: str) -> None:
            nonlocal teach
            step(f"Shared-key login ({'blank' if pw == '' else repr(pw)}) {route.label}")
            client.send_raw_packet(sk.build_login(shared, key, out_path, plen, pw)[0])
            if teach:
                # teach the return path once so replies come direct, not flooded
                # (firmware ClientACL.putClient leaves a new key pathless).
                if one_hop:
                    client.send_raw_packet(sk.build_route_update(
                        shared, key, out_path, plen, return_path=out_path, return_path_len=plen))
                else:
                    client.send_raw_packet(sk.build_route_update(shared, key, out_path, plen))
                teach = False
            node_state["login_checked_at"] = now
            sleep(LOGIN_SETTLE_SECONDS)

        def check_covered() -> PollResult | None:
            # We didn't hear it ourselves — give an observer's overhear a moment
            # to reach CoreScope, then take that reading instead of retrying.
            if covered is None:
                return None
            sleep(COVERED_SETTLE_SECONDS)
            if covered():
                step("Heard via another observer — CoreScope already has the reply")
                return PollResult(OK, key_mode="shared", route=route, covered=True,
                                  detail="reading obtained via another observer")
            return None

        # The firmware keeps us in its guest ACL after a login, so poll without
        # logging in while we believe we are in it — saving the login's airtime.
        # Force a login only on first contact, when the ACL entry looks gone, or
        # for the periodic admin re-check (a login reply is the only thing that
        # reveals a key made admin).
        recheck_due = now - float(node_state.get("login_checked_at") or 0) >= SHARED_ADMIN_RECHECK_SECONDS
        if node_state.get("logged_in_at") and not teach and not recheck_due:
            step(f"Requesting {kind} (existing guest ACL entry, no login)")
            data, login = send_request()
            tried += 1
            if login is not None and login.is_admin:
                admin = True
            elif data is not None:
                reply = data
            else:
                node_state.pop("logged_in_at", None)  # ACL entry likely gone
                c = check_covered()
                if c is not None:
                    return c  # another observer got it; no need to log in and retry

        # Once we know which password a repeater takes, send only that — a
        # known-blank repeater is never sent "hello", and a "hello" one isn't
        # slowed by a blank try. Only a first, unknown contact tries both (blank
        # first); sustained silence later forgets it (record) so it re-learns.
        known = node_state.get("shared_password")
        passwords = [known] if known in sk.SHARED_PASSWORDS else list(sk.SHARED_PASSWORDS)
        if reply is None and not admin:
            for attempt, pw in enumerate(passwords):
                if attempt:
                    c = check_covered()
                    if c is not None:
                        return c
                    sleep(RETRY_GAP_SECONDS)
                do_login(pw)
                data, login = send_request()
                tried += 1
                if login is not None:
                    node_state["logged_in_at"] = now
                    if login.is_admin:
                        admin = True
                        break
                if data is not None:
                    node_state["shared_password"] = pw  # remember what worked
                    reply = data
                    break
                # No answer at all: teach the route again next attempt (a reboot
                # drops the key and its path).
                teach = True
                node_state.pop("logged_in_at", None)
        if admin:
            removed = _admin_removal_drill(client, shared, key, out_path, plen, sleep)
            return PollResult(ADMIN_STOP, role="admin", key_mode="shared", route=route,
                              admin_seen=True, admin_removed=removed,
                              detail="the repeater answered the shared key as admin")
    except sk.UnsafeSharedPassword as exc:
        return PollResult(ERROR, key_mode="shared", detail=str(exc))
    except (CompanionError, OSError, sk.SharedKeyError, ValueError) as exc:
        # ValueError guards a bad route/path so one repeater can't abort the
        # whole poll cycle (and the own-key polls in it).
        return PollResult(ERROR, key_mode="shared", detail=str(exc))

    if reply is None:
        c = check_covered()  # last chance: did an observer's overhear reach CoreScope?
        if c is not None:
            return c
        return PollResult(NO_REPLY, key_mode="shared", route=route, retried=tried > 1,
                          detail="no answer to the shared-key request")
    node_state.setdefault("logged_in_at", now)
    if kind == "status":
        try:
            stats = parse_stats(reply.data)
        except CompanionError:
            return PollResult(NO_REPLY, key_mode="shared", route=route, detail="unreadable status reply")
        mv = int(stats.get("battery_mv") or 0)
        return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, stats=stats, route=route,
                          key_mode="shared", method="status")
    temp = lpp_temperature(reply.data) if read_temperature else None
    temp_code = ("ok" if temp is not None else "none") if read_temperature else ""
    mv = lpp_voltage_mv(reply.data)
    if mv is None:
        return PollResult(NO_REPLY, key_mode="shared", route=route,
                          detail="telemetry answered without a voltage",
                          temperature_c=temp, temperature_code=temp_code)
    return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, route=route, key_mode="shared",
                      method="telemetry", temperature_c=temp, temperature_code=temp_code)


def _admin_removal_drill(client: CompanionClient, shared: sk.SharedKey, key: bytes,
                         out_path: bytes, plen: int, sleep: Callable[[float], None]) -> bool:
    """Undo an admin grant on the shared key, then confirm it is gone.

    Sends ``setperm <shared key> 0`` — which deletes the key's ACL entry and is
    admin-only, so it works precisely because the key still holds the admin
    rights it is removing — waits for the repeater to save it, then logs in
    again (blank) and checks the answer is no longer admin. Returns True once a
    verifying login comes back non-admin, False if none does within the tries.
    """
    try:
        client.send_raw_packet(sk.build_admin_removal(shared, key, out_path, plen))
    except (CompanionError, OSError, sk.SharedKeyError):
        return False
    sleep(ADMIN_REMOVE_SETTLE)
    # A one-hop repeater's return path is back through the relay (non-empty
    # out_path); a direct neighbour's is empty.
    rp, rlen = (out_path, plen) if out_path else (b"", None)
    for attempt in range(ADMIN_REMOVE_TRIES):
        if attempt:
            sleep(RETRY_GAP_SECONDS)
        try:
            client.send_raw_packet(sk.build_login(shared, key, out_path, plen, "")[0])
            # setperm 0 deleted the ACL entry and its return path, so teach the
            # route again or the verifying reply floods and we miss it.
            client.send_raw_packet(sk.build_route_update(shared, key, out_path, plen,
                                                         return_path=rp, return_path_len=rlen))
        except (CompanionError, OSError, sk.SharedKeyError, sk.UnsafeSharedPassword):
            continue
        for frame in client.collect_rx(SHARED_REPLY_WINDOW):
            reply = sk.decode_rx(frame, shared, [key])
            if reply is None:
                continue
            # The login answer is told by shape, not tag (it carries the
            # repeater's clock); a non-admin answer confirms the key is demoted.
            result = sk.login_result(reply)
            if result is not None and not result.is_admin:
                return True
    return False


def record(node_state: dict, result: PollResult, password_set: bool, now: float) -> None:
    node_state["last_attempt"] = now
    if result.covered:
        # We didn't hear it, but another observer did and CoreScope has the
        # reading: count it as heard for scheduling (no retry, no backoff), and
        # keep the last known battery value — we never decoded this one.
        node_state["code"] = OK
        node_state["detail"] = result.detail
        node_state["method"] = "shared-observer"
        node_state["last_success"] = now
        node_state.pop("unheard_since", None)
        node_state["no_reply_count"] = 0
        if result.route is not None:
            node_state["last_route"] = result.route.key
            node_state["last_route_label"] = result.route.label
        return
    node_state["code"] = result.code
    node_state["detail"] = result.detail
    node_state["retried"] = result.retried
    if result.role:
        node_state["role"] = result.role
    if result.code in (OK, NO_BATTERY):
        node_state["last_success"] = now
        node_state.pop("unheard_since", None)  # heard again: back to the normal interval
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
        node_state.setdefault("unheard_since", now)  # start the clock for the daily backoff
        if node_state["no_reply_count"] >= FORGET_LOGIN_AFTER:
            node_state.pop("logged_in_at", None)
            node_state.pop("shared_password", None)  # re-learn blank/hello next time
    if result.code in (NEEDS_PASSWORD, PASSWORD_REJECTED):
        node_state.pop("logged_in_at", None)
        node_state["login_fails"] = 0
        # Don't retry the same credential until the operator changes it.
        node_state["blocked_password_set"] = password_set
    if result.code == ADMIN_STOP:
        # The shared key was answered as admin: remember it so the key is never
        # used here again automatically, and record the drill's outcome for the
        # alert the owner sees.
        node_state["shared_admin_seen"] = True
        node_state["shared_admin_removed"] = result.admin_removed
        node_state["shared_admin_at"] = now
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
    # Unheard for a day or more: drop to one check per day until it answers.
    unheard = node_state.get("unheard_since")
    if unheard is not None and now - float(unheard) >= UNHEARD_BACKOFF_AFTER:
        return now - last >= UNHEARD_BACKOFF_INTERVAL
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
    if result.key_mode == "shared":
        # Marks a reading taken under the shared guest key, so CoreScope can
        # credit it and (for an admin answer) raise the owner alert.
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
    # Every status reply carries these, so they always go out with the battery.
    stats = {k: result.stats[k] for k in PUBLISHED_STATS if k in result.stats}
    if stats:
        out["stats"] = stats
    return out
