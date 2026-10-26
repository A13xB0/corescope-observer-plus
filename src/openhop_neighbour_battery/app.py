"""The plugin's work loop: settings, self-check, polling, publishing, status."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from openhop_core import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder

from . import __version__
from .companion import CompanionClient
from .openhop import (
    Neighbour,
    OpenHopAPI,
    OpenHopConfig,
    OpenHopError,
    Route,
    companion_port,
    corescope_battery_snapshot,
    corescope_responses,
    corescope_latest_battery_ts,
    corescope_owners,
    fetch_shared_guest_key,
    load_openhop_config,
    register_guest_key,
    search_corescope,
)
from . import guest_key
from . import packet_log
from . import shared_key as sk
from .poller import ADMIN_STOP, NO_BATTERY, NO_REPLY, OK, is_due, poll_one, poll_one_shared, reading, record, utc_iso
from .publisher import Publisher
from .settings import TRANSIENT_KEYS, Settings, parse, read_raw, write_raw
from .storage import Secrets, State

LOG = logging.getLogger("openhop.neighbour_battery")

MAX_POLLS_PER_CYCLE = 8  # scheduled cycles; a forced Check now polls every target
# A forced Test of the same repeater within this long is refused: each one can
# put logins on air, and a burst of them only adds load.
TEST_COOLDOWN_S = 60
# A short settle before confirming against CoreScope, so a just-published
# own-key reading has a moment to be ingested.
CORESCOPE_SETTLE_S = 2.0
# CoreScope stamps a reading with the request time the repeater echoes; allow
# this much clock difference when asking "is it from after we started?".
CORESCOPE_CLOCK_SLACK_S = 5
# Check now's "stale" scope: repeaters with no reading for at least this long.
STALE_AFTER_S = 3600
QUEUE_MAX = 200
QUEUE_MAX_AGE = 2 * 3600  # CoreScope drops readings older than this


class App:
    def __init__(self, data_dir: Path, *, companion_host: str = "127.0.0.1"):
        self.data_dir = data_dir
        self.companion_host = companion_host
        self.secrets = Secrets(data_dir)
        self.state = State(data_dir)
        self._seed_tag()
        self.settings = Settings()
        self.ohcfg: OpenHopConfig | None = None
        self.check: dict[str, dict] = {}
        self.candidates: list[Neighbour] = []
        self.openhop_brokers: dict[str, dict] = {}
        self.companion_key = ""
        self.actions: dict[str, dict] = self.state.value.setdefault("actions", {})
        # The last packets sent and what answered them (packet_log.py).
        self.packets = packet_log.PacketLog(self.state.value.setdefault("packet_log", []))
        self._last_refresh = 0.0
        self._shared_cache: dict = {}  # the shared key fetched from CoreScope, cached by URL
        self._own_key: sk.SharedKey | None = None  # this observer's polling key (guest_key.py)
        self._owners: dict = {"at": 0.0, "ok_at": 0.0, "nodes": {}}  # who polls which repeater (CoreScope)

    # -- settings ---------------------------------------------------------

    def _seed_tag(self) -> None:
        """Never send a request timestamp lower than one already used.

        A repeater ignores a request whose timestamp isn't above the last one
        it accepted from that key. openhop_core keeps its timestamps strictly
        increasing within this process; this carries that across restarts (and
        a clock that was briefly ahead) by starting from the highest we used.
        """
        last = int(self.state.value.get("last_tag") or 0)
        if last > PacketBuilder._last_unique_timestamp:
            PacketBuilder._last_unique_timestamp = last

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
        # Re-opting a repeater into the shared key (adding it back to the list
        # after an admin answer disabled it) clears the admin stop, so it can
        # be tried again deliberately.
        for node_id in self.settings.shared_key_targets:
            st = self.state.value.get("nodes", {}).get(node_id)
            if st and st.get("shared_admin_seen"):
                for k in ("shared_admin_seen", "shared_admin_removed", "shared_admin_at"):
                    st.pop(k, None)
                self.state.save()

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

    def _corescope_confirm(self, node_id: str, since: float) -> dict:
        """CoreScope's view of a node's battery for the live "did it land?"
        line. fresh is True when CoreScope's newest reading is from after the
        poll started, i.e. this poll (or an observer's overhear of it) reached
        CoreScope. Always a dict so the UI can show "checked, nothing yet"."""
        snap = corescope_battery_snapshot(self.settings.corescope_url, node_id)
        if not snap:
            return {"reachable": False}
        ts = snap.get("latest_ts")
        snap["reachable"] = True
        snap["fresh"] = _fresh(ts, since)
        return snap

    def shared_key(self, now: float | None = None) -> sk.SharedKey | None:
        """The shared guest key, fetched from CoreScope and cached.

        The plugin keeps no key of its own: it asks the configured CoreScope,
        which holds the one key for its network. Cached for 24 hours per URL.
        None when CoreScope serves no key or can't be reached — the plugin then
        has no shared key and falls back to polling with its own.
        """
        # Nothing opted in: don't call CoreScope at all (an own-key-only install
        # never fetches the shared key).
        if not self.settings.shared_key_targets:
            return None
        now = now or time.time()
        url = self.settings.corescope_url
        c = self._shared_cache
        # A key is cached for 24 h; a miss (CoreScope down or serving none) is
        # cached only briefly, so a transient failure isn't stuck for a day.
        ttl = 24 * 3600 if c.get("key") else 300
        if not (c.get("url") == url and now - float(c.get("at") or 0) < ttl):
            self._shared_cache = {"url": url, "at": now, "key": sk.SharedKey.from_api(fetch_shared_guest_key(url))}
        return self._shared_cache.get("key")

    @property
    def observer_id(self) -> str:
        return LocalIdentity(self.ohcfg.identity_key).get_public_key().hex() if self.ohcfg else ""

    def own_key(self) -> sk.SharedKey:
        if self._own_key is None:
            self._own_key = guest_key.load_or_create(self.data_dir)
        return self._own_key

    def polling_key(self, now: float | None = None) -> sk.SharedKey | None:
        """The key guest polls go out under: this observer's own key once
        CoreScope has accepted it (registered within KEY_LIFETIME), otherwise
        the public shared key. None when nothing is opted in."""
        if not self.settings.shared_key_targets:
            return None
        now = now or time.time()
        reg = self.state.value.setdefault("guest_reg", {})
        if reg.get("url") != self.settings.corescope_url:
            reg.clear()
            reg["url"] = self.settings.corescope_url
        retry = (guest_key.REREGISTER_EVERY if reg.get("ok")
                 else 6 * 3600 if reg.get("unsupported") else 300)
        if self.ohcfg and self.settings.corescope_url and now - float(reg.get("tried_at") or 0) >= retry:
            self._register_own_key(reg, now)
        if reg.get("ok") and now - float(reg.get("ok_at") or 0) < guest_key.KEY_LIFETIME:
            return self.own_key()
        return self.shared_key(now)

    def _register_own_key(self, reg: dict, now: float) -> None:
        """(Re)register this observer's polling key with CoreScope, signed by
        the repeater key. A failure keeps an earlier registration until it
        lapses; a CoreScope without the feature (404) means the shared key."""
        reg["tried_at"] = now
        try:
            body = guest_key.registration_body(LocalIdentity(self.ohcfg.identity_key), self.own_key(), now)
        except ValueError as exc:
            reg["error"] = str(exc)
            LOG.warning("can't register the polling key: %s", exc)
            return
        status, resp = register_guest_key(self.settings.corescope_url, body)
        if status == 200 and resp.get("ok"):
            reg.update(ok=True, ok_at=now, unsupported=False, error="", expires_at=resp.get("expires_at"))
            LOG.info("own polling key %s registered with CoreScope", self.own_key().hex8)
        elif status == 404:
            reg.update(ok=False, unsupported=True, error="this CoreScope doesn't take per-observer keys")
            LOG.info("CoreScope doesn't take per-observer keys; using the shared key")
        else:
            reg["error"] = str(resp.get("error") or (f"HTTP {status}" if status else "CoreScope unreachable"))
            LOG.warning("couldn't register the polling key with CoreScope: %s", reg["error"])

    def owners(self, now: float) -> dict:
        """CoreScope's choice of poller per repeater, refreshed every 10 min.
        Empty (poll everything ourselves) if it hasn't answered for 30 min."""
        c = self._owners
        if self.ohcfg and self.settings.corescope_url and now - c["at"] >= 600:
            c["at"] = now
            nodes = corescope_owners(self.settings.corescope_url, self.observer_id)
            if nodes is not None:
                c["nodes"], c["ok_at"] = nodes, now
            elif now - c["ok_at"] > 1800:
                c["nodes"] = {}
        return c["nodes"]

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
        return self.polling_key() is not None

    def poll(self, node_ids: list[str], *, force: bool = False, on_node=None, on_step=None) -> list[dict]:
        """Poll the given targets that are zero-hop now; return payload readings.

        on_node(neighbour, state) is called after each repeater is polled, so a
        caller (Check now, Test) can stream live progress to the page.
        on_step(label) is called at each step of a single poll (login, status,
        telemetry), for the live step view in Test."""
        if not self.check.get("companion", {}).get("ok") or not self.ohcfg:
            return []
        port = companion_port(self.ohcfg, self.settings.companion_name)
        now = time.time()
        due = []
        for node_id in node_ids:
            interval = self.settings.interval_for(node_id) * 60
            n = self._neighbour(node_id)
            st = self.state.node(node_id)
            if n is None or not self.routes_for(n):
                st["code"] = "not_direct" if n else "not_heard"
                continue
            if not (force or is_due(st, interval, self.secrets.has_password(node_id), now)):
                continue
            # One poller per repeater: if CoreScope has another observer polling
            # this one, leave it to them (its reading still reaches CoreScope).
            # A forced Test or Check always polls.
            if not force and self.uses_shared_key(node_id):
                owner = self.owners(now).get(node_id)
                if owner and not owner.get("mine"):
                    st["polled_by"] = owner.get("owner_name") or str(owner.get("owner") or "")[:8]
                    st["last_attempt"] = now
                    LOG.info("skip %s (%s): polled by %s", n.name, node_id[:8], st["polled_by"])
                    continue
                st.pop("polled_by", None)
            # Before sending anything: a shared-key repeater whose last poll
            # went unanswered is skipped this time if CoreScope already has a
            # reading from within the interval — another observer has it. (The
            # poll itself also asks CoreScope, but only about the answer to its
            # own request, after sending.) Never on a forced poll; the lookup
            # fails open, so an unreachable CoreScope still polls.
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
            for i, n in enumerate(due if force else due[:MAX_POLLS_PER_CYCLE]):
                routes = self.routes_for(n)
                if i:
                    # A poll through a relay costs the relay two transmissions.
                    time.sleep(s.spacing_seconds * (2 if routes[0].hops else 1))
                st = self.state.node(n.node_id)
                pw = self.secrets.password(n.node_id)
                shared = self.polling_key() if self.uses_shared_key(n.node_id) else None
                plog = self.packets.for_node(
                    n.node_id, n.name, "companion" if shared is None
                    else ("guest key" if shared.public_key == self.own_key().public_key else "shared key"),
                    st.setdefault("packets", []))
                if shared is not None:
                    # Whatever we don't hear ourselves, the poll looks for in
                    # the packets CoreScope's observers heard, and decrypts.
                    antenna = None
                    if self.settings.check_corescope and self.settings.corescope_url:
                        url = self.settings.corescope_url
                        antenna = lambda since, u=url: corescope_responses(u, utc_iso(since))
                    result = poll_one_shared(
                        client, n, shared, st, routes=routes,
                        read_temperature=n.node_id in s.temperature_targets,
                        on_step=on_step, antenna=antenna,
                        personal=shared.public_key == self.own_key().public_key,
                        status_every=s.status_every_hours * 3600, plog=plog,
                        guest_password=s.guest_passwords.get(n.node_id),
                    )
                else:
                    result = poll_one(
                        client, n, pw, st, routes=routes, alternative_routes=s.alternative_routes,
                        read_temperature=n.node_id in s.temperature_targets,
                        on_step=on_step, status_every=s.status_every_hours * 3600, plog=plog,
                    )
                record(st, result, bool(pw), time.time())
                st.pop("password_changed", None)
                if result.code in (OK, NO_BATTERY) and not result.covered:
                    st.pop("covered_at", None)  # our own reading supersedes an observer's
                st["name"] = n.name
                if result.code == ADMIN_STOP:
                    self._disable_shared_key(n, result)
                LOG.info("poll %s (%s) %s%s: %s %s%s%s", n.name, n.node_id[:8],
                         result.route.label if result.route else "-",
                         " [shared]" if result.key_mode == "shared" else "", result.code,
                         f"{result.battery_mv} mV" if result.battery_mv is not None else result.detail,
                         f", {result.temperature_c} °C" if result.temperature_c is not None
                         else (f", temperature {result.temperature_code}" if result.temperature_code else ""),
                         f", reply {result.reply_s:.1f} s" if result.reply_s is not None
                         else (" (heard via CoreScope)" if result.via_corescope else ""))
                if shared is not None:
                    # Keep the newest request timestamp across restarts (see _seed_tag).
                    self.state.value["last_tag"] = max(int(self.state.value.get("last_tag") or 0),
                                                       PacketBuilder._last_unique_timestamp)
                # A shared-key reading is not published here: CoreScope decodes
                # it from the raw reply (which this node's openHop uploads like
                # any observer) and credits it to this poller via the claim, so
                # publishing it too would double the row. Own-key readings are
                # published as before.
                if result.code in (OK, NO_BATTERY) and result.key_mode != "shared":
                    out.append(reading(n, result, time.time()))
                self.state.save()
                # A cycle can take many minutes: keep the page (and its packet
                # log) current after each repeater, not only at the end.
                self.write_runtime()
                if on_node:
                    on_node(n, st)
        return out

    def _shared_key_runtime(self) -> dict:
        """What the plugin page shows for the polling key: whether it is this
        observer's own (registered with CoreScope) or the public shared one,
        where it came from, and the opt-in list."""
        key = self.polling_key()
        reg = self.state.value.get("guest_reg") or {}
        personal = key is not None and self._own_key is not None and key.public_key == self._own_key.public_key
        return {
            "configured": key is not None,
            "kind": "own" if personal else ("shared" if key is not None else ""),
            "registered_at": reg.get("ok_at") if reg.get("ok") else None,
            "expires_at": reg.get("expires_at") if reg.get("ok") else None,
            "register_error": reg.get("error") or "",
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
        claims = []
        for node_id in self.settings.shared_key_targets:
            n = self._neighbour(node_id)
            # Claim only repeaters still selected for polling, reachable, and
            # actually using the shared key — not ones left in the list but
            # unticked or out of range.
            if n is None or node_id not in self.settings.targets or not self.uses_shared_key(node_id):
                continue
            routes = self.routes_for(n)
            if not routes:
                continue
            r = routes[0]
            entry = {"node_id": node_id, "reply_hops": r.hops}
            if r.relay_id:
                entry["via"] = r.relay_id
            claims.append(entry)
        if not claims or now - float(self.state.value.get("claims_at") or 0) < 1800:
            return
        publisher = Publisher(self.ohcfg)
        brokers = {b.name: b for b in self.ohcfg.brokers}
        sent = False
        for name in self.settings.brokers:
            b = brokers.get(name)
            if b is None:
                continue
            try:
                publisher.publish_claim(b, claims)
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
                    self._run_test(kind, req, result)
                elif kind == "check_now":
                    self._run_check(req, result)
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
            finally:
                result["running"] = False  # never leave the page waiting on a failed request
            self.actions[kind or "unknown"] = result
        if pending:
            self.state.save()

    # -- live requests (Test, Check now) ---------------------------------------

    def _publish_action(self, kind: str, result: dict) -> None:
        """Show a request's progress on the page now, mid-request (the plugin
        is single-threaded, so this is the only way the page sees it live)."""
        self.actions[kind] = result
        self.write_runtime()

    def _confirm_wanted(self) -> bool:
        return bool(self.settings.check_corescope and self.settings.corescope_url)

    def _node_view(self, node_id: str, name: str = "") -> dict:
        """One repeater's latest outcome, as the live views show it."""
        st = self.state.node(node_id)
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

    def _run_test(self, kind: str, req: dict, result: dict) -> None:
        """Test / poll one repeater now, streaming each step to the dialog."""
        node_id = str(req.get("node_id") or "").lower()
        started = time.time()
        last = float(self.state.node(node_id).get("last_attempt") or 0)
        if started - last < TEST_COOLDOWN_S:
            wait = int(TEST_COOLDOWN_S - (started - last)) + 1
            result.update(self._node_view(node_id), running=False, ok=False,
                          message=f"Polled {int(started - last)} s ago. Wait {wait} s before testing it again.")
            return
        self.refresh()
        # Announce "running" before the poll (it can take up to a minute) so
        # the page shows it working, not stalled.
        result["running"] = True
        result["steps"] = []
        self._publish_action(kind, result)

        def on_step(label: str) -> None:
            result["steps"].append({"label": label, "at": utc_iso()})
            self._publish_action(kind, result)

        readings = self.poll([node_id], force=True, on_step=on_step)
        result.update(self._node_view(node_id))
        if kind == "poll_now" and readings:
            self.queue(readings)
            self.flush()
        # Did it reach CoreScope? For a shared-key repeater this is the real
        # "did it land?" — the reading is never published, CoreScope decodes it
        # from an overheard reply. Skipped when nothing was sent.
        if self._confirm_wanted() and result["code"] not in ("not_direct", "not_heard"):
            time.sleep(CORESCOPE_SETTLE_S)
            result["corescope"] = self._corescope_confirm(node_id, started)

    def _run_check(self, req: dict, result: dict) -> None:
        """Poll every selected repeater now ("stale": only those without a
        reading for STALE_AFTER_S), streaming each result to the page."""
        started = time.time()
        self.refresh()
        scope = "stale" if req.get("scope") == "stale" else "all"
        targets = list(self.settings.targets)
        if scope == "stale":
            def last_heard(t: str) -> float:
                st = self.state.node(t)
                return max(float(st.get("last_success") or 0), float(st.get("covered_at") or 0))
            targets = [t for t in targets if started - last_heard(t) >= STALE_AFTER_S]
        result.update(scope=scope, total=len(targets), progress=[], running=True)
        self._publish_action("check_now", result)

        def on_node(n, _st) -> None:
            result["progress"].append(self._node_view(n.node_id, n.name))
            self._publish_action("check_now", result)

        readings = self.poll(targets, force=True, on_node=on_node)
        if readings:
            self.queue(readings)
            self.flush()
        # Confirm each polled repeater against CoreScope, streaming as it goes.
        # Stop at the first unreachable answer: the rest would fail the same way.
        if self._confirm_wanted() and result["progress"]:
            time.sleep(CORESCOPE_SETTLE_S)
            for row in result["progress"]:
                row["corescope"] = self._corescope_confirm(row["node_id"], started)
                self._publish_action("check_now", result)
                if not row["corescope"].get("reachable"):
                    break
        # Count answers from state, not published readings: shared-key readings
        # are never published (CoreScope decodes them), but they were read.
        codes = [self.state.node(t).get("code") or "?" for t in targets]
        answered = sum(c in (OK, NO_BATTERY) for c in codes)
        result["readings"] = answered
        if not targets:
            result["message"] = ("every repeater had a reading within the hour"
                                 if scope == "stale" else "no repeaters selected")
            return
        rest = sorted({c for c in codes if c not in (OK, NO_BATTERY)})
        result["message"] = f"{answered} of {len(targets)} read" + (f" ({', '.join(rest)} for the rest)" if rest else "")

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
                # The guest password the guest key last logged in with (learnt).
                "guest_password": st.get("shared_password"),
                "packets": st.get("packets") or [],
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
                "covered": bool(st.get("last_covered")),
                "via_corescope": bool(st.get("via_corescope")),
                "flood_replies": int(st.get("flood_replies") or 0),
                "polled_by": st.get("polled_by") or "",
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
            "packet_log": self.packets.entries,
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


def _fresh(ts: str | None, since: float) -> bool:
    """Whether a CoreScope timestamp is from after `since` (with clock slack)."""
    return bool(ts) and _ts(ts) >= since - CORESCOPE_CLOCK_SLACK_S
