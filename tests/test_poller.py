import struct

import pytest

from fake_companion import FakeCompanion
from openhop_neighbour_battery import companion as c
from openhop_neighbour_battery import poller
from openhop_neighbour_battery.companion import CompanionClient
from openhop_neighbour_battery.openhop import Neighbour, Route

NODE = Neighbour(node_id="02f1" + "ab" * 30, name="West Lomond", snr=-5.0, rssi=-119,
                 last_seen=0, zero_hop=True)
RELAY_A = "4128a6" + "11" * 29
RELAY_B = "2eecdf" + "22" * 29
VIA_A = Route(relay_id=RELAY_A, relay_name="sco-edi-garvald", hash_size=3, adverts=5, snr=-0.8)
VIA_B = Route(relay_id=RELAY_B, relay_name="Bishop Hill", hash_size=3, adverts=2, snr=2.5)
DIRECT = Route(adverts=9, snr=-7.4)


@pytest.fixture
def fake():
    f = FakeCompanion()
    yield f
    f.close()


def poll(fake, password="", state=None, now=None, routes=None, **kw):
    state = {} if state is None else state
    with CompanionClient("127.0.0.1", fake.port, timeout=2) as client:
        client._expect_sent = _quick(client._expect_sent)
        result = poller.poll_one(client, NODE, password, state, now=now,
                                 routes=routes if routes is not None else [DIRECT],
                                 sleep=lambda s: None, **kw)
        return result, state


def _quick(expect_sent):
    """Don't wait the full reply window in tests."""
    def wrapped():
        _wait, tag = expect_sent()
        return 0.3, tag
    return wrapped


def statuses(fake):
    return [r for r in fake.requests if r[0] == c.REQ_TYPE_GET_STATUS]


def telemetries(fake):
    return [r for r in fake.requests if r[0] == c.REQ_TYPE_GET_TELEMETRY]


LOGGED_IN = {"logged_in_at": 1.0}


def test_logs_in_once_then_never_again(fake):
    result, state = poll(fake, now=10_000.0)
    assert result.code == poller.OK and result.role == "guest"
    assert fake.commands.count(c.CMD_SEND_LOGIN) == 1
    assert result.battery_mv == 3940 and result.stats["uptime_secs"] == 788089
    assert result.stats["last_snr"] == 1.25
    assert c.CMD_SEND_STATUS_REQ not in fake.commands  # openHop floods its retries of this one
    fake.commands.clear()
    result, _ = poll(fake, state=state, now=20_000.0)
    assert result.code == poller.OK and c.CMD_SEND_LOGIN not in fake.commands


def test_route_pinned_before_any_request(fake):
    poll(fake)
    first_send = next(i for i, cmd in enumerate(fake.commands) if cmd in (c.CMD_SEND_LOGIN, c.CMD_SEND_BINARY_REQ))
    assert fake.commands[:first_send].count(c.CMD_ADD_UPDATE_CONTACT) == 1
    assert fake.commands[:first_send].count(c.CMD_GET_CONTACT_BY_KEY) == 1
    assert fake.contacts[bytes.fromhex(NODE.node_id)] == (0, b"")


def test_nothing_sent_when_route_will_not_pin(fake):
    fake.keep_path_unknown = True
    result, _ = poll(fake)
    assert result.code == poller.NOT_DIRECT
    assert c.CMD_SEND_LOGIN not in fake.commands
    assert c.CMD_SEND_BINARY_REQ not in fake.commands


def test_a_flood_send_is_a_failure_and_not_repeated(fake):
    fake.send_as_flood = True
    result, _ = poll(fake, state=dict(LOGGED_IN))
    assert result.code == poller.NOT_DIRECT
    assert fake.commands.count(c.CMD_SEND_BINARY_REQ) == 1


def test_zero_mv_is_no_battery(fake):
    fake.battery_mv = 0
    result, _ = poll(fake, state=dict(LOGGED_IN))
    assert result.code == poller.NO_BATTERY and result.battery_mv == 0


