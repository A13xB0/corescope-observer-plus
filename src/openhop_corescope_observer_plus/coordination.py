"""Working with the other observers through CoreScope: policy, owners, roles and startup."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TypedDict

from . import __version__, corescope
from .community import timings
from .models import JsonDict
from .station import Station

LOG = logging.getLogger("openhop.corescope_observer_plus")


class Role(TypedDict, total=False):
    """This observer's part for a repeater: poller, standby, excluded, paused or disabled."""

    role: str
    independent: bool
    by: str
    owner_hops: int | None
    rank: int


@dataclass
class OwnersCache:
    at: float = 0.0  # last asked
    ok_at: float = 0.0  # last answered
    nodes: dict[str, JsonDict] = field(default_factory=dict)


class Coordinator:
    def __init__(self, station: Station):
        self.station = station
        self.owners_cache = OwnersCache()
        self.started_at = time.time()
        self.claims_sent_at = 0.0  # when this run last published a claim
        self.gated_at = 0.0  # when CoreScope last had this observer stopped
        self.policy_tried = 0.0

    def owners(self, now: float) -> dict[str, JsonDict]:
        """CoreScope's choice of poller per repeater, refreshed every 10 min.

        Empty (poll everything ourselves) when it hasn't answered for 30 min or
        community polling is off. The answer carries the policy too.
        """
        station = self.station
        settings = station.settings
        if not settings.community:
            return {}
        cache = self.owners_cache
        t = timings()
        if station.ohcfg and settings.corescope_url and now - cache.at >= t.refresh_s:
            cache.at = now
            body = corescope.owners(
                settings.corescope_url, station.observer_id, plugin_version=__version__
            )
            if body is not None:
                cache.nodes, cache.ok_at = body.get("nodes") or {}, now
                if isinstance(body.get("policy"), dict):
                    station.community.apply(body["policy"], now)
                    station.state.save()
            elif now - cache.ok_at > t.owners_stale_s:
                cache.nodes = {}
        return cache.nodes

    def refresh_policy(self, now: float, max_age: float | None = None) -> None:
        """Pull the policy if the saved copy is older than max_age.

        A failure keeps the saved copy and its switches, and is retried no
        sooner than max_age.
        """
        station = self.station
        url = station.settings.corescope_url
        if not url:
            return
        max_age = timings().refresh_s if max_age is None else max_age
        community = station.community
        if now - max(self.policy_tried, community.fetched_at) < max_age:
            return
        self.policy_tried = now
        status, body = corescope.policy(url, station.observer_id, __version__)
        if status == 200 and isinstance(body, dict):
            community.apply(body, now)
        else:
            community.note_failure(status, now)
            if status != 404:
                kept = "saved policy" if community.has_policy else "built-in defaults"
                LOG.warning("CoreScope policy: %s; keeping the %s", community.last_error, kept)
        station.state.save()

    def role_for(self, node_id: str, now: float, *, fetch: bool = True) -> Role:
        """Community off, or nothing from CoreScope about it: poller."""
        if not self.station.settings.community:
            return {"role": "poller", "independent": True}
        nodes = self.owners(now) if fetch else self.owners_cache.nodes
        owner = nodes.get(node_id)
        if not isinstance(owner, dict):
            return {"role": "poller"}
        role = owner.get("role")
        if role == "excluded" or owner.get("excluded"):
            return {"role": "excluded"}
        if role == "paused":
            return {"role": "paused"}
        if role == "disabled":
            return {"role": "disabled"}
        if role in ("polling", "independent") or (role is None and owner.get("mine")):
            return {"role": "poller", "independent": role == "independent"}
        if not owner.get("mine"):
            return {
                "role": "standby",
                "by": str(owner.get("owner_name") or str(owner.get("owner") or "")[:8]),
                "owner_hops": owner.get("owner_hops"),
                "rank": max(1, int(owner.get("standby_rank") or 1)),
            }
        return {"role": "poller"}

    def standby_hold_from(self) -> float:
        return max(self.started_at, self.gated_at)

    def startup_hold_s(self) -> float:
        return 2 * timings().refresh_s + 60

    def starting(self, now: float) -> bool:
        """Whether scheduled polls still wait after a start.

        Until CoreScope holds this observer's claim and has said who polls what
        since, or the hold runs out (CoreScope unreachable). Community only.
        """
        settings = self.station.settings
        if not settings.community or not settings.corescope_url:
            return False
        if now - self.started_at >= self.startup_hold_s():
            return False
        claims_at = float(self.station.state.value.get("claims_at") or 0)
        claimed = self.claims_sent_at > 0 or now - claims_at < timings().claims_every_s
        heard = self.owners_cache.ok_at > max(self.started_at, self.claims_sent_at)
        return not (claimed and heard)

    def note_claim_sent(self, now: float) -> None:
        self.claims_sent_at = now
        self.owners_cache.at = 0.0  # ask who polls what again now CoreScope has the claim
