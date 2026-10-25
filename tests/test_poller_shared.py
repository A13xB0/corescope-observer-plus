"""poll_one_shared end to end against a fake repeater, with real crypto.

The fake repeater decrypts each raw packet the poller sends under the shared
key, and forges exactly the reply a real repeater would put on the air (a
RESPONSE encrypted back to the shared key, wrapped in a 0x88 raw-RX push). No
TCP and no openHop, but every byte on the wire is built and verified by the
real packet builder and crypto, including the admin-removal drill.
"""

from __future__ import annotations

import os
import struct

import pytest

pytest.importorskip("openhop_core")

from openhop_core.protocol import CryptoUtils
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ANON_REQ, PAYLOAD_TYPE_REQ, PAYLOAD_TYPE_RESPONSE, PAYLOAD_TYPE_TXT_MSG)
from openhop_core.protocol.identity import Identity, LocalIdentity
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.packet_builder import PacketBuilder

from openhop_neighbour_battery import companion as c
from openhop_neighbour_battery import poller
from openhop_neighbour_battery import shared_key as sk
from openhop_neighbour_battery.openhop import Neighbour, Route


def _identity() -> LocalIdentity:
    while True:
        lid = LocalIdentity()
        if lid.get_public_key()[0] not in (0x00, 0xFF):
            return lid


def _stats_bytes(battery_mv: int) -> bytes:
    vals = {"battery_mv": battery_mv, "tx_queue": 0, "noise_floor": -92, "last_rssi": -41,
            "packets_recv": 10, "packets_sent": 8, "tx_air_secs": 3, "uptime_secs": 7200,
            "sent_flood": 1, "sent_direct": 7, "recv_flood": 4, "recv_direct": 6,
            "err_events": 0, "last_snr_x4": 30, "direct_dups": 0, "flood_dups": 2,
            "rx_air_secs": 5, "recv_errors": 0}
    return b"".join(struct.pack("<" + fmt, int(vals[name])) for name, fmt in c.STATS_FIELDS)


