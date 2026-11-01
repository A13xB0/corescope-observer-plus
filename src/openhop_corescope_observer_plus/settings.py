"""The plugin's own settings: data/config.json, edited from the plugin page."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

KEY_RE = re.compile(r"^[0-9a-f]{64}$")
INTERVALS = (30, 60, 180, 360)
STATUS_HOURS = (1, 3, 6, 12, 24)
NODE_INTERVALS = (30, 60, 120, 180, 360, 720, 1440)

DEFAULTS = {
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
    interval_minutes: int = 60  # how often each repeater is polled (telemetry)
    # How often a poll also asks for the full status (stats for the Repeater
    # Status charts; about four times the airtime of telemetry). Never through
    # a relay: one-hop repeaters are asked by telemetry only.
    status_every_hours: int = 6
    targets: list[str] = field(default_factory=list)
    brokers: list[str] = field(default_factory=list)
    max_age_hours: int = 24
    spacing_seconds: int = 20
    temperature_targets: list[str] = field(default_factory=list)  # repeaters asked for temperature
    # Repeaters the operator added by key because no advert of theirs was
    # heard: [{"node_id", "name"}]. Treated as heard direct.
    manual_repeaters: list[dict] = field(default_factory=list)
    corescope_url: str = "https://corescope.scotmesh.net"
    max_hops: int = 0  # 0 = direct neighbours only, 1 = also through one relay
    alternative_routes: bool = False
    # Repeaters on the shared guest key (polled under the key CoreScope
    # serves, if it serves one, and if they have no saved password).
    # Repeaters polled under the guest key: every target, on the default, except
    # what guest_key_overrides sets by hand. Worked out by parse().
    shared_key_targets: list[str] = field(default_factory=list)
    guest_key_default: bool = True
    guest_key_overrides: dict[str, bool] = field(default_factory=dict)
    # Before a shared-key repeater's early retry, ask CoreScope whether it
    # already has a fresh reading (from any observer); if so, skip the retry.
    check_corescope: bool = True
    # The guest password to log in with under the guest key, per repeater:
    # "hello" or "" (blank). Unset = learn it (blank first, then hello). Not a
    # secret: the guest key only ever uses these two, both public.
    guest_passwords: dict = field(default_factory=dict)
    # Per-repeater telemetry interval in minutes, overriding interval_minutes.
    poll_intervals: dict = field(default_factory=dict)
    # Community polling: tell CoreScope which repeaters this observer polls
    # (claims) and stand by when it picks another observer. Off: no claims,
    # owners ignored; CoreScope's limits, check, pause and exclusions still apply.
    community: bool = True

    def interval_for(self, node_id: str) -> int:
        """Minutes between polls of this repeater."""
        return int(self.poll_intervals.get(node_id) or self.interval_minutes)


def parse(raw: dict) -> Settings:
    s = Settings()
    s.openhop_api_url = str(raw.get("openhop_api_url") or s.openhop_api_url)
    s.openhop_config_path = str(raw.get("openhop_config_path") or s.openhop_config_path)
    s.companion_name = str(raw.get("companion_name") or s.companion_name)[:32]
    try:
        s.companion_port = int(raw.get("companion_port") or s.companion_port)
    except (TypeError, ValueError):
        pass
    s.enabled = bool(raw.get("enabled"))
    try:
        interval = int(raw.get("interval_minutes") or 60)
    except (TypeError, ValueError):
        interval = 60
    s.interval_minutes = interval if interval in INTERVALS else max(30, interval)
    try:
        hours = int(raw.get("status_every_hours") or 6)
    except (TypeError, ValueError):
        hours = 6
    s.status_every_hours = hours if hours in STATUS_HOURS else 6
    s.targets = sorted(
        {str(k).lower() for k in raw.get("targets") or [] if KEY_RE.match(str(k).lower())}
    )
    s.brokers = [str(b) for b in raw.get("brokers") or [] if isinstance(b, str)]
    try:
        s.max_age_hours = max(1, min(168, int(raw.get("max_age_hours") or 24)))
    except (TypeError, ValueError):
        pass
    try:
        s.spacing_seconds = max(5, min(300, int(raw.get("spacing_seconds") or 20)))
    except (TypeError, ValueError):
        pass
    s.temperature_targets = sorted(
        {
            str(k).lower()
            for k in raw.get("temperature_targets") or []
            if KEY_RE.match(str(k).lower())
        }
    )
    try:
        s.max_hops = 1 if int(raw.get("max_hops") or 0) >= 1 else 0
    except (TypeError, ValueError):
        pass
    s.alternative_routes = bool(raw.get("alternative_routes"))
    manual = {}
    for m in raw.get("manual_repeaters") or []:
        if not isinstance(m, dict):
            continue
        key = str(m.get("node_id") or "").lower()
        if KEY_RE.match(key):
            manual[key] = {"node_id": key, "name": str(m.get("name") or key[:8])[:64]}
    s.manual_repeaters = [manual[k] for k in sorted(manual)]
    url = str(raw.get("corescope_url") or "").strip().rstrip("/")
    if url.startswith(("http://", "https://")):
        s.corescope_url = url
    elif "corescope_url" in raw and url == "":
        s.corescope_url = ""  # cleared on purpose: no CoreScope (no shared key, no checks)
    s.guest_key_default = bool(raw.get("guest_key_default", True))
    ov = raw.get("guest_key_overrides")
    s.guest_key_overrides = {
        str(k).lower(): bool(v)
        for k, v in (ov.items() if isinstance(ov, dict) else [])
        if KEY_RE.match(str(k).lower())
    }
    s.shared_key_targets = sorted(
        k
        for k in {*s.targets, *s.guest_key_overrides}
        if s.guest_key_overrides.get(k, s.guest_key_default)
    )
    gp = raw.get("guest_passwords")
    s.guest_passwords = {
        str(k).lower(): v
        for k, v in (gp.items() if isinstance(gp, dict) else [])
        if KEY_RE.match(str(k).lower()) and v in ("", "hello")
    }
    pi = raw.get("poll_intervals")
    s.poll_intervals = {}
    for k, v in pi.items() if isinstance(pi, dict) else []:
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if KEY_RE.match(str(k).lower()) and v in NODE_INTERVALS:
            s.poll_intervals[str(k).lower()] = v
    s.check_corescope = _flag(raw.get("check_corescope", True))
    s.community = _flag(raw.get("community", True))
    # A headless run (a test bench) can point at openHop's config without
    # editing config.json.
    env_path = os.environ.get("OPENHOP_CONFIG_PATH", "").strip()
    if env_path:
        s.openhop_config_path = env_path
    return s


def _flag(v) -> bool:
    # A JSON false, or the strings "false"/"0"/"no"/"off" from a form, all mean off.
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def read_raw(data_dir: Path) -> dict:
    path = data_dir / "config.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return value if isinstance(value, dict) else dict(DEFAULTS)


def write_raw(data_dir: Path, raw: dict) -> None:
    path = data_dir / "config.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
