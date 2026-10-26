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
    PAYLOAD_TYPE_ANON_REQ, PAYLOAD_TYPE_PATH, PAYLOAD_TYPE_REQ, PAYLOAD_TYPE_RESPONSE, PAYLOAD_TYPE_TXT_MSG)
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
                 answer_telemetry=True, answer_login=True, battery_mv=4012, require_login=False,
                 deaf=False):
        self.id = _identity()
        self.pub = self.id.get_public_key()
        self.shared = shared
        self.secret = Identity(shared.public_key).calc_shared_secret(self.id.get_private_key())
        self.admin = admin
        self.answer_status = answer_status
        self.answer_telemetry = answer_telemetry
        self.answer_login = answer_login
        self.battery_mv = battery_mv
        # require_login models the firmware ACL: a data request is only answered
        # once a login has added the key to the guest ACL. logins counts them.
        self.require_login = require_login
        self.logins = 0
        self._in_acl = False
        self.cli_commands: list[str] = []
        self._outbox: list[bytes] = []
        # Like the firmware's ClientACL: no route back until a PATH packet
        # arrives while the key is in the ACL; until then replies flood.
        self.route_known = False
        self.paths_taught = 0
        # deaf: our radio hears nothing, but other observers do — every reply
        # also lands in ``air`` (raw wire bytes), which is what CoreScope serves.
        self.deaf = deaf
        self.air: list[bytes] = []
        self._clk = 1_700_000_000  # the repeater's own clock, distinct from request tags

    def _clock(self) -> int:
        self._clk += 1
        return self._clk

    # -- CompanionClient surface the poller uses ---------------------------

    def send_raw_packet(self, body: bytes) -> bool:
        assert body[0] == sk.PRIORITY_NORMAL
        pkt = Packet()
        assert pkt.read_from(body[1:])
        self._handle(pkt)
        return True

    def clear_rx_backlog(self) -> None:
        pass

    def collect_rx(self, window: float, until=None):
        out, self._outbox = self._outbox, []
        yield from out

    # -- the repeater's behaviour ------------------------------------------

    def _reply(self, tag: int, payload_body: bytes) -> None:
        plaintext = tag.to_bytes(4, "little") + payload_body
        cipher = CryptoUtils.encrypt_then_mac(self.secret[:16], self.secret, plaintext)
        payload = bytes([self.shared.public_key[0], self.pub[0]]) + cipher
        header = PacketBuilder._create_header(PAYLOAD_TYPE_RESPONSE,
                                              "direct" if self.route_known else "flood")
        pkt = PacketBuilder._create_packet(header, payload)
        self.air.append(pkt.write_to())
        if not self.deaf:
            self._outbox.append(bytes([sk.PUSH_CODE_LOG_RX_DATA, 0x40, 0xC0]) + pkt.write_to())

    def _handle(self, pkt: Packet) -> None:
        ptype = pkt.get_payload_type()
        payload = pkt.get_payload()
        if ptype == PAYLOAD_TYPE_PATH:
            if self._in_acl:  # searchPeersByHash only finds keys in the ACL
                self.route_known = True
                self.paths_taught += 1
            return
        if ptype == PAYLOAD_TYPE_ANON_REQ:
            self.logins += 1
            self._in_acl = True
            if self.answer_login:
                admin_code = 1 if self.admin else 0
                # Real firmware puts the repeater's OWN clock first in a login
                # reply, not the request's tag (MyMesh.cpp), so the plugin can't
                # match it by tag and must tell it by shape. Use a clock value
                # unrelated to any request tag here to prove that path.
                self._reply(self._clock(), bytes([0, 60, admin_code, admin_code]) + os.urandom(4))
            return
        if ptype == PAYLOAD_TYPE_REQ:
            plaintext = CryptoUtils.mac_then_decrypt(self.secret[:16], self.secret, payload[2:])
            tag, code = int.from_bytes(plaintext[:4], "little"), plaintext[4]
            if self.require_login and not self._in_acl:
                return  # not in the ACL yet: a bare request is ignored
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