def test_each_poll_starts_with_a_login_until_one_works(fake):
    fake.status_after_login_only = True
    fake.answer_login = False
    result, state = poll(fake, now=10_000.0)
    # A silent first login doesn't write the route off: all tries still go out.
    assert result.code == poller.NO_REPLY
    assert fake.commands.count(c.CMD_SEND_LOGIN) == 1 and len(statuses(fake)) == 3 and len(telemetries(fake)) == 3
    fake.answer_login = True
    fake.commands.clear()
    result, _ = poll(fake, state=state, now=11_200.0)
    assert result.code == poller.OK and fake.commands.count(c.CMD_SEND_LOGIN) == 1


def test_lucklaw_regression_silent_first_login_then_other_route(fake):
    # 25 Oct 13:14: no login on record, the first login's reply never came,
    # and the route via West Lomond was written off, leaving one try.
    fake.answer_login = False
    fake.answer_paths = {bytes.fromhex("2eecdf")}
    result, state = poll(fake, routes=[VIA_A, VIA_B], alternative_routes=True, now=10_000.0)
    assert result.code == poller.OK and result.route is VIA_B and result.method == "telemetry"
    assert [r[2] for r in telemetries(fake)] == [bytes.fromhex("4128a6"), bytes.fromhex("2eecdf")]
    assert state["logged_in_at"] == 10_000.0  # an answer proves the repeater knows us


def test_a_dead_route_does_not_use_up_tries(fake):
    fake.answer_login = False
    fake.answer_status = False
    fake.answer_paths = {bytes.fromhex("2eecdf")}
    fake.telemetry = None
    poll(fake, routes=[VIA_A, VIA_B], alternative_routes=True, state=dict(LOGGED_IN), now=10_000.0)
    # Try 1 via A fails, the probe via A is silent (A dead), then tries 2 and 3 both go via B.
    assert [r[2] for r in telemetries(fake)] == [bytes.fromhex("4128a6"), bytes.fromhex("2eecdf"), bytes.fromhex("2eecdf")]


def test_unanswered_request_probes_with_one_login(fake):
    # Logged in before, but the repeater rebooted and forgot this client.
    fake.status_after_login_only = True
    result, state = poll(fake, state=dict(LOGGED_IN), now=10_000.0)
    assert result.code == poller.OK and result.retried
    assert fake.commands.count(c.CMD_SEND_LOGIN) == 1 and len(statuses(fake)) == 2


def test_silent_login_stops_the_poll(fake):
    fake.answer_status = False
    fake.answer_login = False
    fake.telemetry = None
    result, _ = poll(fake, state=dict(LOGGED_IN), now=10_000.0)
    assert result.code == poller.NO_REPLY and "link looks down" in result.detail
    assert len(fake.requests) == 1 and fake.commands.count(c.CMD_SEND_LOGIN) == 1


def test_each_request_tried_three_times_all_direct(fake):
    fake.answer_status = False
    fake.telemetry = None
    result, _ = poll(fake, state=dict(LOGGED_IN), routes=[DIRECT, VIA_A])
    assert result.code == poller.NO_REPLY and result.retried
    # 3 tries, plus one straight after the (answered) login probe.
    assert len(statuses(fake)) == 4 and len(telemetries(fake)) == 3
    assert {r[2] for r in fake.requests} == {b""}  # no alternative routes: always the best one
    assert all(r[1] >= 0 for r in fake.requests)  # never unknown path, so never flooded
    assert fake.commands.count(c.CMD_SEND_LOGIN) == 1


def test_login_forgotten_after_three_silent_polls(fake):
    st = dict(LOGGED_IN)
    for t in (1, 2):
        poller.record(st, poller.PollResult(poller.NO_REPLY), False, now=t)
    assert "logged_in_at" in st
    poller.record(st, poller.PollResult(poller.NO_REPLY), False, now=3)
    assert "logged_in_at" not in st


def test_login_fail_is_not_a_password_problem_until_three_in_a_row(fake):
    fake.status_after_login_only = True
    fake.login_fail_always = True
    state = {}
    for n, now in enumerate((10_000.0, 20_000.0)):
        result, state = poll(fake, state=state, now=now)
        assert result.code == poller.NO_REPLY, n
        assert "login refused" in result.detail
    result, state = poll(fake, state=state, now=30_000.0)
    assert result.code == poller.NEEDS_PASSWORD


