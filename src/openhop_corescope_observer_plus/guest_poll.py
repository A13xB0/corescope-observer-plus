"""One poll under a guest key, as raw packets.

Requests are built and encrypted here and replies read straight off the air, so
the route is in the packet and nothing we send is ever flooded. A repeater with
no stored route back to the key floods its answer, so the flow keeps that route
fresh and sends as little as possible:

  1. Still in the repeater's guest ACL and the admin re-check isn't due: the
     route when it may be lost, then the request. No login.
  2. Otherwise, or if that went unanswered: log in, then route and request.

Replies we miss are looked for in what CoreScope's observers heard. A login
answered as admin stops the poll and runs the removal drill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import admin_drill, packet_log
from . import shared_key as sk
from .companion import (
    CompanionClient,
    CompanionError,
    lpp_temperature,
    lpp_voltage_mv,
    parse_stats,
)
from .guest_exchange import Antenna, GuestExchange
from .models import Neighbour, NodeState, Route
from .poll_common import (
    RETRY_GAP_SECONDS,
    STATUS_EVERY_SECONDS,
    SleepFn,
    StepFn,
    no_step,
    ordered_routes,
    status_is_due,
)
from .poll_result import ADMIN_STOP, ERROR, NO_BATTERY, NO_REPLY, NOT_DIRECT, OK, PollResult
from .poll_state import note_route_result

REPLY_WINDOW = 20.0  # how long to listen for a reply after each raw send
# Once a repeater has answered this key, polls keep asking with plain requests;
# only every FORGET_LOGIN_AFTER-th unanswered poll in a row logs in again, in
# case the entry was lost (a rebooted repeater forgets guests). A login is about
# 2.5x a request's airtime and relays forwarded it half as often (26 Oct).
FORGET_LOGIN_AFTER = 3
# The route is re-sent before a no-login request only when it may be gone: after
# a flooded reply, after a failed poll, or once this long since the last.
ROUTE_REFRESH_SECONDS = 24 * 3600
RECENT_TAGS = 16  # tags remembered, so a late reply is never read as a login answer
# A flooded answer is re-broadcast around for seconds and a route sent into that
# is lost (26 Oct: sent 1-4 s after, it never took, 5 of 5; sent in a quiet moment
# it always did). So wait before sending the route.
FLOOD_CLEAR_SECONDS = 15.0
# Only a login reply reveals a key quietly made admin, so one is forced this
# often even while the ACL entry holds.
SHARED_ADMIN_RECHECK_SECONDS = 24 * 3600


def shared_password_order(
    node_state: NodeState, now: float, personal: bool = False, configured: str | None = None
) -> list[str]:
    """The passwords to log in with in one poll, one login each.

    A login's answer floods when the repeater has no route back, so logins are
    kept to the fewest that can work. A known password is sent alone. On first
    contact blank goes first and "hello" only if blank went unanswered. Once the
    key's logins keep failing it is one per poll, blank and "hello" on alternate
    days. With the public shared key only "hello" is ever remembered: a blank
    login can succeed because another observer put the key in the ACL.
    """
    if configured in sk.SHARED_PASSWORDS:
        return [configured]
    known = node_state.get("shared_password")
    if known == "hello" or (personal and known == ""):
        return [known]
    if int(node_state.get("key_login_fails") or 0) >= 2:
        return [sk.SHARED_PASSWORDS[int(now // 86400) % len(sk.SHARED_PASSWORDS)]]
    return list(sk.SHARED_PASSWORDS)


def _pick_alternative(live: list[Route], best: Route) -> Route | None:
    """The next route after the best, for the poll's last retry."""
    return next((r for r in live if r.key != best.key), None)


def _forget_other_key(node_state: NodeState, shared: sk.SharedKey) -> None:
    # The ACL entry, its route and its password belong to the key.
    if node_state.get("acl_key") != shared.public_key.hex():
        for k in (
            "logged_in_at",
            "login_checked_at",
            "shared_password",
            "key_login_fails",
            "flood_replies",
        ):
            node_state.pop(k, None)
        node_state["acl_key"] = shared.public_key.hex()


@dataclass
class _Attempt:
    reply: sk.Reply | None = None
    admin: bool = False
    logged_in: bool = False  # logins were tried
    login_answered: bool = False


