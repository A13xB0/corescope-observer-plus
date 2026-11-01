"""One poll under the companion's own key, through openHop's companion commands.

Every request goes direct along a pinned path. If the path can't be pinned,
nothing is sent; nothing is ever flooded.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import packet_log
from .companion import CompanionClient, CompanionError, FloodSent, lpp_temperature, lpp_voltage_mv
from .models import JsonDict, Neighbour, NodeState, Route
from .poll_common import (
    LOGIN_SETTLE_SECONDS,
    RETRY_GAP_SECONDS,
    STATUS_EVERY_SECONDS,
    SleepFn,
    StepFn,
    describe_reply,
    no_step,
    ordered_routes,
    status_is_due,
)
from .poll_result import (
    ERROR,
    NEEDS_PASSWORD,
    NO_BATTERY,
    NO_REPLY,
    NOT_DIRECT,
    OK,
    PASSWORD_REJECTED,
    PollResult,
)

# Stock repeaters don't answer a wrong password at all, so a "login failed" push
# is usually a reply misread by openHop. Only this many in a row count.
LOGIN_FAILS_BEFORE_BLOCK = 3
REQUEST_ATTEMPTS = 3  # tries per request within one poll, all direct
# A repeater that ignores status but answers telemetry is asked by telemetry
# first; status is tried again after this long.
STATUS_RECHECK = 24 * 3600
TEMP_MISSES_BEFORE_DAILY = 3
TEMP_DAILY = 24 * 3600
# For a repeater silent all day an alternative would only double every poll's logins.
ALTERNATIVE_IF_HEARD_WITHIN = 24 * 3600


def temperature_due(node_state: NodeState, now: float) -> bool:
    if int(node_state.get("temp_misses") or 0) < TEMP_MISSES_BEFORE_DAILY:
        return True
    return now - float(node_state.get("temp_attempt") or 0) >= TEMP_DAILY


def _heard_recently(node_state: NodeState, now: float) -> bool:
    return now - float(node_state.get("last_success") or 0) < ALTERNATIVE_IF_HEARD_WITHIN


@dataclass
class _Answers:
    stats: JsonDict | None = None
    telemetry: bytes | None = None
    route: Route | None = None
    want_temp: bool = False


class _OwnKeyPoll:
    def __init__(
        self,
        client: CompanionClient,
        n: Neighbour,
        key: bytes,
        password: str,
        node_state: NodeState,
        ordered: list[Route],
        *,
        now: float,
        sleep: SleepFn,
        retry_gap: float,
        attempts: int,
        use_alternatives: bool,
        step: StepFn,
        log: packet_log.NodeLog,
    ):
        self.client = client
        self.n = n
        self.key = key
        self.password = password
        self.st = node_state
        self.ordered = ordered
        self.now = now
        self.sleep = sleep
        self.retry_gap = retry_gap
        self.attempts = attempts
        self.use_alternatives = use_alternatives
        self.step = step
        self.log = log
        self.proven: str | None = None  # the route a login was answered on
        self.role: str = node_state.get("role", "")
        self.pinned: str | None = None
        self.pinned_any = False
        self.login_sent = False
        self.refused = ""
        self.dead: set[str] = set()
        self.tries = 0
        self.blocked: PollResult | None = None

    def _pin(self, route: Route) -> bool:
        if self.pinned == route.key:
            return True
        if not self.client.pin_route(self.key, self.n.name, route.path, route.hash_size):
            self.pinned = None
            return False
        self.pinned, self.pinned_any = route.key, True
        return True

    def _login(self, route: Route, probe: bool = True) -> str:
        """ "ok", "silent" or "refused"; at most once per poll.

        Only a probe's silence marks the route dead: the reply to a first login
        is flooded and can arrive late or not at all while the route works.
        """
        self.step(f"Logging in {route.label}" + (" (verifying)" if not probe else ""))
        self.login_sent = True
        self.st["login_attempt_at"] = self.now
        how = "saved password" if self.password else "blank password"
        entry = self.log.sent("login", route.label, how)
        t0 = time.monotonic()
        result = self.client.login(self.key, self.password)
        if result.ok:
            self.log.answered(
                entry,
                "radio",
                after_s=time.monotonic() - t0,
                what=f"logged in as {result.role or 'guest'}",
            )
            self.proven = route.key
            self.st["logged_in_at"] = self.now
            self.st["login_fails"] = 0
            self.role = result.role
            self.sleep(LOGIN_SETTLE_SECONDS)
            return "ok"
        if result.timed_out:
            self.log.unanswered(entry)
            if probe:
                self.dead.add(route.key)
            return "silent"
        self.log.refused(entry, "login refused")
        fails = int(self.st.get("login_fails") or 0) + 1
        self.st["login_fails"] = fails
        if fails >= LOGIN_FAILS_BEFORE_BLOCK:
            code = PASSWORD_REJECTED if self.password else NEEDS_PASSWORD
            self.blocked = PollResult(code, role=self.role, route=route)
        self.refused = f"login refused ({fails} of {LOGIN_FAILS_BEFORE_BLOCK})"
        return "refused"

    def _logged(self, request: Callable[[], Any], kind: str, route: Route) -> Any:
        detail = "alternative route" if route.key != self.ordered[0].key else ""
        entry = self.log.sent(f"{kind} request", route.label, detail)
        t0 = time.monotonic()
        answer = request()
        if answer is None:
            self.log.unanswered(entry)
        else:
            what = (
                f"status, {int(answer.get('battery_mv') or 0)} mV"
                if kind == "status"
                else describe_reply(kind, answer)
            )
            self.log.answered(entry, "radio", after_s=time.monotonic() - t0, what=what)
        return answer

    def _next_route(self, i: int) -> Route | None:
        """The route for try i. Once a login on a route is answered, stay on it."""
        live = [r for r in self.ordered if r.key not in self.dead]
        if not live:
            return None
        proven = next((r for r in live if r.key == self.proven), None)
        if proven is not None:
            return proven
        if not self.use_alternatives:
            return self.ordered[0] if self.ordered[0].key not in self.dead else None
        return live[i % len(live)]

    def _ask(self, request: Callable[[], Any], kind: str) -> tuple[Any, Route | None]:
        """Up to ``attempts`` sends; the answer and the route it came by.

        An unanswered request is followed by one login per poll: it costs nothing
        when we're in the ACL, puts us back after a reboot, and tells a dead link
        from a lost reply.
        """
        sends = 0
        while sends < self.attempts and not self.blocked:
            route = self._next_route(sends)
            if route is None:
                break
            if sends:
                self.sleep(self.retry_gap)
            if not self._pin(route):
                self.dead.add(route.key)
                continue
            self.tries = max(self.tries, sends)
            sends += 1
            answer = self._logged(request, kind, route)
            if answer is not None:
                return answer, route
            if not self.login_sent and self._login(route) == "ok":
                answer = self._logged(request, kind, route)
                if answer is not None:
                    return answer, route
        return None, None

    def _telemetry_only(self, status_every: float) -> bool:
        # The status reply is about four times the airtime of telemetry, and
        # through a relay every byte costs twice, so a one-hop repeater is asked
        # by telemetry only. A repeater that ignored status is too, for a day.
        one_hop = all(r.hops > 0 for r in self.ordered)
        status_ignored = (
            bool(self.st.get("status_ignored"))
            and self.now - float(self.st.get("status_checked_at") or 0) < STATUS_RECHECK
        )
        return one_hop or status_ignored or not status_is_due(self.st, self.now, status_every)

    def _collect(self, telemetry_only: bool, read_temperature: bool) -> _Answers:
        got = _Answers()
        # A repeater ignores a client it doesn't know: log in first until one
        # login has worked.
        if not self.st.get("logged_in_at") and self._pin(self.ordered[0]):
            self._login(self.ordered[0], probe=False)
        if not telemetry_only:
            self.step("Requesting status")
            got.stats, got.route = self._ask(lambda: self.client.status(self.key), "status")
            self.st["status_checked_at"] = self.now
        got.want_temp = read_temperature and temperature_due(self.st, self.now)
        if got.stats is None or got.want_temp:
            self.step("Requesting telemetry")
            got.telemetry, tel_route = self._ask(
                lambda: self.client.telemetry(self.key), "telemetry"
            )
            if got.stats is None:
                got.route = tel_route
        return got

    def _no_reading_detail(self, got: _Answers) -> str:
        if got.telemetry is not None:
            return "telemetry answered without a voltage"
        if self.dead and len(self.dead) >= len(self.ordered):
            return "no answer to requests or login: the link looks down"
        return self.refused or "no answer to status or telemetry"

    def _conclude(self, got: _Answers, telemetry_only: bool) -> PollResult:
        if self.blocked:
            return self.blocked
        tel = got.telemetry
        if got.stats is not None or tel is not None:
            self.st.setdefault("logged_in_at", self.now)  # any answer proves it knows us
        if not self.pinned_any:
            return PollResult(NOT_DIRECT, detail="the companion would not keep the route")
        used = got.route
        retried = self.tries > 0 or self.login_sent and used is not None
        alternative = used is not None and used.key != self.ordered[0].key
        temp = lpp_temperature(tel) if tel is not None else None
        if tel is not None:
            temp_code = "ok" if temp is not None else "none"
        else:
            temp_code = "no_reply" if got.want_temp else ""

        if got.stats is not None:
            self.st["status_ignored"] = False
            mv = int(got.stats.get("battery_mv") or 0)
            return PollResult(
                OK if mv > 0 else NO_BATTERY,
                battery_mv=mv,
                role=self.role,
                stats=got.stats,
                route=used,
                alternative=alternative,
                retried=retried,
                temperature_c=temp,
                temperature_code=temp_code,
            )
        tel_mv = lpp_voltage_mv(tel) if tel is not None else None
        if tel_mv is None:
            return PollResult(
                NO_REPLY,
                role=self.role,
                retried=True,
                detail=self._no_reading_detail(got),
                temperature_c=temp,
                temperature_code=temp_code,
            )
        if not telemetry_only:
            self.st["status_ignored"] = True  # status went unanswered while telemetry came back
        return PollResult(
            OK if tel_mv > 0 else NO_BATTERY,
            battery_mv=tel_mv,
            role=self.role,
            route=used,
            alternative=alternative,
            retried=retried,
            temperature_c=temp,
            temperature_code=temp_code,
            method="telemetry",
        )

    def run(self, read_temperature: bool, status_every: float) -> PollResult:
        telemetry_only = self._telemetry_only(status_every)
        try:
            got = self._collect(telemetry_only, read_temperature)
        except FloodSent as exc:
            return PollResult(NOT_DIRECT, detail=str(exc))
        except (CompanionError, OSError) as exc:
            return PollResult(ERROR, detail=str(exc))
        return self._conclude(got, telemetry_only)


def poll_one(
    client: CompanionClient,
    n: Neighbour,
    password: str,
    node_state: NodeState,
    *,
    routes: list[Route],
    alternative_routes: bool = False,
    read_temperature: bool = False,
    now: float | None = None,
    sleep: SleepFn = time.sleep,
    retry_gap: float = RETRY_GAP_SECONDS,
    attempts: int = REQUEST_ATTEMPTS,
    on_step: StepFn | None = None,
    status_every: float = STATUS_EVERY_SECONDS,
    plog: packet_log.NodeLog | None = None,
) -> PollResult:
    """Read one repeater's battery with as few transmissions as will do."""
    now = now or time.time()
    step = on_step or no_step
    key = bytes.fromhex(n.node_id)
    ordered = ordered_routes(routes, node_state.get("last_route"))
    if not ordered:
        step("No route heard")
        return PollResult(NOT_DIRECT, detail="no direct or one-hop route heard")
    return _OwnKeyPoll(
        client,
        n,
        key,
        password,
        node_state,
        ordered,
        now=now,
        sleep=sleep,
        retry_gap=retry_gap,
        attempts=max(1, attempts),
        use_alternatives=alternative_routes and _heard_recently(node_state, now),
        step=step,
        log=plog or packet_log.NULL,
    ).run(read_temperature, status_every)