def test_real_wrong_password_after_three_tries(fake):
    fake.password = "secret"
    fake.status_after_login_only = True
    state = {}
    for now in (10_000.0, 20_000.0, 30_000.0):
        result, state = poll(fake, password="wrong", state=state, now=now)
    assert result.code == poller.PASSWORD_REJECTED
    result, _ = poll(fake, password="secret", state={}, now=40_000.0)
    assert result.code == poller.OK


def test_admin_role_is_reported(fake):
    fake.password = "guest"
    fake.admin_password = "admin"
    fake.status_after_login_only = True
    result, _ = poll(fake, password="admin", now=10_000.0)
    assert result.code == poller.OK and result.role == "admin"


def test_one_hop_route_pins_relay_path(fake):
    result, _ = poll(fake, routes=[VIA_A], state=dict(LOGGED_IN))
    assert result.method == "telemetry"
    assert result.code == poller.OK and result.route is VIA_A
    opl, path = fake.contacts[bytes.fromhex(NODE.node_id)]
    assert opl == 0x81 and path == bytes.fromhex("4128a6")
    r = poller.reading(NODE, result, 1761300000)
    assert r["reply_hops"] == 1 and r["via"] == RELAY_A and r["snr"] == -0.8 and "rssi" not in r


def test_alternative_routes_go_round_and_the_good_one_is_remembered(fake):
    fake.answer_paths = {bytes.fromhex("2eecdf")}
    fake.telemetry = None
    state = dict(LOGGED_IN, last_success=9_000.0)  # heard recently, so alternatives are tried
    result, state = poll(fake, routes=[DIRECT, VIA_A, VIA_B], alternative_routes=True, state=state, now=10_000.0)
    assert result.code == poller.OK and result.route is VIA_B and result.retried
    # Direct: status, then a login that goes unanswered (the path is dead), so
    # the round moves on, skipping the dead route.
    assert [r[2] for r in statuses(fake)] == [b"", bytes.fromhex("2eecdf")]
    poller.record(state, result, False, 10_000.0)
    assert state["last_route"] == RELAY_B
    fake.requests.clear()
    # Past STATUS_EVERY_SECONDS, so this poll asks for status again.
    poll(fake, routes=[DIRECT, VIA_A, VIA_B], alternative_routes=True, state=state, now=10_000.0 + 7 * 3600)
    assert statuses(fake)[0][2] == bytes.fromhex("2eecdf")  # last good route goes first


def test_without_alternative_routes_only_the_best_is_tried(fake):
    fake.answer_paths = {bytes.fromhex("4128a6")}
    result, _ = poll(fake, routes=[DIRECT, VIA_A], state=dict(LOGGED_IN), now=10_000.0)
    assert result.code == poller.NO_REPLY
    assert {r[2] for r in fake.requests} == {b""}


def test_status_ignored_falls_back_to_telemetry(fake):
    # Lucklaw Hill, 25 Oct 2026: guest status ignored, telemetry answered.
    fake.ignore_status = True
    fake.telemetry = bytes.fromhex("01740197016700a7")
    state = dict(LOGGED_IN)
    result, state = poll(fake, state=state, now=10_000.0)
    assert result.code == poller.OK and result.battery_mv == 4070 and result.method == "telemetry"
    assert result.temperature_c == 16.7 and not result.stats
    poller.record(state, result, False, 10_000.0)
    r = poller.reading(NODE, result, 1761300000)
    assert r["method"] == "telemetry" and r["temperature_c"] == 16.7 and "stats" not in r
    assert state["status_ignored"]
    fake.requests.clear()
    result, state = poll(fake, state=state, now=10_000.0 + 3600)
    assert result.code == poller.OK and not statuses(fake) and len(telemetries(fake)) == 1  # telemetry first
    fake.requests.clear()
    poll(fake, state=state, now=10_000.0 + 25 * 3600)
    assert statuses(fake)  # status checked again after a day


