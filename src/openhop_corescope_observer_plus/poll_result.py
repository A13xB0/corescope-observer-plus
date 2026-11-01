"""What one poll of a repeater came to."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import JsonDict, Route

OK = "ok"
NO_BATTERY = "no_battery"
NEEDS_PASSWORD = "needs_password"
PASSWORD_REJECTED = "password_rejected"
NO_REPLY = "no_reply"
NOT_DIRECT = "not_direct"
ERROR = "error"
# The guest key was answered as admin: disabled on that repeater, removal drill
# run, never retried under that key automatically.
ADMIN_STOP = "admin_stop"


@dataclass
class PollResult:
    code: str
    battery_mv: int | None = None
    role: str = ""
    detail: str = ""
    stats: JsonDict = field(default_factory=dict)
    route: Route | None = None
    retried: bool = False
    temperature_c: float | None = None
    temperature_code: str = ""  # "" not asked, "ok", "none" (answered without one), "no_reply"
    method: str = "status"  # what the battery came from: "status" or "telemetry"
    key_mode: str = "own"  # "own" (this observer's key) or "shared" (the guest key)
    admin_seen: bool = False  # the guest key was answered as admin
    admin_removed: bool | None = None  # drill result: True ok, False failed, None not run
    covered: bool = False  # we didn't hear it, but CoreScope did (another observer overheard)
    via_corescope: bool = False  # our reply, decrypted from another observer's copy
    alternative: bool = False  # the best route got nothing, so the next route was tried
    reply_s: float | None = None  # seconds from the request going out to its reply
