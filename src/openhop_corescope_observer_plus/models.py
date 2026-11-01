"""Plain types shared across the plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, TypedDict

JsonDict = dict[str, Any]
# One repeater's entry under "nodes" in state.json.
NodeState = dict[str, Any]

KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class OpenHopError(Exception):
    """openHop (or CoreScope) could not give the plugin what it asked for."""


class ManualRepeater(TypedDict):
    """A repeater the operator added by key because no advert of it was heard."""

    node_id: str
    name: str


@dataclass
class Route:
    """One way to reach a repeater: direct, or through one relay heard direct."""

    relay_id: str = ""  # lowercase 64-hex of the relay; "" for direct
    relay_name: str = ""
    hash_size: int = 1  # bytes per hop hash on this route
    adverts: int = 0  # the repeater's adverts heard this way in the window
    snr: float | None = None  # their average SNR

    @property
    def hops(self) -> int:
        return 1 if self.relay_id else 0

    @property
    def key(self) -> str:
        return self.relay_id or "direct"

    @property
    def path(self) -> bytes:
        return bytes.fromhex(self.relay_id)[: self.hash_size] if self.relay_id else b""

    @property
    def label(self) -> str:
        return f"via {self.relay_name or self.relay_id[:8]}" if self.relay_id else "direct"

    def as_dict(self) -> JsonDict:
        return {
            "key": self.key,
            "hops": self.hops,
            "relay_id": self.relay_id,
            "relay_name": self.relay_name,
            "label": self.label,
            "adverts": self.adverts,
            "snr": None if self.snr is None else round(self.snr, 1),
        }


@dataclass
class Neighbour:
    node_id: str  # lowercase 64-hex
    name: str
    snr: float | None
    rssi: int | None
    last_seen: float
    zero_hop: bool
    routes: list[Route] = field(default_factory=list)  # best first
    manual: bool = False  # added by the operator, not found from an advert

    def __post_init__(self) -> None:
        if self.zero_hop and not self.routes:
            self.routes = [Route(snr=self.snr)]

    @property
    def hops(self) -> int | None:
        """Fewest hops of any known route; None when it was only heard further away."""
        return min((r.hops for r in self.routes), default=0 if self.zero_hop else None)
