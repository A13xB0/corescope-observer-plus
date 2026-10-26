"""The last packets the plugin sent, each with what came back, for debugging.

One entry per packet put on the air (a login, a route, a status or telemetry
request, an admin removal), newest last, kept to SIZE. Each says which
repeater, by which route, under which key, and what answered it: heard on
this node's radio or found in CoreScope's observers' packets, flooded or
direct, how long after, and what it said. A route packet expects no answer.

Entries live in state.json (so they survive a restart) and are shown on the
plugin page.
"""

from __future__ import annotations

import time

SIZE = 20

WAITING = "waiting"
ANSWERED = "answered"
NO_ANSWER = "no answer"
REFUSED = "refused"
NONE_EXPECTED = "none expected"


class PacketLog:
    def __init__(self, entries: list, size: int = SIZE):
        self.entries = entries
        self.size = size
        del self.entries[:-size]

    def for_node(self, node_id: str, name: str, key: str, node_entries: list | None = None) -> "NodeLog":
        """``node_entries``: the repeater's own list (in its state), which
        keeps its last SIZE packets however busy the others are."""
        return NodeLog(self, node_id, name, key, node_entries)


class NodeLog:
    """The log as seen by one poll of one repeater."""

    def __init__(self, log: PacketLog | None, node_id: str = "", name: str = "", key: str = "",
                 node_entries: list | None = None):
        self._log = log
        self._mine = node_entries
        self._node = {"node_id": node_id, "name": name, "key": key}

    def sent(self, kind: str, route: str, detail: str = "") -> dict:
        entry = {"at": time.time(), **self._node, "kind": kind, "route": route, "detail": detail,
                 "reply": WAITING}
        if self._log is not None:
            self._log.entries.append(entry)
            del self._log.entries[:-self._log.size]
        if self._mine is not None:
            # The same dict, so answers recorded later show in both lists.
            self._mine.append(entry)
            del self._mine[:-SIZE]
        return entry

    @staticmethod
    def unconfirmed(entry: dict) -> None:
        # openHop didn't confirm the send in time (busy); it usually still went.
        entry["unconfirmed"] = True

    @staticmethod
    def answered(entry: dict, via: str, *, flooded: bool | None = None, after_s: float | None = None,
                 what: str = "") -> None:
        entry.update(reply=ANSWERED, via=via, flooded=flooded, what=what,
                     after_s=round(after_s, 1) if after_s is not None else None)

    @staticmethod
    def unanswered(entry: dict, what: str = "") -> None:
        if entry.get("reply") == WAITING:
            entry.update(reply=NO_ANSWER, what=what)

    @staticmethod
    def refused(entry: dict, what: str = "") -> None:
        entry.update(reply=REFUSED, what=what)

    @staticmethod
    def none_expected(entry: dict) -> None:
        entry["reply"] = NONE_EXPECTED


NULL = NodeLog(None)
