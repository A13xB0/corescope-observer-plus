"""Which key guest polls go out under: this observer's own, or the public shared one."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from openhop_core import LocalIdentity

from . import corescope, guest_key
from . import shared_key as sk
from .models import JsonDict, Neighbour
from .poll_result import PollResult
from .settings import read_raw, write_raw
from .station import Station

LOG = logging.getLogger("openhop.corescope_observer_plus")

KEY_CACHE_S = 24 * 3600
NO_KEY_CACHE_S = 300  # a miss is cached briefly, so a transient failure isn't stuck for a day
UNSUPPORTED_RETRY_S = 6 * 3600
FAILED_RETRY_S = 300


@dataclass
class _SharedKeyCache:
    url: str | None = None
    at: float = 0.0
    key: sk.SharedKey | None = None


class PollingKeys:
    def __init__(self, station: Station):
        self.station = station
        self._shared = _SharedKeyCache()
        self._own: sk.SharedKey | None = None

    def shared_key(self, now: float | None = None) -> sk.SharedKey | None:
        """The key CoreScope serves for its network, or None (polls then use the own key).

        Not asked for at all when nothing is on the guest key.
        """
        settings = self.station.settings
        if not settings.shared_key_targets:
            return None
        now = now or time.time()
        url = settings.corescope_url
        cache = self._shared
        ttl = KEY_CACHE_S if cache.key else NO_KEY_CACHE_S
        if not (cache.url == url and now - cache.at < ttl):
            self._shared = _SharedKeyCache(
                url, now, sk.SharedKey.from_api(corescope.shared_guest_key(url))
            )
        return self._shared.key

    def own_key(self) -> sk.SharedKey:
        if self._own is None:
            self._own = guest_key.load_or_create(self.station.data_dir)
        return self._own

    def is_own(self, key: sk.SharedKey) -> bool:
        return key.public_key == self.own_key().public_key

    def polling_key(self, now: float | None = None) -> sk.SharedKey | None:
        """This observer's own key once CoreScope has accepted it, else the shared key.

        None when nothing is on the guest key.
        """
        station = self.station
        settings = station.settings
        if not settings.shared_key_targets:
            return None
        now = now or time.time()
        reg = station.state.value.setdefault("guest_reg", {})
        if reg.get("url") != settings.corescope_url:
            reg.clear()
            reg["url"] = settings.corescope_url
        if reg.get("ok"):
            retry = guest_key.REREGISTER_EVERY
        else:
            retry = UNSUPPORTED_RETRY_S if reg.get("unsupported") else FAILED_RETRY_S
        tried_at = float(reg.get("tried_at") or 0)
        if station.ohcfg and settings.corescope_url and now - tried_at >= retry:
            self._register_own_key(reg, station.ohcfg.identity_key, now)
        if reg.get("ok") and now - float(reg.get("ok_at") or 0) < guest_key.KEY_LIFETIME:
            return self.own_key()
        return self.shared_key(now)

    def _register_own_key(self, reg: JsonDict, identity_key: bytes, now: float) -> None:
        """Signed by the repeater key. A failure keeps an earlier registration until it lapses."""
        reg["tried_at"] = now
        try:
            body = guest_key.registration_body(LocalIdentity(identity_key), self.own_key(), now)
        except ValueError as exc:
            reg["error"] = str(exc)
            LOG.warning("can't register the polling key: %s", exc)
            return
        url = self.station.settings.corescope_url
        status, resp = corescope.register_guest_key(url, body)
        if status == 200 and resp.get("ok"):
            reg.update(
                ok=True, ok_at=now, unsupported=False, error="", expires_at=resp.get("expires_at")
            )
            LOG.info("own polling key %s registered with CoreScope", self.own_key().hex8)
        elif status == 404:
            reg.update(
                ok=False, unsupported=True, error="this CoreScope doesn't take per-observer keys"
            )
            LOG.info("CoreScope doesn't take per-observer keys; using the shared key")
        else:
            fallback = f"HTTP {status}" if status else "CoreScope unreachable"
            reg["error"] = str(resp.get("error") or fallback)
            LOG.warning("couldn't register the polling key with CoreScope: %s", reg["error"])

    def uses_shared_key(self, node_id: str) -> bool:
        """Whether this repeater is polled under a guest key.

        Never with a saved password (the key only ever logs in blank or "hello")
        and never once it has answered the key as admin, until the operator
        clears that.
        """
        station = self.station
        if node_id not in station.settings.shared_key_targets:
            return False
        if station.secrets.has_password(node_id):
            return False
        if station.state.node(node_id).get("shared_admin_seen"):
            return False
        return self.polling_key() is not None

    def disable_for(self, n: Neighbour, result: PollResult) -> None:
        """Turn the key off for a repeater that answered it as admin.

        An override, so the guest key default can't turn it back on. The removal
        drill has already run inside the poll.
        """
        station = self.station
        raw = read_raw(station.data_dir)
        overrides = {**(raw.get("guest_key_overrides") or {}), n.node_id: False}
        raw["guest_key_overrides"] = overrides
        write_raw(station.data_dir, raw)
        settings = station.settings
        settings.guest_key_overrides[n.node_id] = False
        settings.shared_key_targets = [t for t in settings.shared_key_targets if t != n.node_id]
        LOG.warning(
            "%s (%s) answered the shared guest key as ADMIN: shared key disabled for it, "
            "removal %s",
            n.name,
            n.node_id[:8],
            "succeeded" if result.admin_removed else "FAILED — ask the owner to run setperm 0",
        )

    def runtime_view(self) -> JsonDict:
        """What the plugin page shows for the polling key."""
        station = self.station
        key = self.polling_key()
        reg = station.state.value.get("guest_reg") or {}
        personal = key is not None and self._own is not None and self.is_own(key)
        return {
            "configured": key is not None,
            "kind": "own" if personal else ("shared" if key is not None else ""),
            "registered_at": reg.get("ok_at") if reg.get("ok") else None,
            "expires_at": reg.get("expires_at") if reg.get("ok") else None,
            "register_error": reg.get("error") or "",
            "from": station.settings.corescope_url,
            "name": key.name if key else "",
            "version": key.version if key else 0,
            "public": key.public_key.hex() if key else "",
            "targets": station.settings.shared_key_targets,
        }