class _GuestPoll:
    def __init__(
        self,
        x: GuestExchange,
        live: list[Route],
        *,
        personal: bool,
        guest_password: str | None,
        alternative_routes: bool,
    ):
        self.x = x
        self.st = x.st
        self.now = x.now
        self.live = live
        self.personal = personal
        self.guest_password = guest_password
        self.alternative_routes = alternative_routes
        self.tried = 0

    def _route_may_be_lost(self) -> bool:
        return (
            int(self.st.get("flood_replies") or 0) > 0
            or self.st.get("code") not in (OK, NO_BATTERY)
            or self.now - float(self.st.get("route_sent_at") or 0) >= ROUTE_REFRESH_SECONDS
        )

    def _ask_without_login(self) -> sk.Reply | None:
        """Step 1. A lost packet is far likelier than a lost ACL entry (guests only
        drop out on a reboot or when 32 newer keys push them out), so ask again
        on the same route, then once by the next route, rather than log in."""
        x = self.x
        if self._route_may_be_lost():
            x.teach_route()
        self.tried += 1
        reply = x.request()
        if reply is not None:
            return reply
        x.sleep(RETRY_GAP_SECONDS)
        self.tried += 1
        x.step("No reply; asking again on the same route")
        reply = x.request()
        if reply is not None or not self.alternative_routes:
            return reply
        alternative = _pick_alternative(self.live, x.route)
        if alternative is None:
            return None
        x.sleep(RETRY_GAP_SECONDS)
        self.tried += 1
        note_route_result(self.st, x.route.key, False, self.now)
        x.use(alternative)
        x.step(f"Still no reply; asking by the alternative route {x.route.label}")
        x.teach_route()  # the return path differs
        return x.request()

    def _keep_entry_after_miss(self) -> bool:
        misses = int(self.st.get("no_reply_count") or 0) + 1
        if misses % FORGET_LOGIN_AFTER == 0:
            return False
        self.x.step(
            f"Still no reply; keeping the ACL entry (login only on every "
            f"{FORGET_LOGIN_AFTER}rd miss in a row; this is miss {misses})"
        )
        return True

    def _login_round(self, in_acl: bool, result: _Attempt) -> bool:
        """Log in on the current route; returns whether we are now in the ACL."""
        x = self.x
        result.reply, result.admin, result.login_answered = None, False, False
        if in_acl:
            # Admin re-check of a live entry: route first, so the login answer
            # comes back direct rather than flooded.
            x.teach_route()
        passwords = shared_password_order(self.st, self.now, self.personal, self.guest_password)
        for password in passwords:
            if self.tried:
                x.sleep(RETRY_GAP_SECONDS)
            self.tried += 1
            answer = x.login(password)
            if answer is None:
                continue  # no answer heard by us or any observer: not accepted
            result.login_answered = True
            self.st["login_checked_at"] = self.now
            if answer.is_admin:
                result.admin = True
                break
            if password or self.personal:
                self.st["shared_password"] = password
            answer_flooded = bool(x.flooded and x.flooded[-1])
            if not in_acl or answer_flooded:
                # A flooded answer means the repeater has no route for us. The
                # route only sticks once the key is in the ACL, hence after the login.
                if answer_flooded:
                    x.step(
                        "The login answer was flooded; "
                        "waiting for it to clear before sending our route"
                    )
                    x.sleep(FLOOD_CLEAR_SECONDS)
                x.teach_route()
                in_acl = True
            result.reply = x.request()
            break
        return in_acl

    def _log_in(self, in_acl: bool) -> _Attempt:
        """Step 2: on the best route, and if nothing answers, on the alternative.

        Without the second, a repeater whose best route broke while it lost our
        ACL entry could never be reached again: requests by the alternative need
        the entry, and the logins only went by the broken route.
        """
        x = self.x
        result = _Attempt(logged_in=True)
        alternative = _pick_alternative(self.live, x.best) if self.alternative_routes else None
        for route in [x.best] + ([alternative] if alternative is not None else []):
            if result.login_answered:
                break
            if route.key != x.route.key:
                if route.key != x.best.key:
                    note_route_result(self.st, x.route.key, False, self.now)
                    x.step(f"No login answer; logging in by the alternative route {route.label}")
                x.use(route)
            in_acl = self._login_round(in_acl, result)
        return result

    def attempt(self) -> _Attempt:
        in_acl = bool(self.st.get("logged_in_at"))
        checked_at = float(self.st.get("login_checked_at") or 0)
        recheck_due = self.now - checked_at >= SHARED_ADMIN_RECHECK_SECONDS
        if in_acl and not recheck_due:
            reply = self._ask_without_login()
            if reply is not None:
                return _Attempt(reply=reply)
            if self._keep_entry_after_miss():
                return _Attempt()
            in_acl = False
        return self._log_in(in_acl)

    def update_counters(self, attempt: _Attempt) -> None:
        st = self.st
        x = self.x
        if attempt.logged_in:
            if attempt.login_answered:
                st.pop("key_login_fails", None)
            else:
                st["key_login_fails"] = int(st.get("key_login_fails") or 0) + 1
        # Only a data reply counts: each follows a route packet, so a flooded one
        # means the route didn't take. A new key's first login answer floods
        # whatever we do and isn't held against the repeater.
        if x.data_flooded:
            if x.data_flooded[-1]:
                st["flood_replies"] = int(st.get("flood_replies") or 0) + 1
            else:
                st.pop("flood_replies", None)
        st["recent_tags"] = sorted(x.sent_tags)[-RECENT_TAGS:]

    def admin_stop(self) -> PollResult:
        """The admin answer is recorded whatever happens to the removal."""
        x = self.x
        try:
            removed = admin_drill.remove_admin(
                x.client, x.shared, x.key, x.out_path, x.plen, x.sleep, x.log, x.route.label
            )
        except Exception as exc:  # noqa: BLE001 - never let the drill hide the admin answer
            x.step(f"Removing the key's admin access failed: {exc}")
            removed = False
        return PollResult(
            ADMIN_STOP,
            role="admin",
            key_mode="shared",
            route=x.route,
            alternative=x.on_alternative,
            admin_seen=True,
            admin_removed=removed,
            detail="the repeater answered the shared key as admin",
        )

    def _no_reply(self, attempt: _Attempt) -> PollResult:
        # logged_in_at is kept: a single lost reply shouldn't cost a login.
        x = self.x
        if x.on_alternative:
            why = f"no answer on the best route or by the alternative ({x.route.label})"
        elif len(self.live) <= 1:
            why = "no answer; no other route heard"
        elif not self.alternative_routes:
            why = "no answer; alternative route not tried (setting off)"
        elif attempt.login_answered:
            why = "no answer to the request after the login was answered; alternative not needed"
        else:
            why = "no answer"
        return PollResult(
            NO_REPLY,
            key_mode="shared",
            route=x.route,
            alternative=x.on_alternative,
            retried=self.tried > 1,
            detail=why,
        )

    def _status_result(self, reply: sk.Reply, via_corescope: bool) -> PollResult:
        x = self.x
        try:
            stats = parse_stats(reply.data)
        except CompanionError:
            return PollResult(
                NO_REPLY,
                key_mode="shared",
                route=x.route,
                alternative=x.on_alternative,
                detail="unreadable status reply",
            )
        mv = int(stats.get("battery_mv") or 0)
        return PollResult(
            OK if mv > 0 else NO_BATTERY,
            battery_mv=mv,
            stats=stats,
            route=x.route,
            key_mode="shared",
            method="status",
            via_corescope=via_corescope,
            alternative=x.on_alternative,
            reply_s=x.latency[-1] if x.latency else None,
        )

    def _telemetry_result(
        self, reply: sk.Reply, via_corescope: bool, read_temperature: bool
    ) -> PollResult:
        x = self.x
        temp = lpp_temperature(reply.data) if read_temperature else None
        temp_code = ("ok" if temp is not None else "none") if read_temperature else ""
        mv = lpp_voltage_mv(reply.data)
        if mv is None:
            return PollResult(
                NO_REPLY,
                key_mode="shared",
                route=x.route,
                alternative=x.on_alternative,
                detail="telemetry answered without a voltage",
                temperature_c=temp,
                temperature_code=temp_code,
            )
        return PollResult(
            OK if mv > 0 else NO_BATTERY,
            battery_mv=mv,
            route=x.route,
            key_mode="shared",
            method="telemetry",
            temperature_c=temp,
            temperature_code=temp_code,
            via_corescope=via_corescope,
            alternative=x.on_alternative,
            reply_s=x.latency[-1] if x.latency else None,
        )

    def conclude(self, attempt: _Attempt, read_temperature: bool) -> PollResult:
        reply = attempt.reply
        if reply is None:
            return self._no_reply(attempt)
        self.st["logged_in_at"] = self.st.get("logged_in_at") or self.now
        via_corescope = not self.x.latency
        if self.x.kind == "status":
            return self._status_result(reply, via_corescope)
        return self._telemetry_result(reply, via_corescope, read_temperature)


