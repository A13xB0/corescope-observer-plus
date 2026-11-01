"""A minimal MeshCore companion client for openHop's companion TCP server.

Requests that go out as a flood are treated as failures never to repeat.
"""

from __future__ import annotations

import collections
import socket
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import TracebackType

from .models import JsonDict

CMD_APP_START = 1
CMD_ADD_UPDATE_CONTACT = 9
CMD_SEND_LOGIN = 26
CMD_SEND_STATUS_REQ = 27  # never sent: openHop resends a slow one as a flood
CMD_GET_CONTACT_BY_KEY = 30
CMD_SEND_BINARY_REQ = 50
CMD_SEND_RAW_PACKET = 65

RESP_CODE_OK = 0
RESP_CODE_ERR = 1
RESP_CODE_CONTACT = 3
RESP_CODE_SELF_INFO = 5
RESP_CODE_SENT = 6

PUSH_CODE_LOGIN_SUCCESS = 0x85
PUSH_CODE_LOGIN_FAIL = 0x86
PUSH_CODE_BINARY_RESPONSE = 0x8C
PUSH_CODE_LOG_RX_DATA = 0x88  # a raw packet heard off the air: [0x88][snr][rssi][packet]

REQ_TYPE_GET_STATUS = 0x01
REQ_TYPE_GET_TELEMETRY = 0x03

FRAME_TO_SERVER = 0x3C  # '<'
FRAME_FROM_SERVER = 0x3E  # '>'

ADV_TYPE_REPEATER = 2
PATH_SIZE = 64
NAME_SIZE = 32
OUT_PATH_UNKNOWN = 0xFF
MIN_REPLY_WAIT = 15.0
# How long to wait for openHop to confirm a raw send. It confirms only after
# transmitting, and a busy repeater can queue a packet behind several seconds
# of flood forwarding; 5 s gave false "refused" errors on sends that went out.
RAW_SEND_WAIT = 30.0


class CompanionError(Exception):
    """The companion server refused a command or the link failed."""


class FloodSent(CompanionError):
    """A request went out as a flood. Never expected: the path is pinned first."""


@dataclass
class LoginResult:
    ok: bool
    role: str = ""  # "admin" | "guest"
    permissions: int = 0
    timed_out: bool = False


def encode_frame(payload: bytes) -> bytes:
    return bytes([FRAME_TO_SERVER]) + struct.pack("<H", len(payload)) + payload


