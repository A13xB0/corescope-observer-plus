"""The plugin's work loop: settings, self-check, polling, publishing, status."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import __version__
from .companion import CompanionClient
from .openhop import (
    Neighbour,
    OpenHopAPI,
    OpenHopConfig,
    OpenHopError,
    Route,
    companion_port,
    corescope_latest_battery_ts,
    fetch_shared_guest_key,
    load_openhop_config,
    search_corescope,
)
from . import shared_key as sk
from .poller import ADMIN_STOP, NO_BATTERY, NO_REPLY, OK, is_due, poll_one, poll_one_shared, reading, record, utc_iso
from .publisher import Publisher
from .settings import TRANSIENT_KEYS, Settings, parse, read_raw, write_raw
from .storage import Secrets, State

LOG = logging.getLogger("openhop.neighbour_battery")

MAX_POLLS_PER_CYCLE = 8
QUEUE_MAX = 200
QUEUE_MAX_AGE = 2 * 3600  # CoreScope drops readings older than this


class App:
    def __init__(self, data_dir: Path, *, companion_host: str = "127.0.0.1"):
        self.data_dir = data_dir
        self.companion_host = companion_host
        self.secrets = Secrets(data_dir)
        self.state = State(data_dir)
        self.settings = Settings()
        self.ohcfg: OpenHopConfig | None = None
        self.check: dict[str, dict] = {}
        self.candidates: list[Neighbour] = []
        self.openhop_brokers: dict[str, dict] = {}
        self.companion_key = ""
        self.actions: dict[str, dict] = self.state.value.setdefault("actions", {})
        self._last_refresh = 0.0
        self._shared_cache: dict = {}  # the shared key fetched from CoreScope, cached by URL

    # -- settings ---------------------------------------------------------

    def load_settings(self) -> None:
        raw = read_raw(self.data_dir)
        changed = False
        token = raw.get("api_token_input")
        if isinstance(token, str) and token.strip():
            self.secrets.set_api_token(token)
            LOG.info("API token saved")
        updates = raw.get("password_updates")
        if isinstance(updates, dict):
            for node_id, pw in updates.items():
                node_id = str(node_id).lower()
                if len(node_id) == 64:
                    self.secrets.set_password(node_id, str(pw or ""))
                    self.state.node(node_id)["password_changed"] = True
                    LOG.info("login password %s for %s", "set" if pw else "removed", node_id[:8])
        requests = raw.get("requests")
        if isinstance(requests, list):
            self.state.value.setdefault("pending", []).extend(
                r for r in requests if isinstance(r, dict) and r.get("id")
            )
        for k in TRANSIENT_KEYS:
            if k in raw:
                raw.pop(k)
                changed = True
        if changed:
            write_raw(self.data_dir, raw)
            self.state.save()
        self.settings = parse(raw)

    def api(self) -> OpenHopAPI:
        return OpenHopAPI(self.settings.openhop_api_url, self.secrets.api_token)

    # -- self-check and openHop data --------------------------------------

    def refresh(self) -> None:
        """Re-read openHop's config and API; record what works."""
        check: dict[str, dict] = {}
        try:
            self.ohcfg = load_openhop_config(Path(self.settings.openhop_config_path))
            check["config"] = {"ok": True, "message": "openHop config readable"}
        except OpenHopError as exc:
            self.ohcfg = None
            check["config"] = {"ok": False, "message": str(exc)}

        if not self.secrets.api_token:
            check["api"] = {"ok": False, "message": "no API token yet: use Connect on the plugin page"}
        else:
            try:
                self.candidates = self.api().repeaters(self.settings.max_age_hours, self.settings.manual_repeaters)
                self.openhop_brokers = self.api().mqtt_status()
                direct = sum(n.hops == 0 for n in self.candidates)
                one_hop = sum(n.hops == 1 for n in self.candidates)
                check["api"] = {"ok": True, "message": f"{direct} repeaters heard direct, {one_hop} one hop away"}
            except OpenHopError as exc:
                check["api"] = {"ok": False, "message": str(exc)}

        port = companion_port(self.ohcfg, self.settings.companion_name) if self.ohcfg else None
        if port is None:
            check["companion"] = {"ok": False, "message": f"no companion named {self.settings.companion_name!r} yet: use Connect"}
        else:
            try:
                with CompanionClient(self.companion_host, port) as c:
                    self.companion_key = c.public_key.hex()
                check["companion"] = {"ok": True, "message": f"companion reachable on port {port}"}
            except OSError as exc:
                check["companion"] = {"ok": False, "message": f"companion port {port}: {exc}"}
            except Exception as exc:  # noqa: BLE001 - any failure is a failed check
                check["companion"] = {"ok": False, "message": str(exc)}

        if self.ohcfg:
            try:
                Publisher(self.ohcfg)
                check["publish"] = {"ok": True, "message": "repeater key loaded"}
            except Exception as exc:  # noqa: BLE001
                check["publish"] = {"ok": False, "message": f"repeater key unusable: {exc}"}
        else:
            check["publish"] = {"ok": False, "message": "needs openHop config"}
        self.check = check
        self._last_refresh = time.time()

    def connect(self) -> dict:
        """Create the plugin's companion identity if openHop has none yet."""
        if not self.secrets.api_token:
            raise RuntimeError("no API token")
        cfg = load_openhop_config(Path(self.settings.openhop_config_path))
        if companion_port(cfg, self.settings.companion_name) is None:
            self.api().create_companion(self.settings.companion_name, self.settings.companion_port)
            message = f"created companion {self.settings.companion_name} on port {self.settings.companion_port}"
        else:
            message = f"companion {self.settings.companion_name} already exists"
        self.refresh()
        return {"message": message}

    # -- polling ------------------------------------------------------------

    def _neighbour(self, node_id: str) -> Neighbour | None:
        for n in self.candidates:
            if n.node_id == node_id:
                return n
        return None

    def routes_for(self, n: Neighbour) -> list[Route]:
        """The routes polling may use for n under the current reach setting."""
        return [r for r in n.routes if r.hops <= self.settings.max_hops]

    def _corescope_covers(self, node_id: str, within_s: float, now: float) -> bool:
        """True if CoreScope already has a battery reading for the node newer
        than within_s — from any observer. A failed lookup is False, so the
        plugin falls back to retrying rather than skipping."""
        ts = corescope_latest_battery_ts(self.settings.corescope_url, node_id)
        if not ts:
            return False
        return now - _ts(ts) < within_s

    def shared_key(self, now: float | None = None) -> sk.SharedKey | None:
        """The shared guest key, fetched from CoreScope and cached.

        The plugin keeps no key of its own: it asks the configured CoreScope,
        which holds the one key for its network. Cached for 24 hours per URL.
        None when CoreScope serves no key or can't be reached — the plugin then
        has no shared key and falls back to polling with its own.
        """
        now = now or time.time()
        url = self.settings.corescope_url
        c = self._shared_cache
        if not (c.get("url") == url and now - float(c.get("at") or 0) < 24 * 3600):
            self._shared_cache = {"url": url, "at": now, "key": sk.SharedKey.from_api(fetch_shared_guest_key(url))}
        return self._shared_cache.get("key")

    def uses_shared_key(self, node_id: str) -> bool:
        """Whether this repeater is polled under the shared key.

        Only if the operator opted it in, the key is set up, and — the safety
        gate — it has no saved password and has never answered the key as
        admin. A repeater with a password can't be polled with a key that only
        ever logs in blank, and one that handed the key admin is never polled
        under it again until the operator clears the flag.
        """
        if node_id not in self.settings.shared_key_targets:
            return False
        if self.secrets.has_password(node_id):
            return False
        if self.state.node(node_id).get("shared_admin_seen"):
            return False
        return self.shared_key() is not None

    def poll(self, node_ids: list[str], *, force: bool = False) -> list[dict]:
        """Poll the given targets that are zero-hop now; return payload readings."""
        if not self.check.get("companion", {}).get("ok") or not self.ohcfg:
            return []
        port = companion_port(self.ohcfg, self.settings.companion_name)
        interval = self.settings.interval_minutes * 60
        now = time.time()
        due = []
        for node_id in node_ids:
            n = self._neighbour(node_id)
            st = self.state.node(node_id)
            if n is None or not self.routes_for(n):
                st["code"] = "not_direct" if n else "not_heard"
                continue
            if not (force or is_due(st, interval, self.secrets.has_password(node_id), now)):
                continue
            # A shared-key repeater that only answered late (its early retry is
            # due): skip the retry if CoreScope already has a fresh reading from
            # any observer — a neighbour's overhear covers it. Only when
            # CoreScope has the shared-guest feature live, and never on a forced
            # poll. The lookup fails open, so an unreachable CoreScope retries.
            if (not force and self.settings.check_corescope and st.get("code") == NO_REPLY
                    and self.uses_shared_key(node_id)
                    and self._corescope_covers(node_id, interval, now)):
                st["covered_at"] = now
                st["last_attempt"] = now  # reschedule; another observer has it for now
                LOG.info("skip %s (%s): CoreScope already has a fresh reading from another observer",
                         n.name, node_id[:8])
                continue
            due.append(n)
        due.sort(key=lambda n: float(self.state.node(n.node_id).get("last_attempt") or 0))
        out = []
        if not due:
            return out
        s = self.settings
        with CompanionClient(self.companion_host, port) as client:
            for i, n in enumerate(due[:MAX_POLLS_PER_CYCLE]):
                routes = self.routes_for(n)
                if i:
                    # A poll through a relay costs the relay two transmissions.
                    time.sleep(s.spacing_seconds * (2 if routes[0].hops else 1))
                st = self.state.node(n.node_id)
                pw = self.secrets.password(n.node_id)
                shared = self.shared_key() if self.uses_shared_key(n.node_id) else None
                if shared is not None:
                    result = poll_one_shared(
                        client, n, shared, st, routes=routes,
                        read_temperature=n.node_id in s.temperature_targets,
                    )
                else:
                    result = poll_one(
                        client, n, pw, st, routes=routes, alternative_routes=s.alternative_routes,
                        read_temperature=n.node_id in s.temperature_targets,
                    )
                record(st, result, bool(pw), time.time())
                st.pop("password_changed", None)
                if result.code in (OK, NO_BATTERY):
                    st.pop("covered_at", None)
                st["name"] = n.name
                if result.code == ADMIN_STOP:
                    self._disable_shared_key(n, result)
                LOG.info("poll %s (%s) %s%s: %s %s%s", n.name, n.node_id[:8],
                         result.route.label if result.route else "-",
                         " [shared]" if result.key_mode == "shared" else "", result.code,
                         f"{result.battery_mv} mV" if result.battery_mv is not None else result.detail,
                         f", {result.temperature_c} °C" if result.temperature_c is not None
                         else (f", temperature {result.temperature_code}" if result.temperature_code else ""))
                if result.code in (OK, NO_BATTERY):
                    out.append(reading(n, result, time.time()))
                self.state.save()
        return out

    def _shared_key_runtime(self) -> dict:
        """What the plugin page shows for the shared key: the key CoreScope
        serves (if any), where it came from, and the opt-in list."""
        key = self.shared_key()
        return {
            "configured": key is not None,
            "from": self.settings.corescope_url,
            "name": key.name if key else "",
            "version": key.version if key else 0,
            "public": key.public_key.hex() if key else "",
            "targets": self.settings.shared_key_targets,
        }

    def _disable_shared_key(self, n: Neighbour, result) -> None:
        """Turn the shared key off for a repeater that answered it as admin.

        The opt-in is removed from config.json so it stays off until the
        operator turns it back on deliberately, and a loud warning is logged.
        The removal drill has already run inside the poll; its outcome is in
        the node state for the UI and CoreScope alert.
        """
        raw = read_raw(self.data_dir)
        targets = [t for t in (raw.get("shared_key_targets") or []) if str(t).lower() != n.node_id]
        raw["shared_key_targets"] = targets
        write_raw(self.data_dir, raw)
        self.settings.shared_key_targets = [t for t in self.settings.shared_key_targets if t != n.node_id]
        LOG.warning("%s (%s) answered the shared guest key as ADMIN: shared key disabled for it, "
                    "removal %s", n.name, n.node_id[:8],
                    "succeeded" if result.admin_removed else "FAILED — ask the owner to run setperm 0")

    # -- publishing ---------------------------------------------------------

    def queue(self, readings: list[dict]) -> None:
        for name in self.settings.brokers:
            q = self.state.broker(name).setdefault("queue", [])
            q.extend(readings)

    def publish_claims(self, now: float) -> None:
        """Tell CoreScope which repeaters this observer polls under the shared
        key, so decoded replies are credited to it. Refreshed at most twice an
        hour; the claim lasts 3 h on CoreScope."""
        if not self.ohcfg or not self.check.get("publish", {}).get("ok"):
            return
        nodes = [n for n in self.settings.shared_key_targets if self.uses_shared_key(n)]
        if not nodes or now - float(self.state.value.get("claims_at") or 0) < 1800:
            return
        publisher = Publisher(self.ohcfg)
        brokers = {b.name: b for b in self.ohcfg.brokers}
        sent = False
        for name in self.settings.brokers:
            b = brokers.get(name)
            if b is None:
                continue
            try:
                publisher.publish_claim(b, nodes)
                sent = True
            except Exception as exc:  # noqa: BLE001 - retried next tick
                LOG.warning("shared-key claim to %s failed: %s", name, exc)
        if sent:
            self.state.value["claims_at"] = now
            self.state.save()

    def flush(self) -> None:
        if not self.ohcfg or not self.check.get("publish", {}).get("ok"):
            return
        publisher = Publisher(self.ohcfg)
        brokers = {b.name: b for b in self.ohcfg.brokers}
        now = time.time()
        for name in self.settings.brokers:
            bs = self.state.broker(name)
            q = [r for r in bs.get("queue", []) if now - _ts(r["sampled_at"]) < QUEUE_MAX_AGE][-QUEUE_MAX:]
            # One reading per node per message: keep the newest.
            latest: dict[str, dict] = {}
            for r in q:
                latest[r["node_id"]] = r
            bs["queue"] = list(latest.values())
            if not bs["queue"]:
                continue
            broker = brokers.get(name)
            if broker is None:
                bs["last_error"] = "not in openHop's broker list"
                continue
            try:
                publisher.publish(broker, bs["queue"])
                bs["last_ok"] = now
                bs["last_count"] = len(bs["queue"])
                bs["last_error"] = ""
                bs["queue"] = []
                LOG.info("published %d reading(s) to %s", bs["last_count"], name)
            except Exception as exc:  # noqa: BLE001 - kept and retried next tick
                bs["last_error"] = str(exc)
                LOG.warning("publish to %s failed: %s", name, exc)
        self.state.save()

    # -- one tick -------------------------------------------------------------

    def run_requests(self) -> None:
        pending = self.state.value.get("pending") or []
        self.state.value["pending"] = []
        for req in pending:
            rid, kind = str(req.get("id")), req.get("type")
            result = {"id": rid, "type": kind, "at": utc_iso(), "ok": True, "message": ""}
            try:
                if kind == "connect":
                    result["message"] = self.connect()["message"]
                elif kind in ("test_login", "poll_now"):
                    node_id = str(req.get("node_id") or "").lower()
                    self.refresh()
                    readings = self.poll([node_id], force=True)
                    st = self.state.node(node_id)
                    result["code"] = st.get("code")
                    result["role"] = st.get("role", "")
                    result["battery_mv"] = st.get("battery_mv")
                    result["route"] = st.get("last_route_label") or ""
                    result["temperature_c"] = st.get("temperature_c") if st.get("temp_code") == "ok" else None
                    result["message"] = st.get("detail") or st.get("code") or ""
                    if kind == "poll_now" and readings:
                        self.queue(readings)
                        self.flush()
                elif kind == "check_now":
                    self.refresh()
                    readings = self.poll(self.settings.targets, force=True)
                    if readings:
                        self.queue(readings)
                        self.flush()
                    codes = [self.state.node(n).get("code") or "?" for n in self.settings.targets]
                    result["readings"] = len(readings)
                    result["message"] = (
                        f"{len(readings)} of {len(self.settings.targets)} read"
                        + (f" ({', '.join(sorted(set(c for c in codes if c not in ('ok', 'no_battery'))))} for the rest)"
                           if len(readings) < len(self.settings.targets) else "")
                    )
                elif kind == "lookup":
                    query = str(req.get("query") or "").strip()
                    if len(query) < 3:
                        raise RuntimeError("type at least 3 characters")
                    result["results"] = search_corescope(self.settings.corescope_url, query)
                    result["message"] = f"{len(result['results'])} found in CoreScope"
                elif kind == "refresh":
                    self.refresh()
                else:
                    raise RuntimeError(f"unknown request {kind!r}")
            except Exception as exc:  # noqa: BLE001
                result["ok"] = False
                result["message"] = str(exc)
            self.actions[kind or "unknown"] = result
        if pending:
            self.state.save()

    def tick(self) -> None:
        self.load_settings()
        if time.time() - self._last_refresh > 300 or not self.check:
            self.refresh()
        self.run_requests()
        if self.settings.enabled and self.settings.targets:
            readings = self.poll(self.settings.targets)
            if readings:
                self.queue(readings)
        if self.settings.enabled:
            self.publish_claims(time.time())
        self.flush()
        self.write_runtime()

    def write_runtime(self) -> None:
        selected = set(self.settings.targets)
        nodes = []
        for n in self.candidates:
            st = self.state.value["nodes"].get(n.node_id, {})
            nodes.append({
                "node_id": n.node_id, "name": n.name, "snr": n.snr, "rssi": n.rssi,
                "last_seen": n.last_seen, "zero_hop": n.zero_hop, "hops": n.hops,
                "routes": [r.as_dict() for r in n.routes], "reachable": bool(self.routes_for(n)),
                "manual": n.manual,
                "selected": n.node_id in selected,
                "password_set": self.secrets.has_password(n.node_id),
                "code": st.get("code"), "detail": st.get("detail"), "role": st.get("role"),
                "battery_mv": st.get("battery_mv"), "last_success": st.get("last_success"),
                "last_attempt": st.get("last_attempt"), "retried": bool(st.get("retried")),
                "no_reply_count": st.get("no_reply_count") or 0,
                "last_route": st.get("last_route"), "last_route_label": st.get("last_route_label"),
                "temperature_c": st.get("temperature_c"), "temp_at": st.get("temp_at"),
                "temp_code": st.get("temp_code"), "temp_misses": st.get("temp_misses") or 0,
                "read_temperature": n.node_id in self.settings.temperature_targets,
                "uptime_secs": st.get("uptime_secs"), "method": st.get("method"),
                "status_ignored": bool(st.get("status_ignored")),
                "key_mode": "shared" if self.uses_shared_key(n.node_id) else "own",
                "shared_opt_in": n.node_id in self.settings.shared_key_targets,
                "shared_admin_seen": bool(st.get("shared_admin_seen")),
                "shared_admin_removed": st.get("shared_admin_removed"),
                "shared_admin_at": st.get("shared_admin_at"),
                "covered_at": st.get("covered_at"),
            })
        brokers = []
        for b in (self.ohcfg.brokers if self.ohcfg else []):
            bs = self.state.value["brokers"].get(b.name, {})
            live = self.openhop_brokers.get(b.name, {})
            brokers.append({
                "name": b.name, "host": b.host, "format": b.format, "enabled": b.enabled,
                "openhop_connected": bool((live.get("status") or {}).get("connected")),
                "selected": b.name in self.settings.brokers,
                "queued": len(bs.get("queue", [])), "last_ok": bs.get("last_ok"),
                "last_count": bs.get("last_count"), "last_error": bs.get("last_error", ""),
            })
        runtime = {
            "schema": 1,
            "plugin_version": __version__,
            "generated_at": utc_iso(),
            "self_check": self.check,
            "token_configured": bool(self.secrets.api_token),
            "companion": {"name": self.settings.companion_name, "public_key": self.companion_key},
            "repeater": {"name": self.ohcfg.node_name if self.ohcfg else "",
                         "iata": self.ohcfg.iata_code if self.ohcfg else ""},
            "nodes": nodes,
            "brokers": brokers,
            "actions": self.actions,
            "shared_key": self._shared_key_runtime(),
            "corescope": {
                "url": self.settings.corescope_url,
                "check": self.settings.check_corescope,
                "shared_enabled": self.shared_key() is not None,
            },
        }
        path = self.data_dir / "runtime.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(runtime, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)


def _ts(iso: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
