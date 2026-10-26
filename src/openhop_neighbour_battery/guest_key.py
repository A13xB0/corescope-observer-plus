"""This observer's own polling key, registered with CoreScope.

A repeater keeps one ACL entry per key, holding one reply route and one replay
counter. With a single shared key every observer shared that entry: they
overwrote each other's route and their timestamps were compared against each
other. So each observer polls under a key of its own instead, and gives the
private half to CoreScope, which decrypts the replies other observers overhear
and credits them to this observer.

The key is used for nothing else: it is never this repeater's or the
companion's identity, and it only ever logs in blank or with "hello"
(shared_key.build_login enforces that, whatever key is used). CoreScope keeps
it for 24 hours after the last registration, so the plugin re-registers it
every REREGISTER_EVERY seconds while it is polling.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from openhop_core import LocalIdentity
from openhop_core.protocol.identity import Identity

from . import shared_key as sk

KEY_FILE = "guest_key.json"
REREGISTER_EVERY = 6 * 3600
KEY_LIFETIME = 24 * 3600  # how long CoreScope keeps a key after its last registration


def load_or_create(data_dir: Path) -> sk.SharedKey:
    """This observer's polling key, made on first use and kept in data_dir
    (owner-only permissions; it is the key's only copy besides CoreScope's)."""
    path = Path(data_dir) / KEY_FILE
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        key = sk.SharedKey("own polling key", 1, bytes.fromhex(body["public_key"]),
                           bytes.fromhex(body["private_key"]))
        if key.identity().get_public_key() == key.public_key:
            return key
    except (OSError, ValueError, KeyError, sk.SharedKeyError):
        pass
    ident = LocalIdentity()
    key = sk.SharedKey("own polling key", 1, ident.get_public_key(), ident.get_signing_key_bytes())
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"public_key": key.public_key.hex(), "private_key": key.private_key.hex(),
                   "created_at": int(time.time())}, f)
    tmp.replace(path)
    return key


def registration_message(observer_id: str, public_key_hex: str, timestamp: int) -> bytes:
    """What the observer signs with its repeater key when registering."""
    return f"corescope-guest-key/v1|{observer_id.lower()}|{public_key_hex.lower()}|{int(timestamp)}".encode()


def registration_body(observer: LocalIdentity, key: sk.SharedKey, now: float | None = None) -> dict:
    """The POST /api/guest-keys body: the key, signed by this observer."""
    observer_id = observer.get_public_key().hex()
    ts = int(now or time.time())
    msg = registration_message(observer_id, key.public_key.hex(), ts)
    sig = observer.sign(msg)
    # CoreScope checks this signature against the observer's key; check it
    # here first so a key that can't sign verifiably says so plainly.
    if not Identity(observer.get_public_key()).verify(msg, sig):
        raise ValueError("this repeater's key doesn't produce a verifiable signature")
    return {"observer_id": observer_id, "public_key": key.public_key.hex(),
            "private_key": key.private_key.hex(), "timestamp": ts, "signature": sig.hex()}
