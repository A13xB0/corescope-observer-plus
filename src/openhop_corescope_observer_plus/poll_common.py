"""What the own-key and guest-key polls share."""

from __future__ import annotations

from collections.abc import Callable

from . import shared_key as sk
from .companion import CompanionError, lpp_temperature, lpp_voltage_mv, parse_stats
from .models import NodeState, Route

StepFn = Callable[[str], None]
SleepFn = Callable[[float], None]

LOGIN_SETTLE_SECONDS = 1.0
RETRY_GAP_SECONDS = 8.0
# Telemetry (battery, temperature) is a 12-byte guest reply and status is 60, so
# every poll asks for telemetry and full status only when the last is this old.
STATUS_EVERY_SECONDS = 6 * 3600


def no_step(_label: str) -> None:
    pass


def ordered_routes(routes: list[Route], last_key: str | None) -> list[Route]:
    """Best first, with the route that last answered moved to the top."""
    out = list(routes)
    for i, r in enumerate(out):
        if r.key == last_key:
            out.insert(0, out.pop(i))
            break
    return out


def status_is_due(node_state: NodeState, now: float, every: float = STATUS_EVERY_SECONDS) -> bool:
    last = node_state.get("status_checked_at")
    return last is None or now - float(last) >= every


def describe_reply(kind: str, data: bytes) -> str:
    """A reply's content in a few words, for the packet log."""
    try:
        if kind == "status":
            return f"status, {int(parse_stats(data).get('battery_mv') or 0)} mV"
        mv = lpp_voltage_mv(data)
        temp = lpp_temperature(data)
        voltage = f", {mv} mV" if mv is not None else ", no voltage"
        return "telemetry" + voltage + (f", {temp} °C" if temp is not None else "")
    except (CompanionError, ValueError, IndexError):
        return f"{len(data)} bytes"


def describe_login(answer: sk.LoginResult | None) -> str:
    if answer is None:
        return ""
    return "logged in as ADMIN" if answer.is_admin else "logged in as guest"
