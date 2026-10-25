"""Shared guest key: build a request, forge the repeater's reply, decode it.

Round-trips against openhop_core's real crypto and packet builder, so nothing
here depends on hardware. The forged reply is exactly what a repeater puts on
the air: a RESPONSE encrypted with the ECDH secret between the repeater and the
shared key, which is what every observer holding the key then decodes.
"""

from __future__ import annotations

import os
import struct

import pytest

openhop_core = pytest.importorskip("openhop_core")

from openhop_core.protocol import CryptoUtils
from openhop_core.protocol.constants import PAYLOAD_TYPE_RESPONSE
from openhop_core.protocol.identity import Identity, LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder

from openhop_neighbour_battery import companion as c
from openhop_neighbour_battery import shared_key as sk


def _identity() -> LocalIdentity:
    # Firmware refuses a key whose public byte is 0x00 or 0xFF (it doubles as the
    # routing hash), so re-roll until the prefix is usable.
    while True:
        lid = LocalIdentity()
        if lid.get_public_key()[0] not in (0x00, 0xFF):
            return lid


def _shared() -> tuple[sk.SharedKey, LocalIdentity]:
    lid = _identity()
    return sk.SharedKey("CoreScope shared guest", 1,
                        lid.get_public_key(), lid.get_signing_key_bytes()), lid


def _forge_reply(repeater: LocalIdentity, shared_pub: bytes, tag: int, body: bytes) -> bytes:
    """A raw RX push (0x88) carrying a RESPONSE a repeater would transmit."""
    secret = Identity(shared_pub).calc_shared_secret(repeater.get_private_key())
    plaintext = tag.to_bytes(4, "little") + body
    cipher = CryptoUtils.encrypt_then_mac(secret[:16], secret, plaintext)
    payload = bytes([shared_pub[0], repeater.get_public_key()[0]]) + cipher
    header = PacketBuilder._create_header(PAYLOAD_TYPE_RESPONSE, "direct")
    pkt = PacketBuilder._create_packet(header, payload)
    return bytes([sk.PUSH_CODE_LOG_RX_DATA, 0x40, 0xC0]) + pkt.write_to()


def _stats_bytes(**over) -> bytes:
    stats = {"battery_mv": 4042, "tx_queue": 0, "noise_floor": -92, "last_rssi": -41,
             "packets_recv": 10, "packets_sent": 8, "tx_air_secs": 3, "uptime_secs": 86400,
             "sent_flood": 1, "sent_direct": 7, "recv_flood": 4, "recv_direct": 6,
             "err_events": 0, "last_snr_x4": 30, "direct_dups": 0, "flood_dups": 2,
             "rx_air_secs": 5, "recv_errors": 0}
    stats.update(over)
    raw = b""
    for name, fmt in c.STATS_FIELDS:
        raw += struct.pack("<" + fmt, int(stats[name]))
    return raw


def test_login_refuses_a_real_password():
    shared, _ = _shared()
    rpub = _identity().get_public_key()
    with pytest.raises(sk.UnsafeSharedPassword):
        sk.build_login(shared, rpub, b"", 0, password="s3cret")


@pytest.mark.parametrize("password", sk.SHARED_PASSWORDS)
def test_login_allows_only_blank_and_hello(password):
    shared, _ = _shared()
    rpub = _identity().get_public_key()
    body, tag = sk.build_login(shared, rpub, b"", 0, password=password)
    assert body[0] == sk.PRIORITY_NORMAL
    assert tag > 0


def test_never_floods_without_a_route():
    shared, _ = _shared()
    rpub = _identity().get_public_key()
    with pytest.raises(sk.SharedKeyError):
        sk.build_status_request(shared, rpub, b"", -1)


def test_status_reply_round_trip():
    shared, _ = _shared()
    repeater = _identity()
    rpub = repeater.get_public_key()
    _body, tag = sk.build_status_request(shared, rpub, b"", 0)
    frame = _forge_reply(repeater, shared.public_key, tag, _stats_bytes())
    reply = sk.decode_rx(frame, shared, [rpub])
    assert reply is not None
    assert reply.tag == tag and reply.repeater_pubkey == rpub
    parsed = c.parse_stats(reply.data)
    assert parsed["battery_mv"] == 4042
    assert parsed["uptime_secs"] == 86400


