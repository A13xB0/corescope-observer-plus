import pytest

from fake_companion import FakeCompanion
from openhop_neighbour_battery import companion as c
from openhop_neighbour_battery import poller
from openhop_neighbour_battery.companion import CompanionClient
from openhop_neighbour_battery.openhop import Neighbour

NODE = Neighbour(node_id="02f1" + "ab" * 30, name="West Lomond", snr=-5.0, rssi=-119,
                 last_seen=0, zero_hop=True)


@pytest.fixture
def fake():
    f = FakeCompanion()
    yield f
    f.close()


def poll(fake, password="", state=None, now=None):
    state = {} if state is None else state
    with CompanionClient("127.0.0.1", fake.port, timeout=2) as client:
        return poller.poll_one(client, NODE, password, state, now=now), state


def test_blank_login_reads_battery(fake):
    result, state = poll(fake)
    assert result.code == poller.OK
    assert result.battery_mv == 3940
    assert result.role == "guest"
    assert "logged_in_at" in state


def test_path_pinned_direct_before_any_request(fake):
    poll(fake)
    sends = [cmd for cmd in fake.commands if cmd in (c.CMD_SEND_LOGIN, c.CMD_SEND_STATUS_REQ)]
    assert sends
    first_send = fake.commands.index(sends[0])
    assert fake.commands[:first_send].count(c.CMD_ADD_UPDATE_CONTACT) == 1
    assert fake.commands[:first_send].count(c.CMD_GET_CONTACT_BY_KEY) == 1
    assert fake.contacts[bytes.fromhex(NODE.node_id)] == 0


def test_nothing_sent_when_path_will_not_pin(fake):
    fake.keep_path_unknown = True
    result, _ = poll(fake)
    assert result.code == poller.NOT_DIRECT
    assert c.CMD_SEND_LOGIN not in fake.commands
    assert c.CMD_SEND_STATUS_REQ not in fake.commands


def test_a_flood_send_is_a_failure(fake):
    fake.send_as_flood = True
    result, _ = poll(fake)
    assert result.code == poller.NOT_DIRECT
    assert fake.commands.count(c.CMD_SEND_LOGIN) == 1  # and no second attempt


def test_zero_mv_is_no_battery(fake):
    fake.battery_mv = 0
    result, _ = poll(fake)
    assert result.code == poller.NO_BATTERY and result.battery_mv == 0


def test_guest_password_needed(fake):
    fake.password = "secret"
    result, _ = poll(fake)
    assert result.code == poller.NEEDS_PASSWORD
    result, _ = poll(fake, password="wrong")
    assert result.code == poller.PASSWORD_REJECTED
    result, _ = poll(fake, password="secret")
    assert result.code == poller.OK


def test_admin_role_is_reported(fake):
    fake.password = "guest"
    fake.admin_password = "admin"
    result, _ = poll(fake, password="admin")
    assert result.code == poller.OK and result.role == "admin"


def test_status_first_after_recent_login(fake):
    _, state = poll(fake, now=1000.0)
    fake.commands.clear()
    result, _ = poll(fake, state=state, now=2000.0)
    assert result.code == poller.OK
    assert c.CMD_SEND_LOGIN not in fake.commands


def test_logs_in_again_when_status_gets_no_answer(fake):
    _, state = poll(fake, now=1000.0)
    fake.answer_status = False
    fake.commands.clear()
    client = CompanionClient("127.0.0.1", fake.port, timeout=2)
    client.connect()
    client._expect_sent = lambda: 0.3  # don't wait the full server hint in tests
    result = poller.poll_one(client, NODE, "", state, now=2000.0)
    client.close()
    assert result.code == poller.NO_REPLY
    assert c.CMD_SEND_LOGIN in fake.commands


def test_rejected_password_not_retried_until_changed():
    st = {}
    poller.record(st, poller.PollResult(poller.PASSWORD_REJECTED), True, now=0)
    assert not poller.is_due(st, 3600, True, now=10 ** 6)
    st["password_changed"] = True
    assert poller.is_due(st, 3600, True, now=10 ** 6)


def test_needs_password_waits_for_one():
    st = {}
    poller.record(st, poller.PollResult(poller.NEEDS_PASSWORD), False, now=0)
    assert not poller.is_due(st, 3600, False, now=10 ** 6)
    assert poller.is_due(st, 3600, True, now=10 ** 6)


def test_backoff_after_three_no_replies():
    st = {}
    for t in (0, 1, 2):
        poller.record(st, poller.PollResult(poller.NO_REPLY), False, now=t)
    assert not poller.is_due(st, 3600, False, now=2 + 3600)
    assert poller.is_due(st, 3600, False, now=2 + 6 * 3600)


def test_reading_shape():
    r = poller.reading(NODE, poller.PollResult(poller.OK, battery_mv=3940), 1761300000)
    assert r == {
        "node_id": NODE.node_id, "battery_mv": 3940, "source": "poll", "reply_hops": 0,
        "method": "status", "sampled_at": "2025-10-24T10:00:00Z", "snr": -5.0, "rssi": -119,
    }
