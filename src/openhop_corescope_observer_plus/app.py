"""The plugin's work loop: wires the parts together and runs one tick."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .claims import Claims
from .community import fast_mode
from .coordination import Coordinator
from .outbox import Outbox
from .page_requests import PageRequests
from .poll_cycle import PollCycle
from .poll_decisions import PollDecisions
from .polling_keys import PollingKeys
from .runtime_view import RuntimeView
from .self_check import SelfCheck
from .settings_intake import load_settings
from .station import Station

LOG = logging.getLogger("openhop.corescope_observer_plus")


class App:
    def __init__(self, data_dir: Path, *, companion_host: str = "127.0.0.1"):
        self.station = Station(data_dir, companion_host=companion_host)
        self.self_check = SelfCheck(self.station)
        self.coordinator = Coordinator(self.station)
        self.keys = PollingKeys(self.station)
        self.decisions = PollDecisions(self.station, self.coordinator)
        self.runtime = RuntimeView(self.station, self.coordinator, self.keys)
        self.cycle = PollCycle(
            self.station, self.coordinator, self.keys, self.decisions, self.runtime.write
        )
        self.outbox = Outbox(self.station)
        self.claims = Claims(self.station, self.coordinator, self.keys)
        self.requests = PageRequests(
            self.station,
            self.coordinator,
            self.self_check,
            self.cycle,
            self.outbox,
            self.runtime.write,
        )
        if fast_mode():
            LOG.warning(
                "*** TEST FAST MODE IS ON (OPENHOP_OBSERVER_PLUS_TEST_FAST=1): minute floors, "
                "30 s policy refresh, 60 s claims, 30 s retries. For bench runs only, never on a "
                "real mesh. ***"
            )

    def tick(self) -> None:
        station = self.station
        load_settings(station)
        if self.self_check.due():
            self.self_check.refresh()
        self.requests.run()
        now = time.time()
        self.coordinator.refresh_policy(now)
        settings = station.settings
        if settings.enabled and settings.targets:
            self.coordinator.owners(now)
            readings = self.cycle.poll(settings.targets)
            if readings:
                self.outbox.add(readings)
        if settings.enabled:
            self.claims.publish(time.time())
        self.outbox.flush()
        self.runtime.write()
