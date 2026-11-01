"""What the plugin knows right now, shared by the parts that do the work."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from openhop_core import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder

from . import __version__, packet_log
from .community import Community, fast_mode
from .models import JsonDict, Neighbour, Route
from .openhop import OpenHopAPI
from .openhop_config import OpenHopConfig
from .schedule import spread_phases
from .settings import Settings
from .storage import Secrets, State


class CheckResult(TypedDict):
    ok: bool
    message: str


class Station:
    def __init__(self, data_dir: Path, *, companion_host: str = "127.0.0.1"):
        self.data_dir = data_dir
        self.companion_host = companion_host
        self.secrets = Secrets(data_dir)
        self.state = State(data_dir)
        self._seed_request_clock()
        self.settings = Settings()
        self.ohcfg: OpenHopConfig | None = None
        self.check: dict[str, CheckResult] = {}
        self.candidates: list[Neighbour] = []
        self.openhop_brokers: dict[str, JsonDict] = {}
        self.companion_key = ""
        self.actions: dict[str, JsonDict] = self.state.value.setdefault("actions", {})
        self.packets = packet_log.PacketLog(
            self.state.value.setdefault("packet_log", []),
            stats=self.state.value.setdefault("packet_stats", {}),
        )
        self.community = Community(self.state.value.setdefault("community", {}), __version__)

    def _seed_request_clock(self) -> None:
        # A repeater ignores a request whose timestamp isn't above the last it
        # accepted from that key, so timestamps must never go backwards, even
        # across a restart or after a clock that was briefly ahead.
        last = int(self.state.value.get("last_tag") or 0)
        if last > PacketBuilder._last_unique_timestamp:
            PacketBuilder._last_unique_timestamp = last

    def keep_request_clock(self) -> None:
        """Save the newest request timestamp for the next start."""
        self.state.value["last_tag"] = max(
            int(self.state.value.get("last_tag") or 0), PacketBuilder._last_unique_timestamp
        )

    def check_ok(self, name: str) -> bool:
        entry = self.check.get(name)
        return bool(entry and entry["ok"])

    def api(self) -> OpenHopAPI:
        return OpenHopAPI(self.settings.openhop_api_url, self.secrets.api_token)

    @property
    def observer_id(self) -> str:
        if not self.ohcfg:
            return ""
        return str(LocalIdentity(self.ohcfg.identity_key).get_public_key().hex())

    def neighbour(self, node_id: str) -> Neighbour | None:
        return next((n for n in self.candidates if n.node_id == node_id), None)

    def routes_for(self, n: Neighbour) -> list[Route]:
        """The routes polling may use for n under the current reach setting."""
        return [r for r in n.routes if r.hops <= self.settings.max_hops]

    def best_hops(self, node_id: str) -> int:
        n = self.neighbour(node_id)
        routes = self.routes_for(n) if n else []
        return routes[0].hops if routes else 0

    def window_s(self, node_id: str, now: float) -> int:
        return self.community.window_s(node_id, self.best_hops(node_id), now)

    def interval_s(self, node_id: str, now: float) -> float:
        """Seconds between scheduled polls: never faster than CoreScope's window."""
        window = self.window_s(node_id, now)
        if fast_mode():
            return float(window)
        return float(max(self.settings.interval_for(node_id) * 60, window))

    def phases(self, now: float) -> dict[str, float]:
        return spread_phases(self.settings.targets, lambda k: self.interval_s(k, now))
