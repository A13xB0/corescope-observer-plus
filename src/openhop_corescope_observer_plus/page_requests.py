"""What the plugin page asks for: connect, Test, Check now, lookups.

The plugin is single-threaded, so a long request writes runtime.json itself as
it goes; that is the only way the page sees progress.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import __version__, corescope
from .community import timings
from .coordination import Coordinator
from .models import JsonDict, Neighbour, NodeState
from .outbox import Outbox
from .poll_cycle import PollCycle
from .poll_decisions import Skip
from .poll_result import NO_BATTERY, OK
from .self_check import SelfCheck
from .station import Station
from .timeutil import iso_ts, utc_iso

# A forced Test of one repeater within this long is refused: each can put logins
# on air, and a burst of them only adds load.
TEST_COOLDOWN_S = 60
# Lets a just-published own-key reading be ingested before confirming against CoreScope.
CORESCOPE_SETTLE_S = 2.0
# CoreScope stamps a reading with the request time the repeater echoes.
CORESCOPE_CLOCK_SLACK_S = 5


def _fresh(ts: str | None, since: float) -> bool:
    """Whether a CoreScope timestamp is from after ``since``, with clock slack."""
    return bool(ts) and (iso_ts(ts) or 0.0) >= since - CORESCOPE_CLOCK_SLACK_S


class PageRequests:
    def __init__(
        self,
        station: Station,
        coordinator: Coordinator,
        self_check: SelfCheck,
        cycle: PollCycle,
        outbox: Outbox,
        on_progress: Callable[[], None],
    ):
        self.station = station
        self.coordinator = coordinator
        self.self_check = self_check
        self.cycle = cycle
        self.outbox = outbox
        self.on_progress = on_progress

    def run(self) -> None:
        state = self.station.state
        pending = state.value.get("pending") or []
        state.value["pending"] = []
        for req in pending:
            rid, kind = str(req.get("id")), req.get("type")
            result: JsonDict = {"id": rid, "type": kind, "at": utc_iso(), "ok": True, "message": ""}
            try:
                self._handle(kind, req, result)
            except Exception as exc:  # noqa: BLE001 - shown to the operator on the page
                result["ok"] = False
                result["message"] = str(exc)
            finally:
                result["running"] = False  # never leave the page waiting on a failed request
            self.station.actions[kind or "unknown"] = result
        if pending:
            state.save()

    def _handle(self, kind: str | None, req: JsonDict, result: JsonDict) -> None:
        if kind == "connect":
            result["message"] = self.self_check.connect()
        elif kind in ("test_login", "poll_now"):
            self._run_test(kind, req, result)
        elif kind == "check_now":
            self._run_check(req, result)
        elif kind == "lookup":
            self._run_lookup(req, result)
        elif kind == "monitors":
            self._run_monitors(req, result)
        elif kind == "refresh":
            self.self_check.refresh()
        else:
            raise RuntimeError(f"unknown request {kind!r}")

    def _show(self, kind: str, result: JsonDict) -> None:
        self.station.actions[kind] = result
        self.on_progress()

    def _confirm_wanted(self) -> bool:
        settings = self.station.settings
        return bool(settings.check_corescope and settings.corescope_url)

    def _corescope_confirm(self, node_id: str, since: float) -> JsonDict:
        """CoreScope's view for the live "did it land?" line; always a dict, so
        the page can show "checked, nothing yet"."""
        snap = corescope.battery_snapshot(self.station.settings.corescope_url, node_id)
        if not snap:
            return {"reachable": False}
        snap["reachable"] = True
        snap["fresh"] = _fresh(snap.get("latest_ts"), since)
        return snap

    def _node_view(self, node_id: str, name: str = "") -> JsonDict:
        """One repeater's latest outcome, as the live views show it."""
        st = self.station.state.node(node_id)
        return {
            "node_id": node_id,
            "name": st.get("name") or name,
            "code": st.get("code"),
            "covered": bool(st.get("last_covered")),
            "via_corescope": bool(st.get("via_corescope")),
            "flood_replies": int(st.get("flood_replies") or 0),
            "role": st.get("role", ""),
            "battery_mv": st.get("battery_mv"),
            "route": st.get("last_route_label") or "",
            "temperature_c": st.get("temperature_c") if st.get("temp_code") == "ok" else None,
            "message": st.get("detail") or st.get("code") or "",
        }

    def _queue_and_send(self, readings: list[JsonDict]) -> None:
        if readings:
            self.outbox.add(readings)
            self.outbox.flush()

    def _run_test(self, kind: str, req: JsonDict, result: JsonDict) -> None:
        """Test one repeater now, streaming each step to the dialog."""
        station = self.station
        node_id = str(req.get("node_id") or "").lower()
        started = time.time()
        last = float(station.state.node(node_id).get("last_attempt") or 0)
        if started - last < TEST_COOLDOWN_S:
            wait = int(TEST_COOLDOWN_S - (started - last)) + 1
            result.update(
                self._node_view(node_id),
                running=False,
                ok=False,
                message=f"Polled {int(started - last)} s ago. "
                f"Wait {wait} s before testing it again.",
            )
            return
        # CoreScope's switches and exclusions stop a Test too (its limits don't).
        self.coordinator.refresh_policy(started, timings().policy_cache_s)
        stop = station.community.gate(started)
        if stop is None and station.community.excluded(node_id):
            stop = {"reason": "excluded", "text": "Excluded by CoreScope"}
        if stop:
            result.update(
                self._node_view(node_id),
                running=False,
                ok=False,
                skipped=stop["reason"],
                message=stop["text"],
            )
            return
        self.self_check.refresh()
        # Say "running" before the poll, which can take up to a minute.
        result["running"] = True
        result["steps"] = []
        self._show(kind, result)

        def on_step(label: str) -> None:
            result["steps"].append({"label": label, "at": utc_iso()})
            self._show(kind, result)

        readings = self.cycle.poll([node_id], mode="test", on_step=on_step)
        result.update(self._node_view(node_id))
        if kind == "poll_now":
            self._queue_and_send(readings)
        # For a guest-key repeater this is the real "did it land?": the reading
        # is never published. Skipped when nothing was sent.
        if self._confirm_wanted() and result["code"] not in ("not_direct", "not_heard"):
            time.sleep(CORESCOPE_SETTLE_S)
            result["corescope"] = self._corescope_confirm(node_id, started)

    def _run_check(self, req: JsonDict, result: JsonDict) -> None:
        """Poll the selected repeaters now, streaming each result to the page.

        "stale" is like the schedule. "all" ignores fresh readings and standby,
        but not CoreScope's switches, exclusions or request cap.
        """
        station = self.station
        started = time.time()
        self.self_check.refresh()
        scope = "stale" if req.get("scope") == "stale" else "all"
        targets = list(station.settings.targets)
        self.coordinator.refresh_policy(started, timings().policy_cache_s)
        gate = station.community.gate(started)
        if gate:
            result.update(
                scope=scope,
                total=len(targets),
                progress=[],
                running=False,
                ok=False,
                skipped=gate["reason"],
                message=gate["text"],
            )
            return
        result.update(scope=scope, total=len(targets), progress=[], running=True)
        self._show("check_now", result)

        def on_node(n: Neighbour, _st: NodeState) -> None:
            result["progress"].append(self._node_view(n.node_id, n.name))
            self._show("check_now", result)

        def on_skip(n: Neighbour, skip: Skip) -> None:
            row = self._node_view(n.node_id, n.name)
            row.update(skipped=skip["reason"], message=skip["text"])
            result["progress"].append(row)
            self._show("check_now", result)

        self._queue_and_send(self.cycle.poll(targets, mode=scope, on_node=on_node, on_skip=on_skip))
        asked = [row for row in result["progress"] if not row.get("skipped")]
        skipped = len(result["progress"]) - len(asked)
        if self._confirm_wanted() and asked:
            self._confirm_each(asked, started, result)
        self._summarise_check(result, targets, asked, skipped)

    def _confirm_each(self, asked: list[JsonDict], started: float, result: JsonDict) -> None:
        """Stops at the first unreachable answer: the rest would fail the same way."""
        time.sleep(CORESCOPE_SETTLE_S)
        for row in asked:
            row["corescope"] = self._corescope_confirm(row["node_id"], started)
            self._show("check_now", result)
            if not row["corescope"].get("reachable"):
                break

    def _summarise_check(
        self, result: JsonDict, targets: list[str], asked: list[JsonDict], skipped: int
    ) -> None:
        # Counted from state: guest-key readings are read but never published.
        state = self.station.state
        codes = [state.node(row["node_id"]).get("code") or "?" for row in asked]
        answered = sum(c in (OK, NO_BATTERY) for c in codes)
        result["readings"] = answered
        result["asked"], result["skipped_count"] = len(asked), skipped
        if not targets:
            result["message"] = "no repeaters selected"
            return
        if not asked:
            result["message"] = (
                f"nothing to ask: {skipped} skipped"
                if skipped
                else "no selected repeater is reachable"
            )
            return
        rest = sorted({c for c in codes if c not in (OK, NO_BATTERY)})
        result["message"] = (
            f"{answered} of {len(asked)} read"
            + (f" ({', '.join(rest)} for the rest)" if rest else "")
            + (f" · {skipped} skipped" if skipped else "")
        )

    def _run_lookup(self, req: JsonDict, result: JsonDict) -> None:
        query = str(req.get("query") or "").strip()
        if len(query) < 3:
            raise RuntimeError("type at least 3 characters")
        result["results"] = corescope.search_nodes(self.station.settings.corescope_url, query)
        result["message"] = f"{len(result['results'])} found in CoreScope"

    def _run_monitors(self, req: JsonDict, result: JsonDict) -> None:
        """Who else monitors a repeater, for the Details dialog."""
        url = self.station.settings.corescope_url
        node_id = str(req.get("node_id") or "").lower()
        result["node_id"] = node_id
        if not url:
            result.update(ok=False, message="No CoreScope address set")
            return
        status, body = corescope.monitors(url, node_id, __version__)
        if status == 200 and isinstance(body, dict):
            result["monitors"] = body
            return
        result["ok"] = False
        result["status"] = status
        if status == 404:
            result["message"] = "This CoreScope doesn't list monitors yet"
        else:
            result["message"] = (
                f"CoreScope answered HTTP {status}" if status else "CoreScope unreachable"
            )