def test_temperature_read_when_enabled(fake):
    result, state = poll(fake, read_temperature=True, state=dict(LOGGED_IN))
    assert result.temperature_c == 31.5 and result.temperature_code == "ok"
    poller.record(state, result, False, 1000.0)
    assert state["temperature_c"] == 31.5
    assert poller.reading(NODE, result, 1761300000)["temperature_c"] == 31.5


def test_temperature_not_asked_when_off(fake):
    result, _ = poll(fake, state=dict(LOGGED_IN))
    assert result.temperature_code == ""
    assert not telemetries(fake)


def test_no_temperature_reported_goes_daily_and_battery_still_published(fake):
    fake.telemetry = bytes([1, 116, 0x01, 0x8A])  # voltage only
    state = dict(LOGGED_IN)
    for now in (1000.0, 5000.0, 9000.0):
        result, state = poll(fake, state=state, now=now, read_temperature=True)
        assert result.code == poller.OK and result.temperature_code == "none"
        poller.record(state, result, False, now)
    assert not poller.temperature_due(state, 9000.0 + 3600)
    assert poller.temperature_due(state, 9000.0 + 24 * 3600)
    assert "temperature_c" not in poller.reading(NODE, result, 1761300000)


def test_unanswered_telemetry_tried_three_times(fake):
    fake.telemetry = None
    result, _ = poll(fake, read_temperature=True, state=dict(LOGGED_IN))
    assert result.code == poller.OK and result.temperature_code == "no_reply"
    assert len(telemetries(fake)) == 4  # 3 tries, plus one after the login probe


def test_one_hop_repeaters_get_telemetry_only(fake):
    result, _ = poll(fake, routes=[VIA_A, VIA_B], state=dict(LOGGED_IN))
    assert result.code == poller.OK and result.method == "telemetry" and result.battery_mv == 3940
    assert not statuses(fake) and len(telemetries(fake)) == 1
    assert result.temperature_c == 31.5


def test_no_reply_retried_three_times_an_hour_then_hourly():
    st = {}
    poller.record(st, poller.PollResult(poller.NO_REPLY), False, now=0)
    assert not poller.is_due(st, 3600, False, now=19 * 60)
    assert poller.is_due(st, 3600, False, now=20 * 60)
    for t in range(1, 9):
        poller.record(st, poller.PollResult(poller.NO_REPLY), False, now=t * 1200)
    last = 8 * 1200
    assert not poller.is_due(st, 3600, False, now=last + 1200)  # 9 misses: back to hourly
    assert poller.is_due(st, 3600, False, now=last + 3600)  # but never given up
    poller.record(st, poller.PollResult(poller.OK, battery_mv=3900), False, now=last + 3600)
    assert st["no_reply_count"] == 0


def test_rejected_password_not_retried_until_changed():
    st = {}
    poller.record(st, poller.PollResult(poller.PASSWORD_REJECTED), True, now=0)
    assert not poller.is_due(st, 3600, True, now=10 ** 6)
    st["password_changed"] = True
    assert poller.is_due(st, 3600, True, now=10 ** 6)


def test_reading_shape():
    result = poller.PollResult(poller.OK, battery_mv=3940, route=DIRECT,
                               stats={"battery_mv": 3940, "uptime_secs": 5, "noise_floor": -116})
    r = poller.reading(NODE, result, 1761300000)
    assert r == {
        "node_id": NODE.node_id, "battery_mv": 3940, "source": "poll", "reply_hops": 0,
        "method": "status", "sampled_at": "2025-10-24T10:00:00Z", "snr": -5.0, "rssi": -119,
        "stats": {"uptime_secs": 5, "noise_floor": -116},
    }


def test_parse_stats_real_reply():
    # West Lomond, 25 Oct 2026 10:31.
    data = bytes.fromhex("c60f00008cff8eff523d0100344f00007d47000079060c000b4b000029040000"
                         "a629010094130000020005002400eadb6b200100aa6d000000000000")
    s = c.parse_stats(data)
    assert s["battery_mv"] == 4038 and s["uptime_secs"] == 788089 and s["noise_floor"] == -116
    assert s["recv_flood"] == 76198 and s["flood_dups"] == 56298 and s["recv_errors"] == 28074


