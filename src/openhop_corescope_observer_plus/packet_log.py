"""The last packets the plugin sent and what came back, for the plugin page."""

from __future__ import annotations

import time

from .models import JsonDict

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
    def __init__(self, entries: list[JsonDict], size: int = SIZE, stats: JsonDict | None = None):
        self.entries = entries
        self.size = size
        del self.entries[:-size]
        # Counts per day, in state.json. The log itself only keeps 20 packets.
        self.stats = stats if stats is not None else {}

    def count(self, entry: JsonDict, outcome: str) -> None:
        """Count a packet's outcome once, on the day it was sent."""
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
            n = by.setdefault(
                entry.get("node_id") or "", {"name": entry.get("name") or "", "count": 0}
            )
            n["count"] += 1
        for old in sorted(self.stats)[:-STATS_DAYS]:
            del self.stats[old]

    def _day_summary(self, at: float) -> JsonDict:
        d = self.stats.get(_day(at)) or {}
        return {
            "day": _day(at),
            "answered": int(d.get(ANSWERED) or 0),
            "no_answer": int(d.get(NO_ANSWER) or 0),
            "refused": int(d.get(REFUSED) or 0),
            "flooded": int(d.get("flooded") or 0),
            "flooded_by": sorted(((d.get("flooded_by") or {}).values()), key=lambda n: -n["count"]),
        }

    def summary(self, now: float | None = None) -> JsonDict:
        """Today, yesterday and the last week, for the Activity page."""
        now = time.time() if now is None else now
        week = [self._day_summary(now - i * 86400) for i in range(7)]
        totals = ("answered", "no_answer", "refused", "flooded")
        return {
            "today": week[0],
            "yesterday": week[1],
            "week": {k: sum(d[k] for d in week) for k in totals},
        }

    def for_node(
        self, node_id: str, name: str, key: str, node_entries: list[JsonDict] | None = None
    ) -> NodeLog:
        """``node_entries`` is the repeater's own list, which keeps its last SIZE
        packets however busy the others are."""
        return NodeLog(self, node_id, name, key, node_entries)


class NodeLog:
    """The log as seen by one poll of one repeater."""

    def __init__(
        self,
        log: PacketLog | None,
        node_id: str = "",
        name: str = "",
        key: str = "",
        node_entries: list[JsonDict] | None = None,
    ):
        self._log = log
        self._mine = node_entries
        self._node = {"node_id": node_id, "name": name, "key": key}

    def sent(self, kind: str, route: str, detail: str = "") -> JsonDict:
        entry = {
            "at": time.time(),
            **self._node,
            "kind": kind,
            "route": route,
            "detail": detail,
            "reply": WAITING,
        }
        if self._log is not None:
            self._log.entries.append(entry)
            del self._log.entries[: -self._log.size]
        if self._mine is not None:
            # The same dict, so answers recorded later show in both lists.
            self._mine.append(entry)
            del self._mine[:-SIZE]
        return entry

    @staticmethod
    def unconfirmed(entry: JsonDict) -> None:
        # openHop didn't confirm the send in time (busy); it usually still went.
        entry["unconfirmed"] = True

    def _count(self, entry: JsonDict, outcome: str) -> None:
        if self._log is not None:
            self._log.count(entry, outcome)

    def answered(
        self,
        entry: JsonDict,
        via: str,
        *,
        flooded: bool | None = None,
        after_s: float | None = None,
        what: str = "",
    ) -> None:
        entry.update(
            reply=ANSWERED,
            via=via,
            flooded=flooded,
            what=what,
            after_s=round(after_s, 1) if after_s is not None else None,
        )
        self._count(entry, ANSWERED)

    def unanswered(self, entry: JsonDict, what: str = "") -> None:
        if entry.get("reply") == WAITING:
            entry.update(reply=NO_ANSWER, what=what)
            self._count(entry, NO_ANSWER)

    def refused(self, entry: JsonDict, what: str = "") -> None:
        entry.update(reply=REFUSED, what=what)
        self._count(entry, REFUSED)

    @staticmethod
    def none_expected(entry: JsonDict) -> None:
        entry["reply"] = NONE_EXPECTED


NULL = NodeLog(None)
