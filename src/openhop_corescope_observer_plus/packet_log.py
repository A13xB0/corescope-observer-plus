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
STATS_DAYS = 8  # a week back from today

WAITING = "waiting"
ANSWERED = "answered"
NO_ANSWER = "no answer"
REFUSED = "refused"
NONE_EXPECTED = "none expected"


def _day(at: float) -> str:
    """The station's own calendar day, so "today" means what its operator expects."""
    return time.strftime("%Y-%m-%d", time.localtime(at))


class PacketLog:
    def __init__(self, entries: list, size: int = SIZE, stats: dict | None = None):
        self.entries = entries
        self.size = size
        del self.entries[:-size]
        # Counts per day, in state.json. The log itself only keeps 20 packets.
        self.stats = stats if stats is not None else {}

    def count(self, entry: dict, outcome: str) -> None:
        """Count a packet's outcome once. A reply found late (in CoreScope's
        packets) moves it from no answer to answered, on the day it was sent."""
        day = self.stats.setdefault(_day(entry["at"]), {})
        was = entry.get("counted")
        if was == outcome:
            return
        if was:
            day[was] = max(0, int(day.get(was) or 0) - 1)
        day[outcome] = int(day.get(outcome) or 0) + 1
        entry["counted"] = outcome
        if outcome == ANSWERED and entry.get("flooded"):
            day["flooded"] = int(day.get("flooded") or 0) + 1
            by = day.setdefault("flooded_by", {})
            n = by.setdefault(entry.get("node_id") or "", {"name": entry.get("name") or "", "count": 0})
            n["count"] += 1
        for old in sorted(self.stats)[:-STATS_DAYS]:
            del self.stats[old]

    def summary(self, now: float | None = None) -> dict:
        """Today, yesterday and the last week, for the Activity page."""
        now = time.time() if now is None else now
        def day(at: float) -> dict:
            d = self.stats.get(_day(at)) or {}
            answered, none, refused = (int(d.get(k) or 0) for k in (ANSWERED, NO_ANSWER, REFUSED))
            return {"day": _day(at), "answered": answered, "no_answer": none, "refused": refused,
                    "flooded": int(d.get("flooded") or 0),
                    "flooded_by": sorted(((d.get("flooded_by") or {}).values()), key=lambda n: -n["count"])}
        week = [day(now - i * 86400) for i in range(7)]
        return {"today": week[0], "yesterday": week[1], "week": {
            k: sum(d[k] for d in week) for k in ("answered", "no_answer", "refused", "flooded")}}

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

    def _count(self, entry: dict, outcome: str) -> None:
        if self._log is not None:
            self._log.count(entry, outcome)

    def answered(self, entry: dict, via: str, *, flooded: bool | None = None, after_s: float | None = None,
                 what: str = "") -> None:
        entry.update(reply=ANSWERED, via=via, flooded=flooded, what=what,
                     after_s=round(after_s, 1) if after_s is not None else None)
        self._count(entry, ANSWERED)

    def unanswered(self, entry: dict, what: str = "") -> None:
        if entry.get("reply") == WAITING:
            entry.update(reply=NO_ANSWER, what=what)
            self._count(entry, NO_ANSWER)

    def refused(self, entry: dict, what: str = "") -> None:
        entry.update(reply=REFUSED, what=what)
        self._count(entry, REFUSED)

    @staticmethod
    def none_expected(entry: dict) -> None:
        entry["reply"] = NONE_EXPECTED


NULL = NodeLog(None)
