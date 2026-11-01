"""When a repeater is next due: slots, retries and backoffs."""

from __future__ import annotations

import math
from collections.abc import Callable

from .models import NodeState
from .poll_result import NEEDS_PASSWORD, NO_BATTERY, NO_REPLY, PASSWORD_REJECTED

NO_BATTERY_RECHECK = 24 * 3600
# A silent repeater gets a quick retry for its first two misses in a row, then
# waits for its normal slot. It is never given up on. (Nine quick tries, as of
# 26 Oct, mostly failed again and only added load.)
NO_REPLY_RETRY = 20 * 60
NO_REPLY_FAST_TRIES = 2
UNHEARD_BACKOFF_AFTER = 24 * 3600
UNHEARD_BACKOFF_INTERVAL = 24 * 3600
# A repeater whose replies keep flooding is polled daily, so it can't flood the
# mesh every interval.
FLOOD_REPLIES_BEFORE_DAILY = 2
FLOOD_DAILY = 24 * 3600


def _slot_due(last: float, now: float, every: float, phase: float | None) -> bool:
    """Due once per ``every``: at ``phase`` into each period, else ``every`` after the last."""
    if phase is None:
        return now - last >= every
    slot = math.floor((now - phase) / every) * every + phase  # latest slot start up to now
    # At least half a period's gap, so a Test just before the slot isn't
    # followed straight away by the scheduled poll.
    return last < slot and now - last >= every / 2


def _scaled(phase: float | None, interval_s: float, every: float) -> float | None:
    """The same relative slot in a longer period."""
    return None if phase is None or interval_s <= 0 else phase / interval_s * every


def spread_phases(node_ids: list[str], interval_for: Callable[[str], float]) -> dict[str, float]:
    """An even offset per repeater, so those sharing an interval are spaced across it."""
    groups: dict[float, list[str]] = {}
    for node_id in sorted(set(node_ids)):
        groups.setdefault(float(interval_for(node_id)), []).append(node_id)
    return {
        node_id: every * i / len(ids)
        for every, ids in groups.items()
        for i, node_id in enumerate(ids)
    }


def is_due(
    node_state: NodeState,
    interval_s: float,
    password_set: bool,
    now: float,
    phase: float | None = None,
    retry_s: float = NO_REPLY_RETRY,
) -> bool:
    code = node_state.get("code")
    last = float(node_state.get("last_attempt") or 0)
    # Rejected: only once the credential changed (set, replaced or removed).
    if (
        code in (NEEDS_PASSWORD, PASSWORD_REJECTED)
        and node_state.get("blocked_password_set") == password_set
        and not node_state.get("password_changed")
    ):
        return False
    unheard = node_state.get("unheard_since")
    if unheard is not None and now - float(unheard) >= UNHEARD_BACKOFF_AFTER:
        daily = UNHEARD_BACKOFF_INTERVAL
        return _slot_due(last, now, daily, _scaled(phase, interval_s, daily))
    if int(node_state.get("flood_replies") or 0) >= FLOOD_REPLIES_BEFORE_DAILY:
        every = max(interval_s, FLOOD_DAILY)
        return _slot_due(last, now, every, _scaled(phase, interval_s, every))
    if code == NO_BATTERY:
        every = max(interval_s, NO_BATTERY_RECHECK)
        return _slot_due(last, now, every, _scaled(phase, interval_s, every))
    if code == NO_REPLY:
        # Quick retries only for a repeater heard within the last day, so a dead
        # link doesn't cost a login every 20 minutes.
        recent = now - float(node_state.get("last_success") or 0) < 24 * 3600
        if recent and int(node_state.get("no_reply_count") or 0) < NO_REPLY_FAST_TRIES:
            return now - last >= min(interval_s, retry_s)
    return _slot_due(last, now, interval_s, phase)


def next_slot(
    node_state: NodeState, interval_s: float, now: float, phase: float | None = None
) -> float:
    """Roughly when the normal schedule next polls (the page's "next HH:MM")."""
    last = float(node_state.get("last_attempt") or 0)
    if phase is None:
        return max(now, last + interval_s)
    slot = math.floor((now - phase) / interval_s) * interval_s + phase
    if last < slot and now - last >= interval_s / 2:
        return now
    return slot + interval_s