def test_skips_login_when_already_in_acl(shared):
    # Believed in the ACL and the admin re-check isn't due: poll with a bare
    # request, no login — saving the login's airtime.
    rep = FakeRepeater(shared, battery_mv=4012)
    now = 2_000_000.0
    state = {"acl_key": shared.public_key.hex(), "logged_in_at": now - 60, "login_checked_at": now - 60}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now)
    assert res.code == poller.OK
    assert rep.logins == 0


def test_logs_in_when_acl_entry_is_gone(shared):
    # The ACL entry has lapsed (a bare request is ignored): the poll notices the
    # silence and logs in to be re-added, then reads the battery.
    rep = FakeRepeater(shared, require_login=True, battery_mv=4012)
    now = 2_000_000.0
    state = {"acl_key": shared.public_key.hex(), "logged_in_at": now - 60, "login_checked_at": now - 60}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now)
    assert res.code == poller.OK
    assert rep.logins >= 1


def _antenna(rep: FakeRepeater, asked: list | None = None):
    def antenna(since):
        if asked is not None:
            asked.append(since)
        return list(rep.air)
    return antenna


def test_reply_heard_only_by_other_observers_is_taken_from_corescope(shared):
    # We don't hear the reply, another observer does: the poll decrypts it from
    # CoreScope's packets and takes the reading — no login, no re-ask.
    rep = FakeRepeater(shared, battery_mv=4010, deaf=True)
    rep._in_acl = True
    now = 2_000_000.0
    state = {"acl_key": shared.public_key.hex(), "logged_in_at": now - 60, "login_checked_at": now - 60,
             "status_checked_at": now}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now, antenna=_antenna(rep))
    assert res.code == poller.OK and res.battery_mv == 4010
    assert res.via_corescope is True
    assert rep.logins == 0
    poller.record(state, res, password_set=False, now=now)
    assert state["via_corescope"] is True and state["battery_mv"] == 4010


def test_login_answer_heard_only_by_other_observers_stops_more_logins(shared, monkeypatch):
    # The login was answered but only other observers heard it: one login, then
    # the route and the request — never a second login.
    rep = FakeRepeater(shared, battery_mv=4012, deaf=True, require_login=True)
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, {"status_checked_at": 2e6}, routes=[DIRECT],
                                 sleep=lambda s: None, now=2e6, antenna=_antenna(rep))
    assert res.code == poller.OK
    assert rep.logins == 1
    assert rep.paths_taught == 1


def test_route_goes_before_a_no_login_request(shared, monkeypatch):
    # In the ACL: the route goes first, so the reply comes back direct even if
    # the repeater lost it (a reboot) — and nothing floods.
    rep = FakeRepeater(shared, battery_mv=4012)
    rep._in_acl = True
    now = 2_000_000.0
    state = {"acl_key": shared.public_key.hex(), "logged_in_at": now - 60, "login_checked_at": now - 60}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now)
    assert res.code == poller.OK and rep.logins == 0
    assert rep.paths_taught == 1
    assert "flood_replies" not in state


def test_flooded_login_answer_waits_before_the_route(shared):
    # A new key's first login answer floods; the route is sent only once that
    # flood has had time to clear, then the reading comes back direct.
    rep = FakeRepeater(shared, battery_mv=4012)
    slept = []
    state: dict = {}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=slept.append)
    assert res.code == poller.OK
    assert poller.FLOOD_CLEAR_SECONDS in slept
    assert rep.route_known and "flood_replies" not in state


def test_replies_that_keep_flooding_drop_to_daily(shared):
    # The route never takes (lost every time): the replies keep flooding, so
    # after two such polls the repeater is polled only daily.
    rep = FakeRepeater(shared, battery_mv=4012)
    rep._handle_orig = rep._handle

    def lose_paths(pkt):
        if pkt.get_payload_type() != PAYLOAD_TYPE_PATH:
            rep._handle_orig(pkt)
    rep._handle = lose_paths
    state: dict = {}
    now = 2_000_000.0
    for i in range(2):
        res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                     sleep=lambda s: None, now=now + i)
        poller.record(state, res, password_set=False, now=now + i)
    assert state["flood_replies"] == 2
    assert not poller.is_due(state, 3600, False, now + 2 * 3600)
    assert poller.is_due(state, 3600, False, now + 25 * 3600)


