"""ISO 8601 UTC timestamps, as CoreScope and the plugin page use them."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_iso(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts or time.time(), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
