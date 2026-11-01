"""The packets of one guest-key poll: sending, listening, and asking CoreScope."""

from __future__ import annotations

import time
from collections.abc import Callable

from . import packet_log
from . import shared_key as sk
from .companion import CompanionClient, path_len_byte
from .models import JsonDict, NodeState, Route
from .poll_common import (
    LOGIN_SETTLE_SECONDS,
    SleepFn,
    StepFn,
    describe_login,
    describe_reply,
)

Antenna = Callable[[float], "list[bytes] | None"]
Answer = tuple["sk.Reply | None", "sk.LoginResult | None"]

# After a login, listen this long before transmitting again. The radio is half
# duplex and the repeater answers 300 ms after hearing it, so a route sent
# straight away talked over the answer. Generous because listening costs no
# airtime: a new key's answer is flooded back through relays, each adding a
# random delay.
LOGIN_ANSWER_WAIT = 10.0
LOGIN_ANSWER_WAIT_PER_HOP = 10.0
# Give other observers' copies this long to reach CoreScope, and ask for packets
# from a little before we sent (clocks differ).
ANTENNA_SETTLE_SECONDS = 5.0
ANTENNA_CLOCK_SLACK = 30.0
# Then look again after these gaps. Right after a flood, ingest took over 20 s
# (26 Oct: three answered logins missed). An HTTP request costs no airtime.
ANTENNA_RECHECKS = (10.0, 15.0)


