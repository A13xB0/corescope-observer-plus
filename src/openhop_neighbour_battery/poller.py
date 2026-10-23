"""One poll of one neighbour, and the rules for when to poll it next.

Requests are only ever sent direct with an empty path, to repeaters openHop
heard at zero hops. If the path can't be pinned to direct, nothing is sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .companion import CompanionClient, CompanionError, FloodSent
from .openhop import Neighbour

OK = "ok"
NO_BATTERY = "no_battery"
NEEDS_PASSWORD = "needs_password"
PASSWORD_REJECTED = "password_rejected"
NO_REPLY = "no_reply"
NOT_DIRECT = "not_direct"
ERROR = "error"

# A logged-in client stays in the repeater's ACL; ask for status first and
# only log in again if that gets no answer.
LOGIN_REUSE_SECONDS = 6 * 3600
NO_BATTERY_RECHECK = 24 * 3600
NO_REPLY_BACKOFF = 6 * 3600


@dataclass
class PollResult:
    code: str
    battery_mv: int | None = None
    role: str = ""
    detail: str = ""


def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll_one(client: CompanionClient, n: Neighbour, password: str, node_state: dict,
             *, now: float | None = None) -> PollResult:
    """Pin the path to direct, log in if needed, and read the battery."""
    now = now or time.time()
    key = bytes.fromhex(n.node_id)
    try:
        if not client.pin_direct(key, n.name):
            return PollResult(NOT_DIRECT, detail="the companion would not keep the path direct")

        mv = None
        if now - float(node_state.get("logged_in_at") or 0) < LOGIN_REUSE_SECONDS:
            mv = client.battery_mv(key)
        role = node_state.get("role", "")
        if mv is None:
            login = client.login(key, password)
            if not login.ok:
                if login.timed_out:
                    return PollResult(NO_REPLY, detail="no answer to the login")
                return PollResult(PASSWORD_REJECTED if password else NEEDS_PASSWORD)
            node_state["logged_in_at"] = now
            role = login.role
            mv = client.battery_mv(key)
        if mv is None:
            return PollResult(NO_REPLY, role=role, detail="no answer to the status request")
        return PollResult(OK if mv > 0 else NO_BATTERY, battery_mv=mv, role=role)
    except FloodSent as exc:
        return PollResult(NOT_DIRECT, detail=str(exc))
    except (CompanionError, OSError) as exc:
        return PollResult(ERROR, detail=str(exc))


def record(node_state: dict, result: PollResult, password_set: bool, now: float) -> None:
    node_state["last_attempt"] = now
    node_state["code"] = result.code
    node_state["detail"] = result.detail
    if result.role:
        node_state["role"] = result.role
    if result.code in (OK, NO_BATTERY):
        node_state["last_success"] = now
        node_state["battery_mv"] = result.battery_mv
        node_state["no_reply_count"] = 0
    elif result.code == NO_REPLY:
        node_state["no_reply_count"] = int(node_state.get("no_reply_count") or 0) + 1
        node_state.pop("logged_in_at", None)
    if result.code in (NEEDS_PASSWORD, PASSWORD_REJECTED):
        node_state.pop("logged_in_at", None)
        # Don't retry the same credential until the operator changes it.
        node_state["blocked_password_set"] = password_set


def is_due(node_state: dict, interval_s: float, password_set: bool, now: float) -> bool:
    code = node_state.get("code")
    last = float(node_state.get("last_attempt") or 0)
    if code in (NEEDS_PASSWORD, PASSWORD_REJECTED):
        # Retry only once the credential changed (set, replaced or removed).
        if node_state.get("blocked_password_set") == password_set and not node_state.get("password_changed"):
            return False
    if code == NO_BATTERY:
        return now - last >= max(interval_s, NO_BATTERY_RECHECK)
    if code == NO_REPLY and int(node_state.get("no_reply_count") or 0) >= 3:
        return now - last >= max(interval_s, NO_REPLY_BACKOFF)
    return now - last >= interval_s


def reading(n: Neighbour, result: PollResult, sampled_at: float) -> dict:
    """One payload reading (the CoreScope /battery contract, v1)."""
    out = {
        "node_id": n.node_id,
        "battery_mv": int(result.battery_mv or 0),
        "source": "poll",
        "reply_hops": 0,
        "method": "status",
        "sampled_at": utc_iso(sampled_at),
    }
    if n.snr is not None:
        out["snr"] = round(n.snr, 2)
    if n.rssi is not None:
        out["rssi"] = n.rssi
    return out