def test_parse_stats_short_reply_leaves_fields_out():
    s = c.parse_stats(struct.pack("<HHhh", 3900, 0, -110, -90))
    assert s == {"battery_mv": 3900, "tx_queue": 0, "noise_floor": -110, "last_rssi": -90}


def test_lpp_voltage():
    assert c.lpp_voltage_mv(bytes.fromhex("01740197016700a7")) == 4070
    assert c.lpp_voltage_mv(bytes([1, 103, 0, 1])) is None


def test_lpp_temperature():
    assert c.lpp_temperature(bytes([1, 116, 0x01, 0x8A, 1, 103, 0xFF, 0xF6, 0, 0])) == -1.0
    assert c.lpp_temperature(bytes([1, 116, 0x01, 0x8A])) is None
    assert c.lpp_temperature(bytes([2, 103, 0x00, 0x64])) is None  # another channel's sensor
    assert c.lpp_temperature(bytes([1, 250, 0, 1, 103, 0, 1])) is None  # unknown type stops parsing


def test_path_len_byte():
    assert c.path_len_byte(b"", 1) == 0
    assert c.path_len_byte(b"\x41", 1) == 0x01
    assert c.path_len_byte(b"\x41\x28\xa6", 3) == 0x81
    with pytest.raises(ValueError):
        c.path_len_byte(b"\x41\x28", 3)


def test_unheard_24h_backs_off_to_daily():
    # A repeater unheard for 24 h is checked only once a day, until it answers.
    hour = 3600
    now0 = 1_000_000.0
    st = {}
    # 25 h of silence: unheard_since set a day+ ago, last attempt an hour ago.
    st["code"] = poller.NO_REPLY
    st["unheard_since"] = now0 - 25 * hour
    st["last_attempt"] = now0 - hour
    st["no_reply_count"] = 50
    assert not poller.is_due(st, hour, False, now=now0)              # not every hour any more
    assert poller.is_due(st, hour, False, now=now0 + 24 * hour)     # once a day
    # A success clears the backoff: record() drops unheard_since.
    poller.record(st, poller.PollResult(poller.OK, battery_mv=4000), password_set=False, now=now0 + 24 * hour)
    assert "unheard_since" not in st
    st["last_attempt"] = now0 + 24 * hour
    assert poller.is_due(st, hour, False, now=now0 + 25 * hour + 1)  # back to hourly


def test_recent_failures_do_not_back_off():
    # Failing for only a couple of hours still retries at the normal cadence,
    # not once a day.
    hour = 3600
    now0 = 1_000_000.0
    st = {"code": poller.NO_REPLY, "unheard_since": now0 - 2 * hour,
          "last_attempt": now0 - hour, "no_reply_count": 50}
    assert poller.is_due(st, hour, False, now=now0)  # 2 h unheard: still hourly



def test_status_only_every_six_hours_otherwise_telemetry(fake):
    # Telemetry carries battery and temperature in a small reply, so status
    # (60 bytes) is asked for only when the last one is 6 h old.
    fake.telemetry = bytes.fromhex("01740197016700a7")
    state = dict(LOGGED_IN)
    r1, state = poll(fake, state=state, now=100_000.0)
    assert r1.method == "status"
    poller.record(state, r1, False, 100_000.0)
    fake.requests.clear()
    r2, state = poll(fake, state=state, now=100_000.0 + 3600)
    assert r2.code == poller.OK and r2.method == "telemetry" and not statuses(fake)
    fake.requests.clear()
    r3, state = poll(fake, state=state, now=100_000.0 + 6 * 3600 + 1)
    assert r3.method == "status"



def test_companion_stays_on_a_route_whose_login_was_answered(fake):
    # montrave, 26 Oct 15:49: login answered via Bishop Hill but telemetry lost;
    # trying direct and via Markinch after that only wasted two packets.
    fake.answer_paths = {bytes.fromhex("2eecdf")}
    fake.answer_status = False
    fake.telemetry = None
    state = {"logged_in_at": 1.0, "last_success": 9_000.0, "status_checked_at": 10_000.0}
    poll(fake, routes=[VIA_B, DIRECT, VIA_A], alternative_routes=True, state=state, now=10_000.0)
    assert {r[2] for r in fake.requests} == {bytes.fromhex("2eecdf")}
