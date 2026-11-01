"""One round of polling: which repeaters, in what order, under which key."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from . import corescope, guest_poll, outbox, own_key_poll
from . import shared_key as sk
from .community import timings
from .companion import CompanionClient
from .coordination import Coordinator
from .models import JsonDict, Neighbour, NodeState, Route
from .openhop_config import companion_port
from .poll_common import StepFn
from .poll_decisions import PollDecisions, Skip
from .poll_result import ADMIN_STOP, NO_BATTERY, OK, PollResult
from .poll_state import record
from .polling_keys import PollingKeys
from .schedule import is_due
from .station import Station
from .timeutil import utc_iso

LOG = logging.getLogger("openhop.corescope_observer_plus")

SENDS_KEPT = 12  # request times kept per repeater, for the request cap
SENDS_MAX_AGE = 2 * 86400
SILENT_REASONS = ("paused", "blocked", "version_disabled")  # logged once, by the policy

NodeFn = Callable[[Neighbour, NodeState], None]
SkipFn = Callable[[Neighbour, Skip], None]


def _log_result(n: Neighbour, result: PollResult) -> None:
    alternative = " (alternative route: best got no answer)" if result.alternative else ""
    shared = " [shared]" if result.key_mode == "shared" else ""
    reading = f"{result.battery_mv} mV" if result.battery_mv is not None else result.detail
    if result.temperature_c is not None:
        temperature = f", {result.temperature_c} °C"
    else:
        temperature = f", temperature {result.temperature_code}" if result.temperature_code else ""
    if result.reply_s is not None:
        reply = f", reply {result.reply_s:.1f} s"
    else:
        reply = " (heard via CoreScope)" if result.via_corescope else ""
    route = result.route.label if result.route else "-"
    LOG.info(
        "poll %s (%s) %s%s%s: %s %s%s%s",
        n.name,
        n.node_id[:8],
        route,
        alternative,
        shared,
        result.code,
        reading,
        temperature,
        reply,
    )


class PollCycle:
    def __init__(
        self,
        station: Station,
        coordinator: Coordinator,
        keys: PollingKeys,
        decisions: PollDecisions,
        on_progress: Callable[[], None],
    ):
        self.station = station
        self.coordinator = coordinator
        self.keys = keys
        self.decisions = decisions
        self.on_progress = on_progress
        self.last_scheduled = 0.0

    def _due(
        self, node_ids: list[str], scheduled: bool, now: float, retry_s: float
    ) -> list[Neighbour]:
        station = self.station
        # Each target polls in a slot of its own within its interval, so the
        # scheduled polls are spread out rather than sent in bursts.
        phases = station.phases(now)
        due = []
        for node_id in node_ids:
            interval = station.interval_s(node_id, now)
            n = station.neighbour(node_id)
            st = station.state.node(node_id)
            if n is None or not station.routes_for(n):
                st["code"] = "not_direct" if n else "not_heard"
                continue
            if scheduled:
                if now < float(st.get("next_due_at") or 0):
                    continue  # someone's reading is fresh, the cap is reached, or standing by
                password_set = station.secrets.has_password(node_id)
                phase = phases.get(node_id)
                if not is_due(st, interval, password_set, now, phase, retry_s=retry_s):
                    continue
            due.append(n)
        due.sort(key=lambda n: float(station.state.node(n.node_id).get("last_attempt") or 0))
        return due

    def _choose(self, due: list[Neighbour], mode: str, on_skip: SkipFn | None) -> list[Neighbour]:
        chosen = []
        for n in due:
            skip = self.decisions.skip_reason(n, time.time(), mode)
            if skip:
                if skip["reason"] not in SILENT_REASONS:
                    LOG.info("skip %s (%s): %s", n.name, n.node_id[:8], skip["text"])
                if on_skip:
                    on_skip(n, skip)
                continue
            chosen.append(n)
            if mode == "scheduled":
                break  # one scheduled poll per tick
        return chosen

    def _antenna(self) -> guest_poll.Antenna | None:
        """Replies we don't hear ourselves are looked for in CoreScope's observers' packets."""
        settings = self.station.settings
        if not (settings.check_corescope and settings.corescope_url):
            return None
        url = settings.corescope_url

        def antenna(since: float) -> list[bytes] | None:
            return corescope.responses(url, utc_iso(since))

        return antenna

    def _key_label(self, key: sk.SharedKey | None) -> str:
        if key is None:
            return "companion"
        return "guest key" if self.keys.is_own(key) else "shared key"

    def _poll_node(
        self,
        client: CompanionClient,
        n: Neighbour,
        routes: list[Route],
        st: NodeState,
        on_step: StepFn | None,
    ) -> tuple[PollResult, bool]:
        """Poll one repeater; the result, and whether it went under a guest key."""
        station = self.station
        settings = station.settings
        password = station.secrets.password(n.node_id)
        key = self.keys.polling_key() if self.keys.uses_shared_key(n.node_id) else None
        plog = station.packets.for_node(
            n.node_id, n.name, self._key_label(key), st.setdefault("packets", [])
        )
        read_temperature = n.node_id in settings.temperature_targets
        status_every = settings.status_every_hours * 3600
        if key is not None:
            return guest_poll.poll_one_shared(
                client,
                n,
                key,
                st,
                routes=routes,
                read_temperature=read_temperature,
                on_step=on_step,
                antenna=self._antenna(),
                personal=self.keys.is_own(key),
                status_every=status_every,
                plog=plog,
                guest_password=settings.guest_passwords.get(n.node_id),
                alternative_routes=settings.alternative_routes,
            ), True
        return own_key_poll.poll_one(
            client,
            n,
            password,
            st,
            routes=routes,
            alternative_routes=settings.alternative_routes,
            read_temperature=read_temperature,
            on_step=on_step,
            status_every=status_every,
            plog=plog,
        ), False

    def _finish_node(
        self, n: Neighbour, st: NodeState, result: PollResult, guest_key: bool
    ) -> JsonDict | None:
        """Save a poll's outcome; the reading to publish, if there is one."""
        station = self.station
        record(st, result, bool(station.secrets.password(n.node_id)), time.time())
        st.pop("password_changed", None)
        if result.code in (OK, NO_BATTERY) and not result.covered:
            st.pop("covered_at", None)  # our own reading supersedes an observer's
        st["name"] = n.name
        if result.code == ADMIN_STOP:
            self.keys.disable_for(n, result)
        _log_result(n, result)
        if guest_key:
            station.keep_request_clock()
        # A guest-key reading isn't published: CoreScope decodes the raw reply
        # (which openHop uploads like any observer) and credits this poller via
        # the claim, so publishing it too would double the row.
        if result.code in (OK, NO_BATTERY) and result.key_mode != "shared":
            return outbox.reading(n, result, time.time())
        return None

    def poll(
        self,
        node_ids: list[str],
        *,
        force: bool = False,
        mode: str | None = None,
        on_node: NodeFn | None = None,
        on_step: StepFn | None = None,
        on_skip: SkipFn | None = None,
    ) -> list[JsonDict]:
        """Poll the given targets that are reachable now; return payload readings.

        mode is "scheduled" (default), "stale", "all" or "test" (see
        PollDecisions); force=True without a mode is "all". Scheduled polls go out
        one per tick at least scheduled_gap_s apart, so a backlog drains gently.
        """
        mode = mode or ("all" if force else "scheduled")
        scheduled = mode == "scheduled"
        station = self.station
        if not station.check_ok("companion") or not station.ohcfg:
            return []
        port = companion_port(station.ohcfg, station.settings.companion_name)
        now = time.time()
        t = timings()
        due = self._due(node_ids, scheduled, now, t.no_reply_retry_s)
        if not due or (scheduled and time.time() - self.last_scheduled < t.scheduled_gap_s):
            return []
        # A policy older than a minute is pulled again before any send, so a
        # pause or block reaches the radio within a minute.
        self.coordinator.refresh_policy(now, t.policy_cache_s)
        chosen = self._choose(due, mode, on_skip)
        station.state.save()
        if not chosen:
            return []
        assert port is not None  # the companion check passed
        out = []
        with CompanionClient(station.companion_host, port) as client:
            for i, n in enumerate(chosen):
                routes = station.routes_for(n)
                if i:
                    # A poll through a relay costs the relay two transmissions.
                    gap = station.settings.spacing_seconds * (2 if routes[0].hops else 1)
                    time.sleep(gap)
                st = station.state.node(n.node_id)
                # Every request counts toward the per-window cap (quick retries too).
                recent = [x for x in st.get("sends") or [] if time.time() - x < SENDS_MAX_AGE]
                st["sends"] = (recent + [time.time()])[-SENDS_KEPT:]
                for k in ("skip", "next_due_at"):
                    st.pop(k, None)
                result, guest_key = self._poll_node(client, n, routes, st, on_step)
                reading = self._finish_node(n, st, result, guest_key)
                if reading is not None:
                    out.append(reading)
                station.state.save()
                # A cycle can take many minutes: keep the page current.
                self.on_progress()
                if on_node:
                    on_node(n, st)
        if scheduled:
            self.last_scheduled = time.time()
        return out