class GuestExchange:
    def __init__(
        self,
        client: CompanionClient,
        shared: sk.SharedKey,
        key: bytes,
        node_state: NodeState,
        route: Route,
        *,
        status_due: bool,
        now: float,
        sleep: SleepFn,
        reply_window: float,
        step: StepFn,
        log: packet_log.NodeLog,
        antenna: Antenna | None,
    ):
        self.client = client
        self.shared = shared
        self.key = key
        self.st = node_state
        self.best = route
        self.status_due = status_due
        self.now = now
        self.sleep = sleep
        self.reply_window = reply_window
        self.step = step
        self.log = log
        self.antenna = antenna
        self.latency: list[float] = []
        # A reply carrying one of our own recent tags is never a login answer.
        self.sent_tags: set[int] = {int(t) for t in node_state.get("recent_tags") or []}
        self.flooded: list[bool] = []  # one per reply we got, ours or CoreScope's
        self.data_flooded: list[bool] = []  # the same, for data replies only
        self.use(route)

    def use(self, route: Route) -> None:
        """Move the poll's sends to this route."""
        self.route = route
        self.out_path = route.path
        self.plen = path_len_byte(self.out_path, route.hash_size)
        self.one_hop = route.hops > 0
        # Telemetry every poll; status only when due, and never through a relay.
        self.kind = "status" if (not self.one_hop and self.status_due) else "telemetry"
        self.on_alternative = route.key != self.best.key

    def _send(self, body: bytes, what: str, kind: str, detail: str = "") -> JsonDict:
        if self.on_alternative:
            detail = f"{detail} · alternative route" if detail else "alternative route"
        entry = self.log.sent(kind, self.route.label, detail)
        # openHop confirms a raw send only after transmitting; a long wait means
        # it was busy, not that the packet was refused.
        if not self.client.send_raw_packet(body):
            self.log.unconfirmed(entry)
            self.step(f"openHop was busy; {what} sent without confirmation")
        return entry

    def teach_route(self) -> None:
        """Send our return route. Ignored unless the key is in the repeater's ACL."""
        self.st["route_sent_at"] = self.now
        route = self.route
        self.step(f"Sending our return route {route.label}")
        if self.one_hop:
            body = sk.build_route_update(
                self.shared,
                self.key,
                self.out_path,
                self.plen,
                return_path=self.out_path,
                return_path_len=self.plen,
            )
            detail = f"return path via {route.relay_name or route.relay_id[:6]}"
        else:
            body = sk.build_route_update(self.shared, self.key, self.out_path, self.plen)
            detail = "return path: direct"
        self.log.none_expected(self._send(body, "the route", "route", detail))
        self.sleep(LOGIN_SETTLE_SECONDS)

    def _sort(self, reply: sk.Reply, data_tag: int | None) -> Answer:
        """A decoded reply as (data, None), (None, login answer) or neither.

        Data is matched by tag. A login answer carries the repeater's clock
        instead, so it is told by shape, and never if its tag is one of ours (a
        late telemetry reply is not a login).
        """
        if data_tag is not None and reply.tag == data_tag:
            return reply, None
        if reply.tag in self.sent_tags:
            return None, None
        return None, sk.login_result(reply)

    def _search_observers(
        self, antenna: Antenna, since: float, data_tag: int | None
    ) -> tuple[sk.Reply | None, sk.LoginResult | None, bool, bool]:
        """(data, login answer, was it flooded, did CoreScope answer at all)."""
        data: sk.Reply | None = None
        login: sk.LoginResult | None = None
        flooded = False
        reached = False
        for gap in (ANTENNA_SETTLE_SECONDS, *ANTENNA_RECHECKS):
            self.sleep(gap)
            frames = antenna(since - ANTENNA_CLOCK_SLACK)
            if frames is None:
                continue
            reached = True
            for raw in frames:
                frame = bytes([sk.PUSH_CODE_LOG_RX_DATA, 0, 0]) + raw
                reply = sk.decode_rx(frame, self.shared, [self.key])
                if reply is None:
                    continue
                d, lr = self._sort(reply, data_tag)
                if d is not None and data is None:
                    data, flooded = d, reply.flooded
                elif lr is not None and login is None and data_tag is None:
                    login, flooded = lr, reply.flooded
            if data is not None or login is not None:
                break
        return data, login, flooded, reached

    def _ask_observers(self, since: float, data_tag: int | None, entry: JsonDict) -> Answer:
        """After anything we didn't hear: what CoreScope's observers heard since we sent."""
        if self.antenna is None:
            self.log.unanswered(entry)
            return None, None
        self.step("Not heard here; checking what CoreScope's observers heard")
        data, login, flooded, reached = self._search_observers(self.antenna, since, data_tag)
        if data is None and login is None:
            if not reached:
                self.step("CoreScope couldn't be reached")
                self.log.unanswered(entry, "CoreScope unreachable")
            else:
                self.step("No observer heard an answer either")
                self.log.unanswered(entry, "not heard here or by CoreScope's observers")
            return None, None
        self.flooded.append(flooded)
        if data is not None:
            self.data_flooded.append(flooded)
        self.step(
            f"An observer heard the {'reply' if data is not None else 'login answer'} "
            f"({'flooded' if flooded else 'direct'})"
        )
        what = describe_reply(self.kind, data.data) if data is not None else describe_login(login)
        self.log.answered(entry, "CoreScope", flooded=flooded, what=what)
        return data, login

    def request(self) -> sk.Reply | None:
        """Ask for this poll's kind of data and wait for the reply."""
        kind = self.kind
        self.step(f"Requesting {kind}, waiting for a reply")
        build = sk.build_status_request if kind == "status" else sk.build_telemetry_request
        body, data_tag = build(self.shared, self.key, self.out_path, self.plen)
        self.sent_tags.add(data_tag)
        since = time.time()
        entry = self._send(body, "the request", f"{kind} request")
        if kind == "status":
            self.st["status_checked_at"] = self.now
        t0 = time.monotonic()
        for frame in self.client.collect_rx(self.reply_window):
            r = sk.decode_rx(frame, self.shared, [self.key])
            if r is not None and r.tag == data_tag:
                self.latency.append(time.monotonic() - t0)
                self.flooded.append(r.flooded)
                self.data_flooded.append(r.flooded)
                note = " (flooded: the repeater had no route back)" if r.flooded else ""
                self.step("Reply received" + note)
                self.log.answered(
                    entry,
                    "radio",
                    flooded=r.flooded,
                    after_s=self.latency[-1],
                    what=describe_reply(kind, r.data),
                )
                return r
        return self._ask_observers(since, data_tag, entry)[0]

    def login(self, password: str) -> sk.LoginResult | None:
        """Send a login and listen (quietly) for its answer, here and via CoreScope."""
        route = self.route
        self.step(f"Login ({'blank' if password == '' else repr(password)}) {route.label}")
        body, tag = sk.build_login(self.shared, self.key, self.out_path, self.plen, password)
        self.sent_tags.add(tag)
        since = time.time()
        how = "blank password" if password == "" else f"password {password!r}"
        entry = self._send(body, "the login", "login", how)
        t0 = time.monotonic()
        window = LOGIN_ANSWER_WAIT + LOGIN_ANSWER_WAIT_PER_HOP * route.hops
        for frame in self.client.collect_rx(window):
            r = sk.decode_rx(frame, self.shared, [self.key])
            answer = self._sort(r, None)[1] if r is not None else None
            if r is not None and answer is not None:
                self.flooded.append(r.flooded)
                self.step("Login answered" + (" (as admin!)" if answer.is_admin else ""))
                self.log.answered(
                    entry,
                    "radio",
                    flooded=r.flooded,
                    after_s=time.monotonic() - t0,
                    what=describe_login(answer),
                )
                return answer
        return self._ask_observers(since, None, entry)[1]
