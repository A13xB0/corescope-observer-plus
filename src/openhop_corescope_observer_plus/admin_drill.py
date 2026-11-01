"""Undoing an admin grant on the guest key."""

from __future__ import annotations

from . import packet_log
from . import shared_key as sk
from .companion import CompanionClient, CompanionError
from .models import JsonDict
from .poll_common import RETRY_GAP_SECONDS, SleepFn, describe_login

SETTLE_SECONDS = 10.0  # let the setperm take before the verifying login
VERIFY_TRIES = 3
REPLY_WINDOW = 20.0


def _key_is_demoted(
    client: CompanionClient,
    shared: sk.SharedKey,
    key: bytes,
    log: packet_log.NodeLog,
    entry: JsonDict,
) -> bool:
    """Listen for the verifying login's answer: non-admin confirms the removal."""
    for frame in client.collect_rx(REPLY_WINDOW):
        reply = sk.decode_rx(frame, shared, [key])
        if reply is None:
            continue
        result = sk.login_result(reply)
        if result is not None:
            log.answered(entry, "radio", flooded=reply.flooded, what=describe_login(result))
        if result is not None and not result.is_admin:
            return True
    return False


def remove_admin(
    client: CompanionClient,
    shared: sk.SharedKey,
    key: bytes,
    out_path: bytes,
    plen: int,
    sleep: SleepFn,
    log: packet_log.NodeLog = packet_log.NULL,
    route_label: str = "",
) -> bool:
    """Send ``setperm <key> 0``, then log in blank to confirm the key is no longer admin.

    The command is admin-only, so it works because the key still holds the
    rights it is giving up. True once a verifying login comes back non-admin.
    """
    try:
        e = log.sent("admin removal", route_label, "setperm <key> 0")
        client.send_raw_packet(sk.build_admin_removal(shared, key, out_path, plen))
        log.none_expected(e)
    except (CompanionError, OSError, sk.SharedKeyError):
        return False
    sleep(SETTLE_SECONDS)
    # A one-hop repeater's return path is back through the relay; a direct
    # neighbour's is empty.
    return_path, return_len = (out_path, plen) if out_path else (b"", None)
    for attempt in range(VERIFY_TRIES):
        if attempt:
            sleep(RETRY_GAP_SECONDS)
        try:
            e = log.sent("login", route_label, "blank password (verifying the removal)")
            client.send_raw_packet(sk.build_login(shared, key, out_path, plen, "")[0])
            # setperm 0 deleted the ACL entry and its return path, so the route
            # goes again or the verifying reply floods and we miss it.
            log.none_expected(log.sent("route", route_label, "after the removal"))
            client.send_raw_packet(
                sk.build_route_update(
                    shared, key, out_path, plen, return_path=return_path, return_path_len=return_len
                )
            )
        except (CompanionError, OSError, sk.SharedKeyError, sk.UnsafeSharedPassword):
            continue
        if _key_is_demoted(client, shared, key, log, e):
            return True
        log.unanswered(e)
    return False