class FakeRepeater:
    """A repeater that answers the shared key off the air.

    ``admin`` makes the first login answer as admin, as if someone had once
    logged in with its admin password under the shared key. A ``setperm 0`` CLI
    command then clears it, so the verifying login comes back guest.
    """

    def __init__(self, shared: sk.SharedKey, *, admin=False, answer_status=True,
                 answer_telemetry=True, answer_login=True, battery_mv=4012):
        self.id = _identity()
        self.pub = self.id.get_public_key()
        self.shared = shared
        self.secret = Identity(shared.public_key).calc_shared_secret(self.id.get_private_key())
        self.admin = admin
        self.answer_status = answer_status
        self.answer_telemetry = answer_telemetry
        self.answer_login = answer_login
        self.battery_mv = battery_mv
        self.cli_commands: list[str] = []
        self._outbox: list[bytes] = []

    # -- CompanionClient surface the poller uses ---------------------------

    def send_raw_packet(self, body: bytes) -> bool:
        assert body[0] == sk.PRIORITY_NORMAL
        pkt = Packet()
        assert pkt.read_from(body[1:])
        self._handle(pkt)
        return True

    def collect_rx(self, window: float):
        out, self._outbox = self._outbox, []
        yield from out

    # -- the repeater's behaviour ------------------------------------------

    def _reply(self, tag: int, payload_body: bytes) -> None:
        plaintext = tag.to_bytes(4, "little") + payload_body
        cipher = CryptoUtils.encrypt_then_mac(self.secret[:16], self.secret, plaintext)
        payload = bytes([self.shared.public_key[0], self.pub[0]]) + cipher
        header = PacketBuilder._create_header(PAYLOAD_TYPE_RESPONSE, "direct")
        pkt = PacketBuilder._create_packet(header, payload)
        self._outbox.append(bytes([sk.PUSH_CODE_LOG_RX_DATA, 0x40, 0xC0]) + pkt.write_to())

    def _handle(self, pkt: Packet) -> None:
        ptype = pkt.get_payload_type()
        payload = pkt.get_payload()
        if ptype == PAYLOAD_TYPE_ANON_REQ:
            plaintext = CryptoUtils.mac_then_decrypt(self.secret[:16], self.secret, payload[33:])
            tag = int.from_bytes(plaintext[:4], "little")
            if self.answer_login:
                admin_code = 1 if self.admin else 0
                self._reply(tag, bytes([0, 60, admin_code, admin_code]) + os.urandom(4))
            return
        if ptype == PAYLOAD_TYPE_REQ:
            plaintext = CryptoUtils.mac_then_decrypt(self.secret[:16], self.secret, payload[2:])
            tag, code = int.from_bytes(plaintext[:4], "little"), plaintext[4]
            if code == c.REQ_TYPE_GET_STATUS and self.answer_status:
                self._reply(tag, _stats_bytes(self.battery_mv))
            elif code == sk.REQ_TYPE_GET_TELEMETRY_DATA and self.answer_telemetry:
                self._reply(tag, bytes([1, c.LPP_VOLTAGE]) + struct.pack(">H", self.battery_mv // 10)
                            + bytes([1, c.LPP_TEMPERATURE]) + struct.pack(">h", 155))
            return
        if ptype == PAYLOAD_TYPE_TXT_MSG:
            plaintext = CryptoUtils.mac_then_decrypt(self.secret[:16], self.secret, payload[2:])
            # plaintext is timestamp(4) + flags(1) + the CLI text.
            text = plaintext[5:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            self.cli_commands.append(text)
            if text.startswith("setperm") and text.endswith(" 0"):
                self.admin = False  # the grant is gone; the next login is a guest


@pytest.fixture
def shared():
    lid = _identity()
    return sk.SharedKey("CoreScope shared guest", 1,
                        lid.get_public_key(), lid.get_signing_key_bytes())


def _neighbour(rep: FakeRepeater, zero_hop=True) -> Neighbour:
    return Neighbour(node_id=rep.pub.hex(), name="West Lomond", snr=-5.0, rssi=-110,
                     last_seen=0, zero_hop=zero_hop)


DIRECT = Route(adverts=9, snr=-5.0)
VIA = Route(relay_id="02f1fe" + "aa" * 29, relay_name="West Lomond", hash_size=3, adverts=4, snr=-3.0)


def test_direct_status_under_shared_key(shared):
    rep = FakeRepeater(shared, battery_mv=4012)
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, {}, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.OK
    assert res.battery_mv == 4012
    assert res.key_mode == "shared"
    assert res.method == "status"


def test_one_hop_uses_telemetry_under_shared_key(shared):
    rep = FakeRepeater(shared, battery_mv=4040)
    res = poller.poll_one_shared(rep, _neighbour(rep, zero_hop=False), shared, {},
                                 routes=[VIA], read_temperature=True, sleep=lambda s: None)
    assert res.code == poller.OK
    assert res.method == "telemetry"
    assert res.battery_mv == 4040
    assert res.temperature_c == 15.5


def test_no_reply_under_shared_key(shared):
    rep = FakeRepeater(shared, answer_status=False, answer_login=False)
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, {}, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.NO_REPLY
    assert res.key_mode == "shared"


def test_admin_answer_stops_and_runs_removal_drill(shared):
    rep = FakeRepeater(shared, admin=True)
    state: dict = {}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.ADMIN_STOP
    assert res.admin_seen is True
    assert res.admin_removed is True  # the drill cleared it and the re-login was guest
    assert res.battery_mv is None  # the battery is never read when the key is admin
    # The removal command was the shared key's own setperm 0, and nothing else.
    assert len(rep.cli_commands) == 1
    assert rep.cli_commands[0] == f"setperm {shared.public_key.hex()} 0"
    # record() should mark the node so it is not polled under the key again.
    poller.record(state, res, password_set=False, now=1000.0)
    assert state["shared_admin_seen"] is True
    assert state["shared_admin_removed"] is True


def test_admin_removal_drill_reports_failure_when_still_admin(shared):
    # A repeater that keeps answering admin (the setperm never takes): the drill
    # fails, and that is reported so the owner is told to act.
    rep = FakeRepeater(shared, admin=True)
    rep._handle_orig = rep._handle

    def stubborn(pkt):
        rep._handle_orig(pkt)
        rep.admin = True  # refuse to give up admin
    rep._handle = stubborn
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, {}, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.ADMIN_STOP
    assert res.admin_removed is False


def test_reading_marks_shared_key(shared):
    rep = FakeRepeater(shared, battery_mv=4012)
    n = _neighbour(rep)
    res = poller.poll_one_shared(rep, n, shared, {}, routes=[DIRECT], sleep=lambda s: None)
    payload = poller.reading(n, res, 1000.0)
    assert payload["key"] == "shared"
    assert payload["battery_mv"] == 4012
