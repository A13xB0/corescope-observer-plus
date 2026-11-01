"""This observer's own polling key, registered with CoreScope.

A repeater keeps one ACL entry per key, with one reply route and one replay
counter. On a single shared key observers overwrote each other's route and raced
each other's timestamps, so each polls under its own key and gives CoreScope the
private half to decrypt overheard replies. The key is used for nothing else.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from openhop_core import LocalIdentity
from openhop_core.protocol.identity import Identity

from . import shared_key as sk
from .models import JsonDict

KEY_FILE = "guest_key.json"
REREGISTER_EVERY = 6 * 3600
KEY_LIFETIME = 24 * 3600  # how long CoreScope keeps a key after its last registration


def load_or_create(data_dir: Path) -> sk.SharedKey:
    """Made on first use and kept in data_dir, owner-only."""
    path = Path(data_dir) / KEY_FILE
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        key = sk.SharedKey(
            "own polling key",
            1,
            bytes.fromhex(body["public_key"]),
            bytes.fromhex(body["private_key"]),
        )
        if key.identity().get_public_key() == key.public_key:
            return key
    except (OSError, ValueError, KeyError, sk.SharedKeyError):
        pass
    ident = LocalIdentity()
    key = sk.SharedKey("own polling key", 1, ident.get_public_key(), ident.get_signing_key_bytes())
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(
            {
                "public_key": key.public_key.hex(),
                "private_key": key.private_key.hex(),
                "created_at": int(time.time()),
            },
            f,
        )
    tmp.replace(path)
    return key


def registration_message(observer_id: str, public_key_hex: str, timestamp: int) -> bytes:
    """What the observer signs with its repeater key when registering."""
    parts = ("corescope-guest-key/v1", observer_id.lower(), public_key_hex.lower(), int(timestamp))
    return "|".join(str(p) for p in parts).encode()


def registration_body(
    observer: LocalIdentity, key: sk.SharedKey, now: float | None = None
) -> JsonDict:
    """The POST /api/guest-keys body: the key, signed by this observer."""
    observer_id = observer.get_public_key().hex()
    # Never the repeater's own key: it only signs, which doesn't reveal it.
    if key.public_key == observer.get_public_key():
        raise ValueError("refusing to upload this repeater's own key")
    ts = int(now or time.time())
    msg = registration_message(observer_id, key.public_key.hex(), ts)
    sig = observer.sign(msg)
    # CoreScope checks this too; a key that can't sign verifiably should say so here.
    if not Identity(observer.get_public_key()).verify(msg, sig):
        raise ValueError("this repeater's key doesn't produce a verifiable signature")
    return {
        "observer_id": observer_id,
        "public_key": key.public_key.hex(),
        "private_key": key.private_key.hex(),
        "timestamp": ts,
        "signature": sig.hex(),
    }