def path_len_byte(path: bytes, hash_size: int) -> int:
    """MeshCore's out_path_len byte: hop count, with the hash width in the top two bits."""
    if not path:
        return 0
    if hash_size not in (1, 2, 3) or len(path) % hash_size:
        raise ValueError("path must be whole hashes of 1, 2 or 3 bytes")
    return ((hash_size - 1) << 6) | (len(path) // hash_size)


def encode_contact(pubkey: bytes, name: str, *, path: bytes = b"", hash_size: int = 1) -> bytes:
    """CMD_ADD_UPDATE_CONTACT body. An empty path is direct, zero hops; nothing floods."""
    if len(pubkey) != 32:
        raise ValueError("pubkey must be 32 bytes")
    name_b = name.encode("utf-8", errors="replace")[:NAME_SIZE].ljust(NAME_SIZE, b"\x00")
    return (
        bytes([CMD_ADD_UPDATE_CONTACT])
        + pubkey
        + bytes([ADV_TYPE_REPEATER, 0, path_len_byte(path, hash_size)])
        + path.ljust(PATH_SIZE, b"\x00")
        + name_b
        + struct.pack("<IiiI", 0, 0, 0, int(time.time()))
    )


def contact_route(frame: bytes) -> tuple[int, bytes]:
    """(out_path_len byte, path) from a RESP_CODE_CONTACT frame; -1 means unknown (flood)."""
    if len(frame) < 36 or frame[0] != RESP_CODE_CONTACT:
        raise CompanionError("not a contact frame")
    value = frame[35]
    if value == OUT_PATH_UNKNOWN:
        return -1, b""
    nbytes = (value & 0x3F) * ((value >> 6) + 1)
    return value, frame[36 : 36 + nbytes]


# RepeaterStats (MeshCore examples/simple_repeater/MyMesh.h), in order. Older
# firmware sends a shorter struct; fields past its end are left out.
STATS_FIELDS = (
    ("battery_mv", "H"),
    ("tx_queue", "H"),
    ("noise_floor", "h"),
    ("last_rssi", "h"),
    ("packets_recv", "I"),
    ("packets_sent", "I"),
    ("tx_air_secs", "I"),
    ("uptime_secs", "I"),
    ("sent_flood", "I"),
    ("sent_direct", "I"),
    ("recv_flood", "I"),
    ("recv_direct", "I"),
    ("err_events", "H"),
    ("last_snr_x4", "h"),
    ("direct_dups", "H"),
    ("flood_dups", "H"),
    ("rx_air_secs", "I"),
    ("recv_errors", "I"),
)


def parse_stats(data: bytes) -> JsonDict:
    """The RepeaterStats fields present in a status reply."""
    if len(data) < 2:
        raise CompanionError("status reply too short")
    out: JsonDict = {}
    off = 0
    for name, fmt in STATS_FIELDS:
        size = struct.calcsize("<" + fmt)
        if off + size > len(data):
            break
        (value,) = struct.unpack_from("<" + fmt, data, off)
        off += size
        if name == "last_snr_x4":
            out["last_snr"] = value / 4
        else:
            out[name] = value
    return out


# CayenneLPP as MeshCore writes it (src/helpers/sensors/LPPDataHelpers.h):
# type -> (size, multiplier, signed).
LPP_TEMPERATURE = 103
LPP_VOLTAGE = 116
TELEM_CHANNEL_SELF = 1
_LPP_SIZES = {
    0: 1,
    1: 1,
    2: 2,
    3: 2,
    100: 4,
    101: 2,
    102: 1,
    103: 2,
    104: 2,
    113: 6,
    115: 2,
    116: 2,
    117: 2,
    118: 4,
    120: 1,
    121: 2,
    125: 2,
    128: 2,
    130: 4,
    131: 4,
    132: 2,
    133: 4,
    134: 6,
    135: 3,
    136: 9,
    142: 1,
}


def parse_lpp(data: bytes) -> list[tuple[int, int, bytes]]:
    """(channel, type, raw value) records, stopping at padding or an unknown type."""
    out = []
    i = 0
    while i + 2 <= len(data):
        channel, kind = data[i], data[i + 1]
        size = _LPP_SIZES.get(kind)
        if channel == 0 or size is None or i + 2 + size > len(data):
            break
        out.append((channel, kind, data[i + 2 : i + 2 + size]))
        i += 2 + size
    return out


def lpp_voltage_mv(data: bytes) -> int | None:
    """The repeater's own voltage (channel 1) in mV, or None if it sent none."""
    for channel, kind, raw in parse_lpp(data):
        if channel == TELEM_CHANNEL_SELF and kind == LPP_VOLTAGE:
            return int(struct.unpack(">H", raw)[0]) * 10
    return None


def lpp_temperature(data: bytes) -> float | None:
    """The repeater's own temperature (channel 1) in °C, or None if it sent none.

    A board without a sensor still sends the field: -128 °C is MeshCore's "no
    reading", so anything outside what a chip could report is no reading.
    """
    for channel, kind, raw in parse_lpp(data):
        if channel == TELEM_CHANNEL_SELF and kind == LPP_TEMPERATURE:
            c = float(struct.unpack(">h", raw)[0]) / 10
            return c if -60.0 <= c <= 125.0 else None
    return None


class CompanionClient:
    """Blocking client for one companion TCP port. One request at a time."""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        # RX frames that arrive while waiting for another answer (openHop's OK to
        # a raw send comes only after it transmits), so a fast reply isn't lost.
        self._rx_backlog: collections.deque[bytes] = collections.deque(maxlen=256)
        self._sock: socket.socket | None = None
        self._buf = b""
        self.public_key: bytes = b""

    def __enter__(self) -> CompanionClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._buf = b""
        # CMD_APP_START: 7 reserved bytes, then an app name.
        self._send(bytes([CMD_APP_START]) + b"\x01" + b"\x00" * 6 + b"corescope-observer-plus")
        frame = self._wait({RESP_CODE_SELF_INFO, RESP_CODE_ERR}, self.timeout)
        if frame is None or frame[0] != RESP_CODE_SELF_INFO:
            raise CompanionError("companion did not answer APP_START")
        self.public_key = frame[4:36]

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- frames -----------------------------------------------------------

    def _send(self, payload: bytes) -> None:
        if self._sock is None:
            raise CompanionError("not connected")
        self._sock.sendall(encode_frame(payload))

    def _read_frame(self, deadline: float) -> bytes | None:
        assert self._sock is not None
        while True:
            # Drop anything before a frame start.
            start = self._buf.find(bytes([FRAME_FROM_SERVER]))
            if start > 0:
                self._buf = self._buf[start:]
            elif start < 0:
                self._buf = b""
            if len(self._buf) >= 3:
                (length,) = struct.unpack_from("<H", self._buf, 1)
                if len(self._buf) >= 3 + length:
                    frame = self._buf[3 : 3 + length]
                    self._buf = self._buf[3 + length :]
                    return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                return None
            if not chunk:
                raise CompanionError("companion closed the connection")
            self._buf += chunk

    def _wait(
        self, codes: set[int], timeout: float, match: Callable[[bytes], bool] | None = None
    ) -> bytes | None:
        """Next frame whose code is in codes (and that match accepts)."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._read_frame(deadline)
            if frame is None:
                return None
            if frame and frame[0] in codes and (match is None or match(frame)):
                return frame
            if frame and frame[0] == PUSH_CODE_LOG_RX_DATA:
                self._rx_backlog.append(frame)

    def _expect_sent(self) -> tuple[float, bytes]:
        """Wait for RESP_CODE_SENT; refuse a flood. Returns the wait and the tag."""
        frame = self._wait({RESP_CODE_SENT, RESP_CODE_ERR}, self.timeout)
        if frame is None:
            raise CompanionError("no reply to the send command")
        if frame[0] == RESP_CODE_ERR:
            code = frame[1] if len(frame) > 1 else -1
            raise CompanionError(f"send refused (error {code})")
        if frame[1] != 0:
            raise FloodSent("the companion sent the request as a flood")
        (timeout_ms,) = struct.unpack_from("<I", frame, 6)
        # The hint covers airtime both ways; a weak or busy neighbour can take
        # longer, so allow at least MIN_REPLY_WAIT.
        return max(MIN_REPLY_WAIT, min(45.0, timeout_ms / 1000.0 + 5.0)), frame[2:6]

    # -- commands -----------------------------------------------------------

    def pin_route(self, pubkey: bytes, name: str, path: bytes = b"", hash_size: int = 1) -> bool:
        """Set the contact's path and read it back. True if it stuck exactly."""
        self._send(encode_contact(pubkey, name, path=path, hash_size=hash_size))
        frame = self._wait({RESP_CODE_OK, RESP_CODE_ERR}, self.timeout)
        if frame is None or frame[0] != RESP_CODE_OK:
            return False
        got = self.route(pubkey)
        return got == (path_len_byte(path, hash_size), path)

    def route(self, pubkey: bytes) -> tuple[int, bytes] | None:
        self._send(bytes([CMD_GET_CONTACT_BY_KEY]) + pubkey)
        frame = self._wait({RESP_CODE_CONTACT, RESP_CODE_ERR}, self.timeout)
        if frame is None or frame[0] != RESP_CODE_CONTACT:
            return None
        return contact_route(frame)

    def login(self, pubkey: bytes, password: str) -> LoginResult:
        self._send(bytes([CMD_SEND_LOGIN]) + pubkey + password.encode("utf-8"))
        wait, _tag = self._expect_sent()
        prefix = pubkey[:6]
        frame = self._wait(
            {PUSH_CODE_LOGIN_SUCCESS, PUSH_CODE_LOGIN_FAIL},
            wait,
            match=lambda f: f[2:8] == prefix,
        )
        if frame is None:
            return LoginResult(ok=False, timed_out=True)
        if frame[0] == PUSH_CODE_LOGIN_FAIL:
            return LoginResult(ok=False)
        admin = frame[1] == 1
        perms = frame[12] if len(frame) > 12 else 0
        return LoginResult(ok=True, role="admin" if admin else "guest", permissions=perms)

    def binary_request(self, pubkey: bytes, request: bytes) -> bytes | None:
        """Send one binary request; the reply data after the tag, or None."""
        self._send(bytes([CMD_SEND_BINARY_REQ]) + pubkey + request)
        wait, tag = self._expect_sent()
        frame = self._wait({PUSH_CODE_BINARY_RESPONSE}, wait, match=lambda f: f[2:6] == tag)
        if frame is None:
            return None
        return frame[6:]

    def status(self, pubkey: bytes) -> JsonDict | None:
        """Ask for status; the RepeaterStats fields, or None if no answer came."""
        data = self.binary_request(pubkey, bytes([REQ_TYPE_GET_STATUS]))
        return None if data is None else parse_stats(data)

    def telemetry(self, pubkey: bytes) -> bytes | None:
        """Ask for telemetry; the CayenneLPP bytes, or None if no answer came."""
        return self.binary_request(pubkey, bytes([REQ_TYPE_GET_TELEMETRY]))

    # -- shared guest key (raw packets) -------------------------------------

    def send_raw_packet(self, body: bytes, *, wait: float = RAW_SEND_WAIT) -> bool:
        """Transmit a finished packet: [priority][wire packet], sent verbatim.

        openHop answers only once it has gone out, which can take a while when
        busy (listen-before-talk, a flood ahead in the queue). False means no
        answer came: most likely sent, just unconfirmed, so the caller keeps
        listening. An explicit error raises.
        """
        self._send(bytes([CMD_SEND_RAW_PACKET]) + body)
        frame = self._wait({RESP_CODE_OK, RESP_CODE_ERR}, wait)
        if frame is None:
            return False
        if frame[0] != RESP_CODE_OK:
            code = frame[1] if len(frame) > 1 else -1
            raise CompanionError(f"raw packet refused (error {code})")
        return True

    def clear_rx_backlog(self) -> None:
        """Forget RX frames kept from earlier waits (call at the start of a poll)."""
        self._rx_backlog.clear()

    def collect_rx(self, window: float) -> Iterator[bytes]:
        """Raw RX log frames heard within ``window`` seconds, kept ones first."""
        while self._rx_backlog:
            yield self._rx_backlog.popleft()
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            frame = self._wait({PUSH_CODE_LOG_RX_DATA}, remaining)
            if frame is None:
                return
            yield frame
