"""The plugin's own settings: data/config.json, edited from the plugin page."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import KEY_RE, JsonDict, ManualRepeater

INTERVALS = (30, 60, 180, 360)
STATUS_HOURS = (1, 3, 6, 12, 24)
NODE_INTERVALS = (30, 60, 120, 180, 360, 720, 1440)

DEFAULTS: JsonDict = {
    "openhop_api_url": "http://127.0.0.1:8000",
    "openhop_config_path": "/etc/openhop_repeater/config.yaml",
    "companion_name": "NeighbourBattery",
    "companion_port": 5003,
    "enabled": False,
    "interval_minutes": 60,
    "status_every_hours": 6,
    "targets": [],
    "brokers": [],
    "max_age_hours": 24,
    "spacing_seconds": 20,
    "temperature_targets": [],
    "manual_repeaters": [],
    "corescope_url": "https://corescope.scotmesh.net",
    "max_hops": 0,
    "alternative_routes": False,
    "guest_key_default": True,
    "guest_key_overrides": {},
    "check_corescope": True,
    "guest_passwords": {},
    "poll_intervals": {},
    "community": True,
}

# Written by the plugin page, consumed and cleared by the plugin. Secrets go
# straight to secrets.json so they never stay in config.json.
TRANSIENT_KEYS = ("api_token_input", "password_updates", "requests")


@dataclass
class Settings:
    openhop_api_url: str = DEFAULTS["openhop_api_url"]
    openhop_config_path: str = DEFAULTS["openhop_config_path"]
    companion_name: str = DEFAULTS["companion_name"]
    companion_port: int = DEFAULTS["companion_port"]
    enabled: bool = False
    interval_minutes: int = 60
    status_every_hours: int = 6  # never through a relay: one-hop repeaters get telemetry only
    targets: list[str] = field(default_factory=list)
    brokers: list[str] = field(default_factory=list)
    max_age_hours: int = 24
    spacing_seconds: int = 20
    temperature_targets: list[str] = field(default_factory=list)
    manual_repeaters: list[ManualRepeater] = field(default_factory=list)
    corescope_url: str = "https://corescope.scotmesh.net"
    max_hops: int = 0  # 0 = direct neighbours only, 1 = also through one relay
    alternative_routes: bool = False
    # Polled under the guest key: every target on the default, except what
    # guest_key_overrides sets by hand. Worked out by parse().
    shared_key_targets: list[str] = field(default_factory=list)
    guest_key_default: bool = True
    guest_key_overrides: dict[str, bool] = field(default_factory=dict)
    check_corescope: bool = True
    # Per repeater, "hello" or "" (blank); unset means learn it. Not secret.
    guest_passwords: dict[str, str] = field(default_factory=dict)
    poll_intervals: dict[str, int] = field(default_factory=dict)  # minutes, per repeater
    # Off: no claims and owners ignored; CoreScope's limits, check, pause and
    # exclusions still apply.
    community: bool = True

    def interval_for(self, node_id: str) -> int:
        """Minutes between polls of this repeater."""
        return int(self.poll_intervals.get(node_id) or self.interval_minutes)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flag(value: Any) -> bool:
    # A JSON false, or the strings "false"/"0"/"no"/"off" from a form, all mean off.
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _keys(values: Any) -> list[str]:
    return sorted({str(k).lower() for k in values or [] if KEY_RE.match(str(k).lower())})


def _key_items(value: Any) -> list[tuple[str, Any]]:
    """A dict's items with valid repeater keys, lowercased."""
    items = value.items() if isinstance(value, dict) else []
    return [(str(k).lower(), v) for k, v in items if KEY_RE.match(str(k).lower())]


def _manual_repeaters(values: Any) -> list[ManualRepeater]:
    manual: dict[str, ManualRepeater] = {}
    for m in values or []:
        if not isinstance(m, dict):
            continue
        key = str(m.get("node_id") or "").lower()
        if KEY_RE.match(key):
            manual[key] = {"node_id": key, "name": str(m.get("name") or key[:8])[:64]}
    return [manual[k] for k in sorted(manual)]


def _poll_intervals(value: Any) -> dict[str, int]:
    out = {}
    for key, minutes in _key_items(value):
        minutes = _int(minutes, 0)
        if minutes in NODE_INTERVALS:
            out[key] = minutes
    return out


def parse(raw: JsonDict) -> Settings:
    s = Settings()
    s.openhop_api_url = str(raw.get("openhop_api_url") or s.openhop_api_url)
    s.openhop_config_path = str(raw.get("openhop_config_path") or s.openhop_config_path)
    s.companion_name = str(raw.get("companion_name") or s.companion_name)[:32]
    s.companion_port = _int(raw.get("companion_port") or s.companion_port, s.companion_port)
    s.enabled = bool(raw.get("enabled"))
    interval = _int(raw.get("interval_minutes") or 60, 60)
    s.interval_minutes = interval if interval in INTERVALS else max(30, interval)
    hours = _int(raw.get("status_every_hours") or 6, 6)
    s.status_every_hours = hours if hours in STATUS_HOURS else 6
    s.targets = _keys(raw.get("targets"))
    s.brokers = [str(b) for b in raw.get("brokers") or [] if isinstance(b, str)]
    s.max_age_hours = max(1, min(168, _int(raw.get("max_age_hours") or 24, 24)))
    s.spacing_seconds = max(5, min(300, _int(raw.get("spacing_seconds") or 20, 20)))
    s.temperature_targets = _keys(raw.get("temperature_targets"))
    s.max_hops = 1 if _int(raw.get("max_hops") or 0, 0) >= 1 else 0
    s.alternative_routes = bool(raw.get("alternative_routes"))
    s.manual_repeaters = _manual_repeaters(raw.get("manual_repeaters"))
    url = str(raw.get("corescope_url") or "").strip().rstrip("/")
    if url.startswith(("http://", "https://")):
        s.corescope_url = url
    elif "corescope_url" in raw and url == "":
        s.corescope_url = ""  # cleared on purpose: no CoreScope (no shared key, no checks)
    s.guest_key_default = bool(raw.get("guest_key_default", True))
    s.guest_key_overrides = {k: bool(v) for k, v in _key_items(raw.get("guest_key_overrides"))}
    s.shared_key_targets = sorted(
        k
        for k in {*s.targets, *s.guest_key_overrides}
        if s.guest_key_overrides.get(k, s.guest_key_default)
    )
    s.guest_passwords = {
        k: v for k, v in _key_items(raw.get("guest_passwords")) if v in ("", "hello")
    }
    s.poll_intervals = _poll_intervals(raw.get("poll_intervals"))
    s.check_corescope = _flag(raw.get("check_corescope", True))
    s.community = _flag(raw.get("community", True))
    # A headless bench can point at openHop's config without editing config.json.
    env_path = os.environ.get("OPENHOP_CONFIG_PATH", "").strip()
    if env_path:
        s.openhop_config_path = env_path
    return s


def read_raw(data_dir: Path) -> JsonDict:
    path = data_dir / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return value if isinstance(value, dict) else dict(DEFAULTS)


def write_raw(data_dir: Path, raw: JsonDict) -> None:
    path = data_dir / "config.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
