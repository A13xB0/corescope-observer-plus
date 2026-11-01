"""Re-reading openHop's config and API, and recording what works."""

from __future__ import annotations

import time
from pathlib import Path

from . import publisher
from .companion import CompanionClient
from .models import OpenHopError
from .openhop_config import companion_port, load_openhop_config
from .station import CheckResult, Station

REFRESH_EVERY_S = 300


class SelfCheck:
    def __init__(self, station: Station):
        self.station = station
        self.last_refresh = 0.0

    def due(self) -> bool:
        return time.time() - self.last_refresh > REFRESH_EVERY_S or not self.station.check

    def _check_config(self) -> CheckResult:
        station = self.station
        try:
            station.ohcfg = load_openhop_config(Path(station.settings.openhop_config_path))
        except OpenHopError as exc:
            station.ohcfg = None
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "openHop config readable"}

    def _check_api(self) -> CheckResult:
        station = self.station
        if not station.secrets.api_token:
            return {"ok": False, "message": "no API token yet: use Connect on the plugin page"}
        settings = station.settings
        try:
            station.candidates = station.api().repeaters(
                settings.max_age_hours, settings.manual_repeaters
            )
            station.openhop_brokers = station.api().mqtt_status()
        except OpenHopError as exc:
            return {"ok": False, "message": str(exc)}
        direct = sum(n.hops == 0 for n in station.candidates)
        one_hop = sum(n.hops == 1 for n in station.candidates)
        message = f"{direct} repeaters heard direct, {one_hop} one hop away"
        return {"ok": True, "message": message}

    def _check_companion(self) -> CheckResult:
        station = self.station
        name = station.settings.companion_name
        port = companion_port(station.ohcfg, name) if station.ohcfg else None
        if port is None:
            return {"ok": False, "message": f"no companion named {name!r} yet: use Connect"}
        try:
            with CompanionClient(station.companion_host, port) as client:
                station.companion_key = client.public_key.hex()
        except OSError as exc:
            return {"ok": False, "message": f"companion port {port}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - any failure is a failed check
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": f"companion reachable on port {port}"}

    def _check_publish(self) -> CheckResult:
        if not self.station.ohcfg:
            return {"ok": False, "message": "needs openHop config"}
        try:
            publisher.Publisher(self.station.ohcfg)
        except Exception as exc:  # noqa: BLE001 - any failure is a failed check
            return {"ok": False, "message": f"repeater key unusable: {exc}"}
        return {"ok": True, "message": "repeater key loaded"}

    def refresh(self) -> None:
        self.station.check = {
            "config": self._check_config(),
            "api": self._check_api(),
            "companion": self._check_companion(),
            "publish": self._check_publish(),
        }
        self.last_refresh = time.time()

    def connect(self) -> str:
        """Create the plugin's companion identity if openHop has none yet."""
        station = self.station
        if not station.secrets.api_token:
            raise RuntimeError("no API token")
        settings = station.settings
        cfg = load_openhop_config(Path(settings.openhop_config_path))
        if companion_port(cfg, settings.companion_name) is None:
            station.api().create_companion(settings.companion_name, settings.companion_port)
            message = (
                f"created companion {settings.companion_name} on port {settings.companion_port}"
            )
        else:
            message = f"companion {settings.companion_name} already exists"
        self.refresh()
        return message
