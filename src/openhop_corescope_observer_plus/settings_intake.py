"""Taking in what the plugin page wrote to config.json."""

from __future__ import annotations

import logging

from .models import JsonDict
from .settings import TRANSIENT_KEYS, parse, read_raw, write_raw
from .station import Station

LOG = logging.getLogger("openhop.corescope_observer_plus")


def _take_transients(station: Station, raw: JsonDict) -> None:
    """Secrets and requests leave config.json as soon as they are read."""
    token = raw.get("api_token_input")
    if isinstance(token, str) and token.strip():
        station.secrets.set_api_token(token)
        LOG.info("API token saved")
    updates = raw.get("password_updates")
    if isinstance(updates, dict):
        for node_id, password in updates.items():
            node_id = str(node_id).lower()
            if len(node_id) == 64:
                station.secrets.set_password(node_id, str(password or ""))
                station.state.node(node_id)["password_changed"] = True
                LOG.info("login password %s for %s", "set" if password else "removed", node_id[:8])
    requests = raw.get("requests")
    if isinstance(requests, list):
        station.state.value.setdefault("pending", []).extend(
            r for r in requests if isinstance(r, dict) and r.get("id")
        )
    if any(k in raw for k in TRANSIENT_KEYS):
        for k in TRANSIENT_KEYS:
            raw.pop(k, None)
        write_raw(station.data_dir, raw)
        station.state.save()


def _clear_admin_stops_lifted_by_hand(station: Station) -> None:
    # Turning the guest key back on by hand clears an admin stop. Only by hand:
    # the default never restarts it.
    for node_id, on in station.settings.guest_key_overrides.items():
        st = station.state.value.get("nodes", {}).get(node_id)
        if on and st and st.get("shared_admin_seen"):
            for k in ("shared_admin_seen", "shared_admin_removed", "shared_admin_at"):
                st.pop(k, None)
            station.state.save()


def load_settings(station: Station) -> None:
    raw = read_raw(station.data_dir)
    _take_transients(station, raw)
    station.settings = parse(raw)
    if "guest_key_default" not in raw and "shared_key_targets" in raw:
        # The guest key used to be opt-in; it is opt-out now, and the old list goes.
        raw.pop("shared_key_targets")
        raw.update(guest_key_default=True, guest_key_overrides={})
        write_raw(station.data_dir, raw)
    station.community.reset_for_url(station.settings.corescope_url)
    _clear_admin_stops_lifted_by_hand(station)