def test_telemetry_reply_is_never_read_as_a_login(shared):
    # CayenneLPP: channel 1, voltage, high byte 1 for 2.56-5.11 V — the shape
    # of an admin login answer if only codes 0/1 were checked (Earl, 26 Oct).
    lpp = bytes([1, c.LPP_VOLTAGE]) + struct.pack(">H", 410) + bytes([1, c.LPP_TEMPERATURE, 0, 50])
    assert sk.login_result(sk.Reply(b"\x00" * 32, 1, lpp)) is None
    assert sk.login_result(sk.Reply(b"\x00" * 32, 1, bytes([0, 0, 1, 3]) + b"\x00" * 8)).is_admin


def test_admin_recheck_forces_a_login(shared):
    # In the ACL, but the periodic admin re-check is overdue: a login still goes
    # out, so a key quietly made admin is still caught.
    rep = FakeRepeater(shared, admin=True, battery_mv=4012)
    now = 2_000_000.0
    state = {"acl_key": shared.public_key.hex(), "logged_in_at": now - 60, "login_checked_at": now - (25 * 3600)}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now)
    assert res.code == poller.ADMIN_STOP
    assert rep.logins >= 1


def test_blank_success_is_not_remembered(shared):
    # A blank login can work only because another observer's "hello" login put
    # the shared key in the ACL, so it proves nothing: don't remember blank.
    rep = FakeRepeater(shared, battery_mv=4012)
    state: dict = {}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.OK
    assert "shared_password" not in state


def test_known_hello_is_sent_alone(shared, monkeypatch):
    sent = []
    real = poller.sk.build_login
    monkeypatch.setattr(poller.sk, "build_login", lambda *a, **k: (sent.append(a[4]), real(*a, **k))[1])
    rep = FakeRepeater(shared, battery_mv=4012)
    state = {"acl_key": shared.public_key.hex(), "shared_password": "hello"}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.OK
    assert sent == ["hello"]  # never a blank try for a repeater known to want hello (answered first time)


def test_hello_tried_after_blank_unanswered(shared, monkeypatch):
    # Unknown repeater whose guest password is "hello": the blank login gets no
    # answer, so "hello" goes next — and is remembered.
    sent = []
    real = poller.sk.build_login
    monkeypatch.setattr(poller.sk, "build_login", lambda *a, **k: (sent.append(a[4]), real(*a, **k))[1])
    rep = FakeRepeater(shared, battery_mv=4012, require_login=True)
    first = {"done": False}
    orig = rep._handle

    def guarded(pkt):  # ignore the first (blank) login entirely, like the firmware
        if pkt.get_payload_type() == PAYLOAD_TYPE_ANON_REQ and not first["done"]:
            first["done"] = True
            return
        orig(pkt)
    rep._handle = guarded
    state: dict = {}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.OK
    assert sent == ["", "hello"]
    assert state["shared_password"] == "hello"


def test_unanswered_login_is_not_followed_by_a_request(shared):
    # Nobody heard the login answered (not us, not CoreScope): it wasn't
    # accepted, so no request follows it, and the admin re-check stays due.
    rep = FakeRepeater(shared, battery_mv=4012, answer_login=False, require_login=True)
    state: dict = {}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.NO_REPLY
    assert "login_checked_at" not in state
    assert rep.logins == 2  # blank, then hello: one each


def test_route_retaught_after_unanswered_fast_path(shared, monkeypatch):
    taught = []
    real = poller.sk.build_route_update
    monkeypatch.setattr(poller.sk, "build_route_update", lambda *a, **k: (taught.append(1), real(*a, **k))[1])
    rep = FakeRepeater(shared, battery_mv=4012, require_login=True)  # bare request ignored
    now = 2_000_000.0
    state = {"acl_key": shared.public_key.hex(), "logged_in_at": now - 60, "login_checked_at": now - 60}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now)
    assert res.code == poller.OK
    assert taught, "the login after a lapsed ACL entry must re-teach the return path"


def test_any_answer_stops_the_backoff_clock():
    st = {"unheard_since": 1.0}
    poller.record(st, poller.PollResult(poller.NEEDS_PASSWORD), password_set=False, now=100.0)
    assert "unheard_since" not in st


