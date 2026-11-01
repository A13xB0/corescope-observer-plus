"""CoreScope's polling policy: its limits, clamped to what the plugin accepts, and its switches.

The switches are read from the saved copy, so they hold through a CoreScope outage.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from .models import KEY_RE, JsonDict
from .timeutil import iso_ts

LOG = logging.getLogger("openhop.corescope_observer_plus")

FAST_ENV = "OPENHOP_OBSERVER_PLUS_TEST_FAST"


def fast_mode() -> bool:
    """Bench runs only. Only the environment variable turns it on, never a setting."""
    return os.environ.get(FAST_ENV, "").strip() == "1"


@dataclass(frozen=True)
class Timings:
    refresh_s: float  # pull /policy and /owners this often
    policy_cache_s: float  # before a send, re-pull a policy older than this
    claims_every_s: float  # re-publish the claim this often (and on a route change)
    no_reply_retry_s: float  # the quick retry after an unanswered poll
    scheduled_gap_s: float  # at least this far between scheduled polls
    owners_stale_s: float  # forget CoreScope's owners after this long without an answer
    hold_recheck_s: float  # a standby holding through an outage looks again after this
    stale_policy_s: float  # an unreachable CoreScope's policy older than this: 2x windows


NORMAL = Timings(
    refresh_s=600,
    policy_cache_s=60,
    claims_every_s=1800,
    no_reply_retry_s=20 * 60,
    scheduled_gap_s=60,
    owners_stale_s=1800,
    hold_recheck_s=600,
    stale_policy_s=6 * 3600,
)
FAST = Timings(
    refresh_s=30,
    policy_cache_s=10,
    claims_every_s=60,
    no_reply_retry_s=30,
    scheduled_gap_s=5,
    owners_stale_s=90,
    hold_recheck_s=30,
    stale_policy_s=6 * 3600,
)


class Gate(TypedDict):
    """Why nothing at all may be sent right now."""

    reason: str
    text: str


def timings() -> Timings:
    return FAST if fast_mode() else NORMAL


DEFAULT_POLICY: JsonDict = {
    "version": "",
    "direct_s": 3600,
    "one_hop_s": 7200,
    "grace_s": 1800,
    "max_requests": 2,
    "takeover_after": 2,
    "paused": False,
    "paused_reason": "",
    "paused_until": "",
    "excluded_nodes": [],
    "node_windows": {},
    "min_plugin_version": "",
    "min_version_reason": "",
}

# What the plugin accepts from CoreScope, whatever it asks for.
LIMITS = {
    "direct_s": (30 * 60, 1440 * 60),
    "one_hop_s": (60 * 60, 1440 * 60),
    "grace_s": (10 * 60, 120 * 60),
    "max_requests": (1, 3),
    "takeover_after": (1, 6),
}
# Fast mode drops the time floors to a minute so a bench run can use
# few-minute windows.
FAST_LIMITS = {
    **LIMITS,
    "direct_s": (60, 1440 * 60),
    "one_hop_s": (60, 1440 * 60),
    "grace_s": (60, 120 * 60),
}


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def clamp(raw: JsonDict | None, fast: bool | None = None) -> tuple[JsonDict, list[JsonDict]]:
    """The policy made safe, and a note per clamped value ({field, asked, using})."""
    fast = fast_mode() if fast is None else fast
    limits = FAST_LIMITS if fast else LIMITS
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_POLICY)
    notes: list[JsonDict] = []
    for name, (lo, hi) in limits.items():
        asked = _int(raw.get(name), DEFAULT_POLICY[name]) if name in raw else DEFAULT_POLICY[name]
        using = max(lo, min(hi, asked))
        if using != asked:
            notes.append({"field": name, "asked": asked, "using": using})
        out[name] = using
    out["version"] = str(raw.get("version") or "")
    out["paused"] = raw.get("paused") is True
    out["paused_reason"] = str(raw.get("paused_reason") or "")
    out["paused_until"] = str(raw.get("paused_until") or "")
    out["excluded_nodes"] = sorted(
        {str(k).lower() for k in raw.get("excluded_nodes") or [] if KEY_RE.match(str(k).lower())}
    )
    # A repeater's own window is a window like any other: never below the
    # direct floor, never above a day.
    lo, hi = limits["direct_s"]
    windows = {}
    nw = raw.get("node_windows")
    for k, v in nw.items() if isinstance(nw, dict) else []:
        k = str(k).lower()
        if not KEY_RE.match(k):
            continue
        asked = _int(v, 0)
        if asked <= 0:
            continue
        using = max(lo, min(hi, asked))
        if using != asked:
            notes.append({"field": "node_windows", "node_id": k, "asked": asked, "using": using})
        windows[k] = using
    out["node_windows"] = windows
    out["min_plugin_version"] = str(raw.get("min_plugin_version") or "")
    out["min_version_reason"] = str(raw.get("min_version_reason") or "")
    # Only meaningful when CoreScope was told who and what is asking.
    if "version_ok" in raw:
        out["version_ok"] = raw.get("version_ok") is not False
    if "observer_blocked" in raw:
        out["observer_blocked"] = raw.get("observer_blocked") is True
    return out, notes


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def version_at_least(mine: str, minimum: str) -> bool:
    """mine >= minimum, numerically per component. No minimum: True. An
    unparsable own version counts as 0.0.0."""
    want = parse_version(minimum)
    if want is None:
        return True
    return (parse_version(mine) or (0, 0, 0)) >= want


class Community:
    """The saved policy (state.json "community") and what follows from it."""

    def __init__(self, store: JsonDict, plugin_version: str):
        self.s = store
        self.plugin_version = plugin_version

    # -- what was fetched ---------------------------------------------------

    def reset_for_url(self, url: str) -> None:
        """A policy belongs to the CoreScope that sent it."""
        if self.s.get("url") != url:
            self.s.clear()
            self.s["url"] = url

    def apply(self, raw: JsonDict, now: float) -> None:
        """Keep the raw copy; it is clamped on use."""
        before = self.gate(now)
        self.s["policy"] = raw
        self.s["fetched_at"] = now
        self.s["for_version"] = self.plugin_version
        self.s["last_error"] = ""
        self.s["last_status"] = 200
        after = self.gate(now)
        if (before and before["reason"]) != (after and after["reason"]):
            if after:
                LOG.warning("CoreScope policy: %s", after["text"])
            elif before:
                LOG.info("CoreScope policy: %s lifted, polling resumes", before["reason"])

    def note_failure(self, status: int, now: float) -> None:
        """The saved copy, and its switches, stay in force."""
        self.s["last_status"] = status
        self.s["last_error"] = (
            "this CoreScope has no polling policy (404)"
            if status == 404
            else f"HTTP {status}"
            if status
            else "CoreScope unreachable"
        )

    @property
    def fetched_at(self) -> float:
        return float(self.s.get("fetched_at") or 0)

    @property
    def last_error(self) -> str:
        return str(self.s.get("last_error") or "")

    @property
    def has_policy(self) -> bool:
        return isinstance(self.s.get("policy"), dict)

    def stale(self, now: float) -> bool:
        """CoreScope out of reach for hours: everyone polls at twice the window.

        Not for one answering 404 (it has no policy), nor before any policy was
        fetched (the defaults apply as they are).
        """
        return (
            self.has_policy
            and int(self.s.get("last_status") or 0) not in (200, 404)
            and now - self.fetched_at > timings().stale_policy_s
        )

    def policy(self) -> tuple[JsonDict, list[JsonDict]]:
        return clamp(self.s.get("policy") if self.has_policy else None)

    # -- the switches -----------------------------------------------------------

    def paused(self, now: float) -> bool:
        p, _ = self.policy()
        if not p["paused"]:
            return False
        until = iso_ts(p["paused_until"])
        return until is None or now < until

    def version_ok(self) -> bool:
        p, _ = self.policy()
        # CoreScope's answer is about the version that asked; after an upgrade
        # the saved copy is judged against the new version until re-fetched.
        if "version_ok" in p and self.s.get("for_version") == self.plugin_version:
            return bool(p["version_ok"])
        return version_at_least(self.plugin_version, p["min_plugin_version"])

    def blocked(self) -> bool:
        p, _ = self.policy()
        return bool(p.get("observer_blocked"))

    def gate(self, now: float) -> Gate | None:
        """Why nothing may be sent right now, Test and Check now included."""
        p, _ = self.policy()
        if self.blocked():
            return {"reason": "blocked", "text": "CoreScope has stopped this observer from polling"}
        if not self.version_ok():
            reason = f': "{p["min_version_reason"]}"' if p["min_version_reason"] else ""
            return {
                "reason": "version_disabled",
                "text": f"This version ({self.plugin_version}) is disabled by CoreScope. "
                f"Upgrade to {p['min_plugin_version']} or later{reason}",
            }
        if self.paused(now):
            until = f" until {p['paused_until']}" if p["paused_until"] else ""
            why = f": {p['paused_reason']}" if p["paused_reason"] else ""
            return {"reason": "paused", "text": f"Paused by CoreScope{until}{why}"}
        return None

    def excluded(self, node_id: str) -> bool:
        p, _ = self.policy()
        return node_id in p["excluded_nodes"]

    # -- windows ------------------------------------------------------------------

    def window_s(self, node_id: str, hops: int, now: float) -> int:
        """CoreScope's figure for this repeater, else by this observer's best route."""
        p, _ = self.policy()
        w = p["node_windows"].get(node_id) or (p["direct_s"] if hops == 0 else p["one_hop_s"])
        return int(w * 2 if self.stale(now) else w)

    def grace_s(self) -> int:
        return int(self.policy()[0]["grace_s"])

    def max_requests(self) -> int:
        return int(self.policy()[0]["max_requests"])

    def view(self, now: float) -> JsonDict:
        """For the plugin page."""
        p, notes = self.policy()
        g = self.gate(now)
        return {
            "has_policy": self.has_policy,
            "policy": p,
            "version": p["version"],
            "fetched_at": self.fetched_at or None,
            "last_error": self.s.get("last_error") or "",
            "stale": self.stale(now),
            "clamped": notes,
            "gate": g,
            "paused": self.paused(now),
            "blocked": self.blocked(),
            "version_ok": self.version_ok(),
            "fast_mode": fast_mode(),
        }
