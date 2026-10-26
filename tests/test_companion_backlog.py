"""Frames heard while waiting on a command are kept for the reply listener."""

from openhop_neighbour_battery import companion as c


def test_rx_frame_during_raw_send_is_not_lost():
    client = c.CompanionClient("127.0.0.1", 1)
    rx = bytes([c.PUSH_CODE_LOG_RX_DATA, 0x40, 0xC0]) + b"reply"
    frames = [rx, bytes([c.RESP_CODE_OK])]
    client._send = lambda body: None
    client._read_frame = lambda deadline: frames.pop(0) if frames else None
    assert client.send_raw_packet(b"\x00pkt") is True
    assert list(client.collect_rx(0.01)) == [rx]


def test_raw_send_without_answer_is_unconfirmed_not_an_error():
    client = c.CompanionClient("127.0.0.1", 1)
    client._send = lambda body: None
    client._read_frame = lambda deadline: None
    assert client.send_raw_packet(b"\x00pkt", wait=0.01) is False