def test_telemetry_reply_round_trip():
    shared, _ = _shared()
    repeater = _identity()
    rpub = repeater.get_public_key()
    _body, tag = sk.build_telemetry_request(shared, rpub, b"", 0)
    lpp = (bytes([1, c.LPP_VOLTAGE]) + struct.pack(">H", 404)
           + bytes([1, c.LPP_TEMPERATURE]) + struct.pack(">h", 162))
    frame = _forge_reply(repeater, shared.public_key, tag, lpp)
    reply = sk.decode_rx(frame, shared, [rpub])
    assert reply is not None and reply.tag == tag
    assert c.lpp_voltage_mv(reply.data) == 4040
    assert c.lpp_temperature(reply.data) == 16.2


@pytest.mark.parametrize("admin_code,expect_admin", [(0, False), (1, True), (3, False)])
def test_login_reply_admin_detection(admin_code, expect_admin):
    shared, _ = _shared()
    repeater = _identity()
    rpub = repeater.get_public_key()
    _body, tag = sk.build_login(shared, rpub, b"", 0)
    login_struct = bytes([0, 60, admin_code, admin_code]) + os.urandom(4)
    frame = _forge_reply(repeater, shared.public_key, tag, login_struct)
    reply = sk.decode_rx(frame, shared, [rpub])
    assert reply is not None
    result = sk.login_result(reply)
    assert result is not None
    assert result.is_admin is expect_admin


def test_reply_to_a_different_key_is_ignored():
    shared, _ = _shared()
    repeater = _identity()
    other, _ = _shared()
    frame = _forge_reply(repeater, other.public_key, 123, _stats_bytes())
    assert sk.decode_rx(frame, shared, [repeater.get_public_key()]) is None


def test_wrong_repeater_for_the_hash_ruled_out_by_mac():
    # Two repeaters whose keys start with the same hash byte: only the one that
    # actually sent it decrypts; the 2-byte MAC rejects the other.
    shared, _ = _shared()
    sender = _identity()
    hashbyte = sender.get_public_key()[0]
    while True:
        other = _identity()
        if other.get_public_key()[0] == hashbyte:
            break
    _body, tag = sk.build_status_request(shared, sender.get_public_key(), b"", 0)
    frame = _forge_reply(sender, shared.public_key, tag, _stats_bytes())
    # Offer only the wrong repeater that shares the hash: no decode.
    assert sk.decode_rx(frame, shared, [other.get_public_key()]) is None
    # Offer both: the real sender decodes.
    reply = sk.decode_rx(frame, shared, [other.get_public_key(), sender.get_public_key()])
    assert reply is not None and reply.repeater_pubkey == sender.get_public_key()


def test_route_update_builds_a_direct_path_packet():
    from openhop_core.protocol.constants import PAYLOAD_TYPE_PATH
    from openhop_core.protocol.packet import Packet
    shared, _ = _shared()
    rpub = _identity().get_public_key()
    body = sk.build_route_update(shared, rpub, b"", 0)
    assert body[0] == sk.PRIORITY_NORMAL
    pkt = Packet()
    assert pkt.read_from(body[1:])
    assert pkt.get_payload_type() == PAYLOAD_TYPE_PATH
    # Sent direct (route type 2), not flood, so it stays off the wider mesh.
    assert pkt.get_route_type() == 2


def test_route_update_refuses_to_flood():
    shared, _ = _shared()
    rpub = _identity().get_public_key()
    with pytest.raises(sk.SharedKeyError):
        sk.build_route_update(shared, rpub, b"", -1)


def test_admin_removal_builds_the_setperm_command():
    shared, _ = _shared()
    rpub = _identity().get_public_key()
    body = sk.build_admin_removal(shared, rpub, b"", 0)
    assert body[0] == sk.PRIORITY_NORMAL
    assert len(body) > 1
