"""One poll of one repeater, and the rules for when to poll it next.

Every request and every retry is sent direct: with an empty path to a
repeater heard at zero hops, or along a one-relay path through a repeater
heard direct. If the path can't be pinned, nothing is sent. Nothing we send is
ever flooded; a repeater's reply is flooded when it has no route back to us,
which the guest-key flow works to avoid (see poll_one_shared).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import packet_log
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
# Battery and chip temperature come from telemetry, whose guest reply is about
# 12 bytes; a status reply is 60 (RepeaterStats). So every poll asks for
# telemetry, and the full status (uptime, noise floor, counters for the
# Repeater Status charts) is asked for only when the last one is this old
# (the default for the status_every_hours setting).
STATUS_EVERY_SECONDS = 6 * 3600
# After this many polls in a row with no answer at all, the last login is
# assumed lost (a rebooted repeater forgets guests): the next poll logs in
# before asking anything.
FORGET_LOGIN_AFTER = 3
FORGET_PASSWORD_AFTER = 7 * 24 * 3600
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
# When we don't hear an answer ourselves, give other observers' copies this
# long to reach CoreScope (observer -> MQTT -> ingest) before asking it, and
# ask for packets from a little before we sent (clocks differ).
ANTENNA_SETTLE_SECONDS = 5.0
ANTENNA_CLOCK_SLACK = 30.0
# If CoreScope has nothing yet, look again after these gaps (seconds) before
# anything else goes on air. Ingest usually takes under 10 s, but right after a
# flood (30+ observers uploading at once) it took over 20 s (Redstone, 26 Oct:
# three answered logins missed). An HTTP request costs no airtime.
ANTENNA_RECHECKS = (10.0, 15.0)
# The route is re-sent before a no-login request only when it may be gone:
# after a flooded reply, after a failed poll, or once this long since the last.
ROUTE_REFRESH_SECONDS = 24 * 3600
# Request tags remembered per repeater, so a late reply to an earlier poll's
# request is never read as a login answer.
RECENT_TAGS = 16
# A repeater whose replies keep coming back flooded (our route isn't taking)
# is polled only daily, so it can't flood the mesh every interval.
FLOOD_REPLIES_BEFORE_DAILY = 2
# A flooded login answer is re-broadcast by every repeater around for several
# seconds; a route packet sent into that is lost (26 Oct: sent 1-4 s after a
# flooded answer it never took, 5 of 5; sent in a quiet moment it always did).
# So after a flooded answer, wait this long before sending the route.
FLOOD_CLEAR_SECONDS = 15.0
FLOOD_DAILY = 24 * 3600
TEMP_MISSES_BEFORE_DAILY = 3
TEMP_DAILY = 24 * 3600

# Guest-key poll: how long to listen off the air for a reply after each raw send.
SHARED_REPLY_WINDOW = 20.0
# After a login, listen this long for its answer before transmitting again.
# The radio is half duplex and the repeater answers 300 ms after it hears the
# login (SERVER_RESPONSE_DELAY), so sending the route update straight away
# talked over the answer. A new key's answer is flooded, so allow for relays.
# Generous because listening costs no airtime (it stops as soon as the answer
# arrives): a direct status reply was measured at 6.3 s on 26 Oct, and a new
# key's login answer is flooded back through relays, each adding a random delay.
LOGIN_ANSWER_WAIT = 10.0
LOGIN_ANSWER_WAIT_PER_HOP = 10.0
# The firmware keeps the shared key in its (RAM, 32-entry) guest ACL after a
# login, so most polls can just send the request and save the login's airtime.
# A login reply is the only thing that reveals a key quietly made admin, so one
# is still forced this often to re-check, even while the ACL entry holds.
SHARED_ADMIN_RECHECK_SECONDS = 24 * 3600
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
    via_corescope: bool = False  # our reply, decrypted from another observer's copy via CoreScope
    reply_s: float | None = None  # seconds from the request going out to its reply (for tuning windows)


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


def status_is_due(node_state: dict, now: float, every: float = STATUS_EVERY_SECONDS) -> bool:
    """Whether to ask for the full status this poll (never read yet, or the
    last ask was ``every`` seconds ago; the status_every_hours setting)."""
    last = node_state.get("status_checked_at")
    return last is None or now - float(last) >= every


def temperature_due(node_state: dict, now: float) -> bool:
    if int(node_state.get("temp_misses") or 0) < TEMP_MISSES_BEFORE_DAILY:
        return True
    return now - float(node_state.get("temp_attempt") or 0) >= TEMP_DAILY


def poll_one(client: CompanionClient, n: Neighbour, password: str, node_state: dict, *,
             routes: list[Route], alternative_routes: bool = False, read_temperature: bool = False,
             now: float | None = None, sleep: Callable[[float], None] = time.sleep,
             retry_gap: float = RETRY_GAP_SECONDS, attempts: int = REQUEST_ATTEMPTS,
             on_step: Callable[[str], None] | None = None,
             status_every: float = STATUS_EVERY_SECONDS,
             plog: packet_log.NodeLog | None = None) -> PollResult:
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
    log = plog or packet_log.NULL
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
        entry = log.sent("login", route.label, "saved password" if password else "blank password")
        t0 = time.monotonic()
        result = client.login(key, password)
        if result.ok:
            log.answered(entry, "radio", after_s=time.monotonic() - t0, what=f"logged in as {result.role or 'guest'}")
        elif result.timed_out:
            log.unanswered(entry)
        else:
            log.refused(entry, "login refused")
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

    def logged(request: Callable[[], object], kind: str, route: Route):
        entry = log.sent(f"{kind} request", route.label)
        t0 = time.monotonic()
        answer = request()
        if answer is None:
            log.unanswered(entry)
        elif kind == "status":
            log.answered(entry, "radio", after_s=time.monotonic() - t0,
                         what=f"status, {int(answer.get('battery_mv') or 0)} mV")
        else:
            log.answered(entry, "radio", after_s=time.monotonic() - t0, what=_describe(kind, answer))
        return answer

    def next_route(i: int) -> Route | None:
        """The route for try i: the best live one, or round the live ones."""
        live = [r for r in ordered if r.key not in ctx["dead"]]
        if not live:
            return None
        return live[i % len(live)] if alternative_routes else live[0]

    def ask(request: Callable[[], object], kind: str):
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
            answer = logged(request, kind, route)
            if answer is not None:
                return answer, route
            if not ctx["login_sent"] and login(route) == "ok":
                answer = logged(request, kind, route)
                if answer is not None:
                    return answer, route
        return None, None

    # Through a relay every byte costs airtime twice, so a repeater one hop
    # away is asked by telemetry only (battery and temperature, no stats).
    one_hop = all(r.hops > 0 for r in ordered)
    status_ignored = bool(node_state.get("status_ignored")) and \
        now - float(node_state.get("status_checked_at") or 0) < STATUS_RECHECK
    status_due = status_is_due(node_state, now, status_every)
    telemetry_only = one_hop or status_ignored or not status_due
    stats = tel = None
    used = None
    try:
        # A repeater ignores requests from a client it doesn't know, so log in
        # first until one login has worked.
        if not node_state.get("logged_in_at") and pin(ordered[0]):
            login(ordered[0], probe=False)
        if not telemetry_only:
            step("Requesting status")
            stats, used = ask(lambda: client.status(key), "status")
            node_state["status_checked_at"] = now
        want_temp = read_temperature and temperature_due(node_state, now)
        if stats is None or want_temp:
            step("Requesting telemetry")
            tel, tel_route = ask(lambda: client.telemetry(key), "telemetry")
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


def shared_password_order(node_state: dict, now: float, personal: bool = False,
                          configured: str | None = None) -> list[str]:
    """The passwords to log in with in one poll, one login each, under a guest key.

    A login's answer is flooded whenever the repeater has no route back to the
    key (MeshCore 1.18; older firmware floods every login answer), so logins
    are kept to the fewest that can work: CoreScope's observers catch an answer
    we miss ourselves, which makes a blind retry of the same password pointless.

    A repeater known to take a password is sent only that one. On a first
    contact blank comes first and "hello" only after blank went unanswered, so
    a blank-guest repeater that answers is never sent "hello". Once the key's
    logins are failing (key_login_fails: polls in a row with no answer, seen by
    us or CoreScope) it is one login per poll, blank and "hello" on alternate
    days. With the public shared key only "hello" is ever remembered: a blank
    login can succeed just because another observer put the shared key in the
    ACL (one key, one entry for everyone). With this observer's own key a blank
    success is our own.
    """
    if configured in sk.SHARED_PASSWORDS:
        return [configured]  # set by the operator for this repeater
    known = node_state.get("shared_password")
    if known == "hello" or (personal and known == ""):
        return [known]
    if int(node_state.get("key_login_fails") or 0) >= 2:
        return [sk.SHARED_PASSWORDS[int(now // 86400) % len(sk.SHARED_PASSWORDS)]]
    return list(sk.SHARED_PASSWORDS)


def poll_one_shared(client: CompanionClient, n: Neighbour, shared: sk.SharedKey, node_state: dict, *,
                    routes: list[Route], read_temperature: bool = False, now: float | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    reply_window: float = SHARED_REPLY_WINDOW,
                    on_step: Callable[[str], None] | None = None,
                    antenna: Callable[[float], list[bytes] | None] | None = None,
                    personal: bool = False, status_every: float = STATUS_EVERY_SECONDS,
                    plog: packet_log.NodeLog | None = None,
                    guest_password: str | None = None) -> PollResult:
    """Poll one repeater under a guest key, as raw packets.

    Requests are built and encrypted with the key and replies are read straight
    off the air; the route is carried in the packet, so nothing is pinned in
    the companion and nothing we send is ever flooded. What the repeater sends
    back is another matter: with no stored route back to the key it floods its
    answer across the mesh. So the flow keeps the repeater's route fresh and
    sends as little as possible:

      1. Still in the repeater's guest ACL (logged in before) and the admin
         re-check isn't due → send our return route, then the request. No
         login. The route makes the reply come back direct even if the
         repeater lost it (a reboot), and costs one short direct packet.
      2. Otherwise, or if that went unanswered → log in (see
         shared_password_order), and once the login is answered send the route
         and request again.

    After anything we don't hear answered ourselves, ``antenna(since)`` is
    asked for every reply CoreScope's observers heard since we sent; those are
    decrypted here like our own radio's frames, so a reply someone else heard
    is never re-asked for. Replies that came back flooded are counted
    (flood_replies) so a repeater that keeps flooding is polled only daily.

    Safety: ``build_login`` refuses any password but blank or "hello", and a
    login answered as admin stops the poll, runs the removal drill and returns
    ADMIN_STOP so the caller disables the key there and alerts.
    """
    now = now or time.time()
    step = on_step or (lambda *_a: None)
    log = plog or packet_log.NULL
    # The ACL entry, its route and the password it took all belong to the key:
    # when the key changes (shared → this observer's own), start over.
    if node_state.get("acl_key") != shared.public_key.hex():
        for k in ("logged_in_at", "login_checked_at", "shared_password", "key_login_fails",
                  "flood_replies"):
            node_state.pop(k, None)
        node_state["acl_key"] = shared.public_key.hex()
    try:
        key = bytes.fromhex(n.node_id)
        live = ordered_routes(routes, node_state.get("last_route"))
        if not live:
            step("No route heard")
            return PollResult(NOT_DIRECT, key_mode="shared", detail="no direct or one-hop route heard")
        route = live[0]
        out_path = route.path
        plen = path_len_byte(out_path, route.hash_size)
        one_hop = route.hops > 0
        # Telemetry every poll (battery + chip temperature, small reply);
        # status only every status_every seconds, and never through a relay.
        status_due = status_is_due(node_state, now, status_every)
        kind = "status" if (not one_hop and status_due) else "telemetry"
        client.clear_rx_backlog()  # only frames heard during this poll count
        latency: list[float] = []
        # Our own request/login tags (this poll's and recent polls'): a reply
        # carrying one is never a login answer.
        sent_tags: set[int] = set(int(t) for t in node_state.get("recent_tags") or [])
        flooded: list[bool] = []  # one per reply we got, ours or CoreScope's
        data_flooded: list[bool] = []  # the same, for data replies only (each follows a route)
        wall = time.time

        def send(body: bytes, what: str, kind_: str, detail: str = "") -> dict:
            entry = log.sent(kind_, route.label, detail)
            # openHop confirms a raw send only after transmitting; a long wait
            # means it was busy, not that the packet was refused.
            if not client.send_raw_packet(body):
                log.unconfirmed(entry)
                step(f"openHop was busy; {what} sent without confirmation")
            return entry

        def teach() -> None:
            # Ignored by a repeater that doesn't have the key in its ACL, so
            # only sent when it does (or is believed to).
            node_state["route_sent_at"] = now
            step(f"Sending our return route {route.label}")
            if one_hop:
                e = send(sk.build_route_update(
                    shared, key, out_path, plen, return_path=out_path, return_path_len=plen),
                    "the route", "route", f"return path via {route.relay_name or route.relay_id[:6]}")
            else:
                e = send(sk.build_route_update(shared, key, out_path, plen), "the route", "route",
                         "return path: direct")
            log.none_expected(e)
            sleep(LOGIN_SETTLE_SECONDS)

        def sort(r: sk.Reply, data_tag: int | None) -> tuple[sk.Reply | None, sk.LoginResult | None]:
            """A decoded reply as (data, None), (None, login answer) or neither.
            Data is matched by our tag; a login answer carries the repeater's
            clock instead, so it is told by shape, and never if its tag is one
            of ours (a late telemetry reply is not a login)."""
            if data_tag is not None and r.tag == data_tag:
                return r, None
            if r.tag in sent_tags:
                return None, None
            return None, sk.login_result(r)

        def wider(since: float, data_tag: int | None,
                  entry: dict) -> tuple[sk.Reply | None, sk.LoginResult | None]:
            """Ask CoreScope for what its observers heard since we sent."""
            if antenna is None:
                log.unanswered(entry)
                return None, None
            step("Not heard here; checking what CoreScope's observers heard")
            data = login = None
            fl = None
            reached = False
            for gap in (ANTENNA_SETTLE_SECONDS, *ANTENNA_RECHECKS):
                sleep(gap)
                frames = antenna(since - ANTENNA_CLOCK_SLACK)
                if frames is None:
                    continue
                reached = True
                for raw in frames:
                    r = sk.decode_rx(bytes([sk.PUSH_CODE_LOG_RX_DATA, 0, 0]) + raw, shared, [key])
                    if r is None:
                        continue
                    d, lr = sort(r, data_tag)
                    if d is not None and data is None:
                        data, fl = d, r.flooded
                    elif lr is not None and login is None and data_tag is None:
                        login, fl = lr, r.flooded
                if data is not None or login is not None:
                    break
            if data is None and login is None:
                if not reached:
                    step("CoreScope couldn't be reached")
                    log.unanswered(entry, "CoreScope unreachable")
                else:
                    step("No observer heard an answer either")
                    log.unanswered(entry, "not heard here or by CoreScope's observers")
                return None, None
            flooded.append(fl)
            if data is not None:
                data_flooded.append(fl)
            step(f"An observer heard the {'reply' if data is not None else 'login answer'} "
                 f"({'flooded' if fl else 'direct'})")
            log.answered(entry, "CoreScope", flooded=fl,
                         what=_describe(kind, data.data) if data is not None else _role(login))
            return data, login

        def request() -> sk.Reply | None:
            step(f"Requesting {kind}, waiting for a reply")
            body, data_tag = (sk.build_status_request if kind == "status"
                              else sk.build_telemetry_request)(shared, key, out_path, plen)
            sent_tags.add(data_tag)
            since = wall()
            entry = send(body, "the request", f"{kind} request")
            if kind == "status":
                node_state["status_checked_at"] = now
            t0 = time.monotonic()
            for frame in client.collect_rx(reply_window):
                r = sk.decode_rx(frame, shared, [key])
                if r is not None and r.tag == data_tag:
                    latency.append(time.monotonic() - t0)
                    flooded.append(r.flooded)
                    data_flooded.append(r.flooded)
                    step("Reply received" + (" (flooded: the repeater had no route back)" if r.flooded else ""))
                    log.answered(entry, "radio", flooded=r.flooded, after_s=latency[-1],
                                 what=_describe(kind, r.data))
                    return r
            return wider(since, data_tag, entry)[0]

        def login(pw: str) -> sk.LoginResult | None:
            """Send a login and listen (quietly) for its answer: off our radio,
            then from CoreScope's observers."""
            step(f"Login ({'blank' if pw == '' else repr(pw)}) {route.label}")
            body, tag = sk.build_login(shared, key, out_path, plen, pw)
            sent_tags.add(tag)
            since = wall()
            entry = send(body, "the login", "login", "blank password" if pw == "" else f"password {pw!r}")
            t0 = time.monotonic()
            for frame in client.collect_rx(LOGIN_ANSWER_WAIT + LOGIN_ANSWER_WAIT_PER_HOP * route.hops):
                r = sk.decode_rx(frame, shared, [key])
                lr = sort(r, None)[1] if r is not None else None
                if lr is not None:
                    flooded.append(r.flooded)
                    step("Login answered" + (" (as admin!)" if lr.is_admin else ""))
                    log.answered(entry, "radio", flooded=r.flooded, after_s=time.monotonic() - t0,
                                 what=_role(lr))
                    return lr
            return wider(since, None, entry)[1]

        reply = None
        admin = False
        tried = 0
        in_acl = bool(node_state.get("logged_in_at"))
        recheck_due = now - float(node_state.get("login_checked_at") or 0) >= SHARED_ADMIN_RECHECK_SECONDS

        # 1. Still in the guest ACL: the request, no login; the route first
        #    only when it may have been lost (see ROUTE_REFRESH_SECONDS).
        route_stale = (int(node_state.get("flood_replies") or 0) > 0
                       or node_state.get("code") not in (OK, NO_BATTERY)
                       or now - float(node_state.get("route_sent_at") or 0) >= ROUTE_REFRESH_SECONDS)
        if in_acl and not recheck_due:
            if route_stale:
                teach()
            tried += 1
            reply = request()
            if reply is None:
                # The entry may have lapsed (32-slot LRU, or a reboot).
                in_acl = False

        # 2. Log in, then route and request.
        logged_in = False
        login_answered = False
        if reply is None:
            logged_in = True
            if in_acl:
                # Admin re-check of a live entry: route first, so the login
                # answer comes back direct rather than flooded.
                teach()
            for pw in shared_password_order(node_state, now, personal, guest_password):
                if tried:
                    sleep(RETRY_GAP_SECONDS)
                tried += 1
                lr = login(pw)
                if lr is None:
                    continue  # no answer heard by us or any observer: not accepted
                login_answered = True
                node_state["login_checked_at"] = now
                if lr.is_admin:
                    admin = True
                    break
                if pw or personal:
                    node_state["shared_password"] = pw  # see shared_password_order
                answer_flooded = bool(flooded and flooded[-1])
                if not in_acl or answer_flooded:
                    # A flooded answer means the repeater has no route for us
                    # (a new entry, or a lapsed one on the admin re-check).
                    if answer_flooded:
                        step("The login answer was flooded; waiting for it to clear before sending our route")
                        sleep(FLOOD_CLEAR_SECONDS)
                    teach()  # now the repeater can read it
                    in_acl = True
                reply = request()
                break

        if logged_in:
            if login_answered:
                node_state.pop("key_login_fails", None)
            else:
                node_state["key_login_fails"] = int(node_state.get("key_login_fails") or 0) + 1
        # Only a data reply counts: each one follows a route packet, so a
        # flooded one means the route didn't take. A new key's first login
        # answer floods whatever we do and isn't held against the repeater.
        if data_flooded:
            if data_flooded[-1]:
                node_state["flood_replies"] = int(node_state.get("flood_replies") or 0) + 1
            else:
                node_state.pop("flood_replies", None)
        node_state["recent_tags"] = sorted(sent_tags)[-RECENT_TAGS:]
        if admin:
            # Whatever happens to the removal, the admin answer itself must be
            # recorded, so the key is disabled here and the owner is alerted.
            try:
                removed = _admin_removal_drill(client, shared, key, out_path, plen, sleep, log, route.label)
            except Exception as exc:  # noqa: BLE001 - never let the drill hide the admin answer
                step(f"Removing the key's admin access failed: {exc}")
                removed = False
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
        node_state.pop("logged_in_at", None)
        return PollResult(NO_REPLY, key_mode="shared", route=route, retried=tried > 1,
                          detail="no answer to the shared-key request")
    node_state["logged_in_at"] = node_state.get("logged_in_at") or now
    via = not latency  # the reply came from CoreScope, not our radio
    if kind == "status":
        try:
            stats = parse_stats(reply.data)
        except CompanionError:
            return PollResult(NO_REPLY, key_mode="shared", route=route, detail="unreadable status reply")
        mv = int(stats.get("battery_mv") or 0)
        return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, stats=stats, route=route,
                          key_mode="shared", method="status", via_corescope=via,
                          reply_s=latency[-1] if latency else None)
    temp = lpp_temperature(reply.data) if read_temperature else None
    temp_code = ("ok" if temp is not None else "none") if read_temperature else ""
    mv = lpp_voltage_mv(reply.data)
    if mv is None:
        return PollResult(NO_REPLY, key_mode="shared", route=route,
                          detail="telemetry answered without a voltage",
                          temperature_c=temp, temperature_code=temp_code)
    return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, route=route, key_mode="shared",
                      method="telemetry", temperature_c=temp, temperature_code=temp_code,
                      via_corescope=via, reply_s=latency[-1] if latency else None)


