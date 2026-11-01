"""Whether to poll a repeater now, or why not."""

from __future__ import annotations

import logging
from typing import TypedDict

from . import corescope
from .community import timings
from .coordination import Coordinator, Role
from .corescope import BatteryCheck
from .models import Neighbour, NodeState
from .station import Station
from .timeutil import iso_ts

LOG = logging.getLogger("openhop.corescope_observer_plus")


class Skip(TypedDict, total=False):
    """Why a repeater was left alone; kept in its state for the page."""

    reason: str
    text: str
    at: float
    by: str | None
    owner_hops: int | None
    until: float


def _skip(st: NodeState, reason: str, text: str, now: float, **extra: object) -> Skip:
    skip: Skip = {"reason": reason, "text": text, "at": now, **extra}  # type: ignore[typeddict-item]
    st["skip"] = skip
    return skip


def _by_text(role: Role) -> str:
    hops = role.get("owner_hops")
    route = " (direct)" if hops == 0 else " (1 hop)" if hops == 1 else ""
    return f"{role.get('by') or 'another observer'} polls it{route}"


class PollDecisions:
    def __init__(self, station: Station, coordinator: Coordinator):
        self.station = station
        self.coordinator = coordinator

    def _cap_reached(self, st: NodeState, window: int, now: float) -> Skip | None:
        limit = self.station.community.max_requests()
        sends = [x for x in st.get("sends") or [] if now - x < window]
        if len(sends) < limit:
            return None
        st["next_due_at"] = min(sends) + window
        text = f"{len(sends)} requests already sent within the window (CoreScope allows {limit})"
        return _skip(st, "cap", text, now, until=st["next_due_at"])

    def _newest_reading(self, node_id: str, snap: BatteryCheck) -> float | None:
        latest = iso_ts(snap.get("latest_ts"))
        owner = self.coordinator.owners_cache.nodes.get(node_id) or {}
        owner_latest = iso_ts(owner.get("latest_ts"))
        if owner_latest is not None and (latest is None or owner_latest > latest):
            return owner_latest
        return latest

    def _freshness(
        self, n: Neighbour, st: NodeState, role: Role, window: int, now: float
    ) -> Skip | None:
        """Ask CoreScope whether a reading inside the window is already there."""
        station = self.station
        standby = role["role"] == "standby"
        by_text = _by_text(role)
        hops = role.get("owner_hops")
        # Standbys queue, each a grace period behind the last, so one steps in
        # for a silent poller and its reading stands the others down.
        wait = window + (station.community.grace_s() * role["rank"] if standby else 0)
        reachable, snap = corescope.battery_check(station.settings.corescope_url, n.node_id)
        if not reachable:
            if not standby:
                return None  # the poller fails open
            # A standby holds through an outage (the poller is probably fine);
            # once CoreScope's owners lapse this observer polls again.
            st["next_due_at"] = now + timings().hold_recheck_s
            text = f"{by_text}; you're standby (CoreScope unreachable, holding)"
            return _skip(st, "standby", text, now, by=role.get("by"), owner_hops=hops)
        latest = self._newest_reading(n.node_id, snap)
        hold_from = self.coordinator.standby_hold_from()
        if latest is None and standby:
            # No reading anywhere yet: give the poller one window plus grace from
            # when this observer went on standby before stepping in.
            until = max(float(st["standby_since"]), hold_from) + wait
            if now < until:
                st["next_due_at"] = until
                text = f"{by_text}; you're standby"
                return _skip(
                    st, "standby", text, now, by=role.get("by"), owner_hops=hops, until=until
                )
        if latest is not None and standby:
            # Every timer runs out during a pause or an outage of this plugin:
            # the wait restarts, or poller and standbys would all ask at once.
            latest = max(latest, hold_from)
        if latest is not None and now - latest < wait:
            by = snap.get("by") or ""
            st["next_due_at"] = latest + wait
            st["fresh_at"], st["fresh_by"] = latest, by
            if standby:
                text = f"{by_text}; you're standby"
                return _skip(
                    st,
                    "standby",
                    text,
                    now,
                    by=role.get("by"),
                    owner_hops=hops,
                    until=latest + wait,
                )
            mins = max(0, round((now - latest) / 60))
            text = f"{by or 'another observer'} sent a reading {mins} min ago"
            return _skip(st, "fresh", text, now, by=by, until=latest + wait)
        if standby:
            LOG.info(
                "standby for %s (%s): newest reading is older than the window plus grace; "
                "stepping in",
                n.name,
                n.node_id[:8],
            )
        return None

    def skip_reason(self, n: Neighbour, now: float, mode: str) -> Skip | None:
        """Why this repeater is not polled now, or None to poll it.

        mode: "scheduled", "stale" (Check now, only stale), "all" (Check now, all
        of them) or "test". Every mode stops for CoreScope's switches and
        exclusions; all but Test keep to the request cap; scheduled and stale
        also stand by and check for a reading inside the window.
        """
        station = self.station
        node_id = n.node_id
        st = station.state.node(node_id)
        gate = station.community.gate(now)
        if gate:
            self.coordinator.gated_at = now
            return _skip(st, gate["reason"], gate["text"], now)
        if station.community.excluded(node_id):
            return _skip(st, "excluded", "Excluded by CoreScope", now)
        window = station.window_s(node_id, now)
        if mode != "test":
            capped = self._cap_reached(st, window, now)
            if capped:
                return capped
        if mode in ("test", "all"):
            return None
        role = self.coordinator.role_for(node_id, now)
        if role["role"] == "excluded":
            return _skip(st, "excluded", "Excluded by CoreScope", now)
        if role["role"] == "paused":
            return _skip(st, "paused", "CoreScope has paused polling", now)
        if role["role"] == "disabled":
            return _skip(st, "disabled", "CoreScope has disabled this observer", now)
        if role["role"] == "standby":
            st.setdefault("standby_since", now)
        else:
            st.pop("standby_since", None)
        if mode == "scheduled" and self.coordinator.starting(now):
            # Just started: claim and hear who polls what before asking anything,
            # so stations restarting together don't all ask at once. No
            # next_due_at: polls start on the first tick after CoreScope answers.
            return _skip(st, "starting", "waiting to hear from CoreScope who polls it", now)
        if mode == "stale" and now - float(st.get("last_success") or 0) < window:
            return _skip(st, "fresh_own", "this observer read it within the window", now)
        if not station.settings.corescope_url:
            return None  # nothing to ask: poll
        return self._freshness(n, st, role, window, now)