def poll_one_shared(
    client: CompanionClient,
    n: Neighbour,
    shared: sk.SharedKey,
    node_state: NodeState,
    *,
    routes: list[Route],
    read_temperature: bool = False,
    now: float | None = None,
    sleep: SleepFn = time.sleep,
    reply_window: float = REPLY_WINDOW,
    on_step: StepFn | None = None,
    antenna: Antenna | None = None,
    personal: bool = False,
    status_every: float = STATUS_EVERY_SECONDS,
    plog: packet_log.NodeLog | None = None,
    guest_password: str | None = None,
    alternative_routes: bool = False,
) -> PollResult:
    """Poll one repeater under a guest key. See the module docstring for the flow."""
    now = now or time.time()
    step = on_step or no_step
    _forget_other_key(node_state, shared)
    try:
        key = bytes.fromhex(n.node_id)
        live = ordered_routes(routes, node_state.get("last_route"))
        if not live:
            step("No route heard")
            return PollResult(
                NOT_DIRECT, key_mode="shared", detail="no direct or one-hop route heard"
            )
        exchange = GuestExchange(
            client,
            shared,
            key,
            node_state,
            live[0],
            status_due=status_is_due(node_state, now, status_every),
            now=now,
            sleep=sleep,
            reply_window=reply_window,
            step=step,
            log=plog or packet_log.NULL,
            antenna=antenna,
        )
        client.clear_rx_backlog()  # only frames heard during this poll count
        poll = _GuestPoll(
            exchange,
            live,
            personal=personal,
            guest_password=guest_password,
            alternative_routes=alternative_routes,
        )
        attempt = poll.attempt()
        poll.update_counters(attempt)
        if attempt.admin:
            return poll.admin_stop()
    except (CompanionError, OSError, sk.SharedKeyError, sk.UnsafeSharedPassword, ValueError) as exc:
        # ValueError guards a bad route so one repeater can't abort the whole cycle.
        return PollResult(ERROR, key_mode="shared", detail=str(exc))
    return poll.conclude(attempt, read_temperature)