def _describe(kind: str, data: bytes) -> str:
    """A reply's content in a few words, for the packet log."""
    try:
        if kind == "status":
            return f"status, {int(parse_stats(data).get('battery_mv') or 0)} mV"
        mv = lpp_voltage_mv(data)
        temp = lpp_temperature(data)
        return "telemetry" + (f", {mv} mV" if mv is not None else ", no voltage") + \
            (f", {temp} °C" if temp is not None else "")
    except (CompanionError, ValueError, IndexError):
        return f"{len(data)} bytes"


def _role(lr: sk.LoginResult | None) -> str:
    return "" if lr is None else ("logged in as ADMIN" if lr.is_admin else "logged in as guest")


def _admin_removal_drill(client: CompanionClient, shared: sk.SharedKey, key: bytes,
                         out_path: bytes, plen: int, sleep: Callable[[float], None],
                         log: packet_log.NodeLog = packet_log.NULL, route_label: str = "") -> bool:
    """Undo an admin grant on the shared key, then confirm it is gone.

    Sends ``setperm <shared key> 0`` — which deletes the key's ACL entry and is
    admin-only, so it works precisely because the key still holds the admin
    rights it is removing — waits for the repeater to save it, then logs in
    again (blank) and checks the answer is no longer admin. Returns True once a
    verifying login comes back non-admin, False if none does within the tries.
    """
    try:
        e = log.sent("admin removal", route_label, "setperm <key> 0")
        client.send_raw_packet(sk.build_admin_removal(shared, key, out_path, plen))
        log.none_expected(e)
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
            e = log.sent("login", route_label, "blank password (verifying the removal)")
            client.send_raw_packet(sk.build_login(shared, key, out_path, plen, "")[0])
            # setperm 0 deleted the ACL entry and its return path, so teach the
            # route again or the verifying reply floods and we miss it.
            log.none_expected(log.sent("route", route_label, "after the removal"))
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
            if result is not None:
                log.answered(e, "radio", flooded=reply.flooded, what=_role(result))
            if result is not None and not result.is_admin:
                return True
        log.unanswered(e)
    return False


