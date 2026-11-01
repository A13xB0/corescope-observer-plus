"""Telling CoreScope which repeaters this observer polls, by what route and key."""

from __future__ import annotations

import logging

from . import publisher
from .community import timings
from .coordination import Coordinator
from .models import Claim
from .polling_keys import PollingKeys
from .station import Station

LOG = logging.getLogger("openhop.corescope_observer_plus")

RETRY_S = 60.0


class Claims:
    def __init__(self, station: Station, coordinator: Coordinator, keys: PollingKeys):
        self.station = station
        self.coordinator = coordinator
        self.keys = keys
        self.tried_at = 0.0

    def entries(self) -> list[Claim]:
        """One claim per target that is reachable and not excluded, whatever its key,
        so CoreScope can choose one poller per repeater."""
        station = self.station
        out: list[Claim] = []
        for node_id in station.settings.targets:
            n = station.neighbour(node_id)
            routes = station.routes_for(n) if n else []
            if not n or not routes or station.community.excluded(node_id):
                continue
            route = routes[0]
            if self.keys.uses_shared_key(node_id):
                key = "shared"
            else:
                key = "password" if station.secrets.has_password(node_id) else "own"
            snr = route.snr if route.snr is not None else n.snr
            out.append(
                {
                    "node_id": node_id,
                    "reply_hops": route.hops,
                    "via": route.relay_id or None,
                    "key": key,
                    "snr": None if snr is None else round(float(snr), 1),
                }
            )
        return out

    def publish(self, now: float) -> None:
        """Sent every 30 min and at once when a route's hops or relay change.

        With community polling off: one last claim saying so, then none.
        """
        station = self.station
        if not station.ohcfg or not station.check_ok("publish"):
            return
        t = timings()
        saved = station.state.value
        community = station.settings.community
        if not community and not saved.get("claims_community"):
            return  # off, and CoreScope already knows (or never heard from us)
        claims = self.entries()
        if not claims:
            return
        sig = sorted([c["node_id"], c["reply_hops"], c["via"] or ""] for c in claims)
        changed = sig != saved.get("claims_sig") or community != saved.get("claims_community")
        if not changed and now - float(saved.get("claims_at") or 0) < t.claims_every_s:
            return
        if now - self.tried_at < min(RETRY_S, t.claims_every_s):
            return
        self.tried_at = now
        publish = publisher.Publisher(station.ohcfg)
        brokers = {b.name: b for b in station.ohcfg.brokers}
        sent = False
        for name in station.settings.brokers:
            broker = brokers.get(name)
            if broker is None:
                continue
            try:
                publish.publish_claim(
                    broker,
                    claims,
                    policy_version=station.community.policy()[0]["version"],
                    community=community,
                )
                sent = True
            except Exception as exc:  # noqa: BLE001 - retried a minute later
                LOG.warning("polling claim to %s failed: %s", name, exc)
        if sent:
            saved.update(
                claims_at=now,
                claims_sig=sig,
                claims_community=community,
                claims_count=len(claims),
            )
            self.coordinator.note_claim_sent(now)
            station.state.save()
