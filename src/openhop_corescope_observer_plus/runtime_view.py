"""runtime.json: everything the plugin page shows."""

from __future__ import annotations

import json
import time

from . import __version__
from .coordination import Coordinator
from .models import JsonDict, Neighbour
from .polling_keys import PollingKeys
from .schedule import next_slot
from .station import Station
from .timeutil import utc_iso


class RuntimeView:
    def __init__(self, station: Station, coordinator: Coordinator, keys: PollingKeys):
        self.station = station
        self.coordinator = coordinator
        self.keys = keys

    def _who_polls(self, n: Neighbour, now: float, phases: dict[str, float]) -> JsonDict:
        """The "Who polls" cell. Cached data only: no CoreScope calls."""
        station = self.station
        node_id = n.node_id
        st = station.state.value["nodes"].get(node_id, {})
        window = station.window_s(node_id, now)
        interval = station.interval_s(node_id, now)
        role = self.coordinator.role_for(node_id, now, fetch=False)
        skip = st.get("skip") or {}
        next_due = float(st.get("next_due_at") or 0)
        if station.community.excluded(node_id) or role["role"] == "excluded":
            who = "excluded"
        elif role["role"] == "standby":
            who = "standby"
        elif next_due > now and skip.get("reason") == "fresh":
            who = "covered"
        elif next_due > now and skip.get("reason") == "cap":
            who = "capped"
        else:
            who = "you"
        return {
            "who": who,
            "by": role.get("by") or skip.get("by") or "",
            "owner_hops": role.get("owner_hops"),
            "independent": bool(role.get("independent")),
            "next_at": max(next_due, next_slot(st, interval, now, phases.get(node_id))),
            "fresh_at": st.get("fresh_at"),
            "fresh_by": st.get("fresh_by") or "",
            "window_s": window,
            "interval_s": interval,
            "hops": station.best_hops(node_id),
            "skip": skip,
        }

    def _community(self, now: float) -> JsonDict:
        station = self.station
        saved = station.state.value
        view = station.community.view(now)
        view.update(
            enabled=station.settings.community,
            corescope_url=station.settings.corescope_url,
            claimed=int(saved.get("claims_count") or 0) if saved.get("claims_community") else 0,
            claims_at=saved.get("claims_at"),
            owners_ok_at=self.coordinator.owners_cache.ok_at or None,
        )
        return view

    def _node(self, n: Neighbour, now: float, phases: dict[str, float]) -> JsonDict:
        station = self.station
        settings = station.settings
        node_id = n.node_id
        st = station.state.value["nodes"].get(node_id, {})
        password_set = station.secrets.has_password(node_id)
        selected = node_id in settings.targets
        return {
            "node_id": node_id,
            "name": n.name,
            "snr": n.snr,
            "rssi": n.rssi,
            "last_seen": n.last_seen,
            "zero_hop": n.zero_hop,
            "hops": n.hops,
            "routes": [r.as_dict() for r in n.routes],
            "reachable": bool(station.routes_for(n)),
            "manual": n.manual,
            "selected": selected,
            "password_set": password_set,
            # The guest password the guest key last logged in with (learnt).
            "guest_password": st.get("shared_password"),
            "packets": st.get("packets") or [],
            "code": st.get("code"),
            "detail": st.get("detail"),
            "role": st.get("role"),
            "battery_mv": st.get("battery_mv"),
            "last_success": st.get("last_success"),
            "last_attempt": st.get("last_attempt"),
            "retried": bool(st.get("retried")),
            "no_reply_count": st.get("no_reply_count") or 0,
            "last_route": st.get("last_route"),
            "last_route_label": st.get("last_route_label"),
            "route_results": st.get("route_results") or {},
            "temperature_c": st.get("temperature_c"),
            "temp_at": st.get("temp_at"),
            "temp_code": st.get("temp_code"),
            "temp_misses": st.get("temp_misses") or 0,
            "read_temperature": node_id in settings.temperature_targets,
            "uptime_secs": st.get("uptime_secs"),
            "method": st.get("method"),
            "status_ignored": bool(st.get("status_ignored")),
            "key_mode": "shared" if self.keys.uses_shared_key(node_id) else "own",
            "shared_opt_in": node_id in settings.shared_key_targets and not password_set,
            "guest_overridden": node_id in settings.guest_key_overrides,
            "shared_admin_seen": bool(st.get("shared_admin_seen")),
            "shared_admin_removed": st.get("shared_admin_removed"),
            "shared_admin_at": st.get("shared_admin_at"),
            "covered_at": st.get("covered_at"),
            "covered": bool(st.get("last_covered")),
            "via_corescope": bool(st.get("via_corescope")),
            "flood_replies": int(st.get("flood_replies") or 0),
            "community": self._who_polls(n, now, phases) if selected else None,
        }

    def _brokers(self) -> list[JsonDict]:
        station = self.station
        out = []
        for b in station.ohcfg.brokers if station.ohcfg else []:
            saved = station.state.value["brokers"].get(b.name, {})
            live = station.openhop_brokers.get(b.name, {})
            out.append(
                {
                    "name": b.name,
                    "host": b.host,
                    "format": b.format,
                    "enabled": b.enabled,
                    "openhop_connected": bool((live.get("status") or {}).get("connected")),
                    "selected": b.name in station.settings.brokers,
                    "queued": len(saved.get("queue", [])),
                    "last_ok": saved.get("last_ok"),
                    "last_count": saved.get("last_count"),
                    "last_error": saved.get("last_error", ""),
                }
            )
        return out

    def write(self) -> None:
        station = self.station
        settings = station.settings
        now = time.time()
        phases = station.phases(now)
        nodes = [self._node(n, now, phases) for n in station.candidates]
        brokers = self._brokers()
        ohcfg = station.ohcfg
        runtime = {
            "schema": 1,
            "plugin_version": __version__,
            "generated_at": utc_iso(),
            "observer_id": station.observer_id,
            "community": self._community(now),
            "self_check": station.check,
            "token_configured": bool(station.secrets.api_token),
            "companion": {"name": settings.companion_name, "public_key": station.companion_key},
            "repeater": {
                "name": ohcfg.node_name if ohcfg else "",
                "iata": ohcfg.iata_code if ohcfg else "",
            },
            "nodes": nodes,
            "brokers": brokers,
            "actions": station.actions,
            "packet_log": station.packets.entries,
            "activity": station.packets.summary(),
            "shared_key": self.keys.runtime_view(),
            "corescope": {
                "url": settings.corescope_url,
                "check": settings.check_corescope,
                "shared_enabled": self.keys.shared_key() is not None,
            },
        }
        path = station.data_dir / "runtime.json"
        tmp = path.with_suffix(".tmp")
        text = json.dumps(runtime, indent=2, ensure_ascii=False) + "\n"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