def record(node_state: dict, result: PollResult, password_set: bool, now: float) -> None:
    node_state["last_attempt"] = now
    node_state["last_covered"] = result.covered
    if result.covered:
        # We didn't hear it, but another observer did and CoreScope has the
        # reading. For scheduling that counts as heard (no retry, no backoff),
        # but it isn't our reading: battery_mv and last_success stay as they
        # were, and covered_at says when CoreScope had it.
        node_state["code"] = OK
        node_state["detail"] = result.detail
        node_state["covered_at"] = now
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
    if result.code not in (NO_REPLY, NOT_DIRECT, ERROR):
        # Any answer at all (a reading, a password refusal, an admin answer)
        # proves the repeater is alive: stop the daily-backoff clock.
        node_state.pop("unheard_since", None)
    if result.code in (OK, NO_BATTERY):
        node_state["last_success"] = now
        node_state["battery_mv"] = result.battery_mv
        node_state["no_reply_count"] = 0
        node_state["method"] = result.method
        node_state["via_corescope"] = result.via_corescope
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
        # A remembered guest password is kept through an outage — a repeater
        # that is merely out of reach hasn't changed its password. Only after
        # FORGET_PASSWORD_AFTER of silence is it re-learnt (blank, then hello).
        if now - float(node_state.get("unheard_since") or now) >= FORGET_PASSWORD_AFTER:
            node_state.pop("shared_password", None)
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
    if int(node_state.get("flood_replies") or 0) >= FLOOD_REPLIES_BEFORE_DAILY:
        # Its replies keep flooding: daily until one comes back direct.
        return now - last >= max(interval_s, FLOOD_DAILY)
    if code == NO_BATTERY:
        return now - last >= max(interval_s, NO_BATTERY_RECHECK)
    if code == NO_REPLY:
        # Quick retries only for a repeater that answered within the last day:
        # one that has been silent longer (or never answered) waits the normal
        # interval, so a dead link doesn't cost a login every 20 minutes.
        recent = now - float(node_state.get("last_success") or 0) < 24 * 3600
        fast = recent and int(node_state.get("no_reply_count") or 0) < NO_REPLY_FAST_TRIES
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