def test_shared_uses_telemetry_between_status_reads(shared):
    rep = FakeRepeater(shared, battery_mv=4012)
    now = 2_000_000.0
    state = {"status_checked_at": now - 3600}  # status read an hour ago
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, now=now)
    assert res.code == poller.OK and res.method == "telemetry"
    state2 = {"status_checked_at": now - 7 * 3600}
    res2 = poller.poll_one_shared(rep, _neighbour(rep), shared, state2, routes=[DIRECT],
                                  sleep=lambda s: None, now=now)
    assert res2.method == "status" and state2["status_checked_at"] == now


def test_unconfirmed_send_keeps_listening(shared):
    # openHop didn't confirm the send in time (it was busy), but the packet went
    # out: the poll carries on and reads the reply instead of failing.
    rep = FakeRepeater(shared, battery_mv=4012)
    real = rep.send_raw_packet

    def slow(body):
        real(body)
        return False
    rep.send_raw_packet = slow
    steps = []
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, {}, routes=[DIRECT],
                                 sleep=lambda s: None, on_step=steps.append)
    assert res.code == poller.OK
    assert any("busy" in s for s in steps)



def test_key_change_starts_the_acl_state_over(shared):
    # State learnt under the shared key means nothing for this observer's own key.
    rep = FakeRepeater(shared, battery_mv=4012)
    state = {"acl_key": "ab" * 32, "logged_in_at": 1.0, "login_checked_at": 1.0, "shared_password": "hello"}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.OK
    assert state["acl_key"] == shared.public_key.hex()
    assert rep.logins >= 1  # logged in afresh rather than trusting the old entry
    assert "shared_password" not in state or state["shared_password"] != "hello"


def test_own_key_remembers_a_blank_login(shared):
    rep = FakeRepeater(shared, battery_mv=4012)
    state: dict = {}
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, state, routes=[DIRECT],
                                 sleep=lambda s: None, personal=True)
    assert res.code == poller.OK and state["shared_password"] == ""
    assert poller.shared_password_order(state, 0, personal=True) == [""]
    # The shared key never trusts a blank success; a first contact tries each once.
    assert poller.shared_password_order(state, 0, personal=False) == ["", "hello"]
    # Failing: one login per poll, blank and hello on alternate days.
    assert poller.shared_password_order({"key_login_fails": 2}, 0) == [""]
    assert poller.shared_password_order({"key_login_fails": 2}, 86400) == ["hello"]
    assert poller.shared_password_order({"shared_password": "hello"}, 0) == ["hello"]



def test_admin_removal_builds_for_a_one_hop_route(shared):
    # Earl, 26 Oct: the removal command couldn't be built for a relayed route.
    rep = FakeRepeater(shared)
    body = sk.build_admin_removal(shared, rep.pub, VIA.path, poller.path_len_byte(VIA.path, VIA.hash_size))
    assert body[0] == sk.PRIORITY_NORMAL


def test_admin_answer_recorded_even_if_removal_fails(shared, monkeypatch):
    rep = FakeRepeater(shared, admin=True)

    def broken(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(poller, "_admin_removal_drill", broken)
    res = poller.poll_one_shared(rep, _neighbour(rep), shared, {}, routes=[DIRECT], sleep=lambda s: None)
    assert res.code == poller.ADMIN_STOP and res.admin_seen is True and res.admin_removed is False


def test_admin_drill_over_one_hop(shared):
    rep = FakeRepeater(shared, admin=True)
    res = poller.poll_one_shared(rep, _neighbour(rep, zero_hop=False), shared, {}, routes=[VIA], sleep=lambda s: None)
    assert res.code == poller.ADMIN_STOP and res.admin_removed is True
    assert rep.cli_commands == [f"setperm {shared.public_key.hex()} 0"]



def test_known_password_survives_an_outage():
    st = {"shared_password": "hello", "no_reply_count": 5, "unheard_since": 1_000_000.0}
    poller.record(st, poller.PollResult(poller.NO_REPLY), password_set=False, now=1_000_000.0 + 2 * 86400)
    assert st["shared_password"] == "hello"
    poller.record(st, poller.PollResult(poller.NO_REPLY), password_set=False, now=1_000_000.0 + 8 * 86400)
    assert "shared_password" not in st
