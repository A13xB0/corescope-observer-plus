"""Folding a poll's result into the repeater's saved state."""

from __future__ import annotations

from .models import NodeState
from .poll_result import (
    ADMIN_STOP,
    ERROR,
    NEEDS_PASSWORD,
    NO_BATTERY,
    NO_REPLY,
    NOT_DIRECT,
    OK,
    PASSWORD_REJECTED,
    PollResult,
)

FORGET_PASSWORD_AFTER = 7 * 24 * 3600
ROUTE_RESULTS_KEPT = 8  # routes per repeater with a remembered outcome


def note_route_result(node_state: NodeState, route_key: str, answered: bool, now: float) -> None:
    """Remember a route's latest outcome, for the routes table on the page."""
    results = node_state.setdefault("route_results", {})
    results.pop(route_key, None)
    results[route_key] = [now, "answered" if answered else "no answer"]
    for k in list(results)[:-ROUTE_RESULTS_KEPT]:
        del results[k]


def _remember_route(node_state: NodeState, result: PollResult) -> None:
    if result.route is not None:
        node_state["last_route"] = result.route.key
        node_state["last_route_label"] = result.route.label


def _record_covered(node_state: NodeState, result: PollResult, now: float) -> None:
    # Counts as heard for scheduling, but it isn't our reading: battery_mv and
    # last_success stay as they were.
    node_state["code"] = OK
    node_state["detail"] = result.detail
    node_state["covered_at"] = now
    node_state.pop("unheard_since", None)
    node_state["no_reply_count"] = 0
    _remember_route(node_state, result)


def _record_reading(node_state: NodeState, result: PollResult, now: float) -> None:
    node_state["last_success"] = now
    node_state["battery_mv"] = result.battery_mv
    node_state["no_reply_count"] = 0
    node_state["method"] = result.method
    node_state["via_corescope"] = result.via_corescope
    _remember_route(node_state, result)
    if "uptime_secs" in result.stats:
        node_state["uptime_secs"] = result.stats["uptime_secs"]


def _record_no_reply(node_state: NodeState, now: float) -> None:
    node_state["no_reply_count"] = int(node_state.get("no_reply_count") or 0) + 1
    node_state.setdefault("unheard_since", now)  # starts the clock for the daily backoff
    # A repeater merely out of reach hasn't changed its guest password, so it is
    # only re-learnt after this long.
    if now - float(node_state.get("unheard_since") or now) >= FORGET_PASSWORD_AFTER:
        node_state.pop("shared_password", None)


def _record_temperature(node_state: NodeState, result: PollResult, now: float) -> None:
    node_state["temp_attempt"] = now
    node_state["temp_code"] = result.temperature_code
    if result.temperature_code == "ok":
        node_state["temperature_c"] = result.temperature_c
        node_state["temp_at"] = now
        node_state["temp_misses"] = 0
    else:
        node_state["temp_misses"] = int(node_state.get("temp_misses") or 0) + 1


def record(node_state: NodeState, result: PollResult, password_set: bool, now: float) -> None:
    node_state["last_attempt"] = now
    node_state["last_covered"] = result.covered
    if result.covered:
        _record_covered(node_state, result, now)
        return
    node_state["code"] = result.code
    node_state["detail"] = result.detail
    node_state["retried"] = result.retried
    if result.role:
        node_state["role"] = result.role
    if result.code not in (NO_REPLY, NOT_DIRECT, ERROR):
        # Any answer at all proves the repeater is alive: stop the backoff clock.
        node_state.pop("unheard_since", None)
    if result.route is not None and result.code in (OK, NO_BATTERY, NO_REPLY):
        note_route_result(node_state, result.route.key, result.code != NO_REPLY, now)
    if result.code in (OK, NO_BATTERY):
        _record_reading(node_state, result, now)
    elif result.code == NO_REPLY:
        _record_no_reply(node_state, now)
    if result.code in (NEEDS_PASSWORD, PASSWORD_REJECTED):
        node_state.pop("logged_in_at", None)
        node_state["login_fails"] = 0
        # Don't retry the same credential until the operator changes it.
        node_state["blocked_password_set"] = password_set
    if result.code == ADMIN_STOP:
        node_state["shared_admin_seen"] = True
        node_state["shared_admin_removed"] = result.admin_removed
        node_state["shared_admin_at"] = now
    if result.temperature_code:
        _record_temperature(node_state, result, now)
