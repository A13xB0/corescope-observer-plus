"""A minimal MeshCore companion client for openHop's companion TCP server.

Only the commands this plugin needs: pin a contact's path to direct, read it
back, log in and ask for status. Every request is checked after sending: the
server's RESP_CODE_SENT says whether the packet went out as a flood, and the
plugin treats that as a failure it must never repeat.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

CMD_APP_START = 1
CMD_ADD_UPDATE_CONTACT = 9
CMD_SEND_LOGIN = 26
CMD_SEND_STATUS_REQ = 27
CMD_GET_CONTACT_BY_KEY = 30

RESP_CODE_OK = 0
RESP_CODE_ERR = 1
RESP_CODE_CONTACT = 3
RESP_CODE_SELF_INFO = 5
RESP_CODE_SENT = 6

PUSH_CODE_LOGIN_SUCCESS = 0x85
PUSH_CODE_LOGIN_FAIL = 0x86
PUSH_CODE_STATUS_RESPONSE = 0x87

ERR_CODE_NOT_FOUND = 2

FRAME_TO_SERVER = 0x3C  # '<'
FRAME_FROM_SERVER = 0x3E  # '>'

ADV_TYPE_REPEATER = 2
PATH_SIZE = 64
NAME_SIZE = 32
OUT_PATH_UNKNOWN = 0xFF
MIN_REPLY_WAIT = 15.0


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


def encode_contact(pubkey: bytes, name: str, *, out_path_len: int = 0) -> bytes:
    """CMD_ADD_UPDATE_CONTACT body for a repeater with the given path length.

    out_path_len 0 is "direct, empty path": the packet has no next hop, so only
    a node in radio range can receive it.
    """
    if len(pubkey) != 32:
        raise ValueError("pubkey must be 32 bytes")
    name_b = name.encode("utf-8", errors="replace")[:NAME_SIZE].ljust(NAME_SIZE, b"\x00")
    return (
        bytes([CMD_ADD_UPDATE_CONTACT])
        + pubkey
        + bytes([ADV_TYPE_REPEATER, 0, out_path_len & 0xFF])
        + b"\x00" * PATH_SIZE
        + name_b
        + struct.pack("<IiiI", 0, 0, 0, int(time.time()))
    )


def contact_out_path_len(frame: bytes) -> int:
    """out_path_len from a RESP_CODE_CONTACT frame; -1 means unknown (flood)."""
    if len(frame) < 36 or frame[0] != RESP_CODE_CONTACT:
        raise CompanionError("not a contact frame")
    value = frame[35]
    return -1 if value == OUT_PATH_UNKNOWN else value


def status_battery_mv(frame: bytes) -> int:
    """batt_milli_volts, the first RepeaterStats field of a status push."""
    if len(frame) < 10 or frame[0] != PUSH_CODE_STATUS_RESPONSE:
        raise CompanionError("not a status response")
    return struct.unpack_from("<H", frame, 8)[0]


class CompanionClient:
    """Blocking client for one companion TCP port. One request at a time."""

    def __init__(self, host: str, port: int, *, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self.public_key: bytes = b""

    def __enter__(self) -> "CompanionClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._buf = b""
        # CMD_APP_START: 7 reserved bytes, then an app name.
        self._send(bytes([CMD_APP_START]) + b"\x01" + b"\x00" * 6 + b"neighbour-battery")
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
            except socket.timeout:
                return None
            if not chunk:
                raise CompanionError("companion closed the connection")
            self._buf += chunk

    def _wait(self, codes: set[int], timeout: float, match=None) -> bytes | None:
        """Next frame whose code is in codes (and that match accepts).

        Unrelated pushes (adverts, message notices, raw RX log) are skipped.
        """
        deadline = time.monotonic() + timeout
        while True:
            frame = self._read_frame(deadline)
            if frame is None:
                return None
            if frame and frame[0] in codes and (match is None or match(frame)):
                return frame

    def _expect_sent(self) -> float:
        """Wait for RESP_CODE_SENT; refuse a flood. Returns the server's timeout hint."""
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
        return max(MIN_REPLY_WAIT, min(45.0, timeout_ms / 1000.0 + 5.0))

    # -- commands -----------------------------------------------------------

    def pin_direct(self, pubkey: bytes, name: str) -> bool:
        """Set the contact's path to direct and read it back. True if it is 0."""
        self._send(encode_contact(pubkey, name, out_path_len=0))
        frame = self._wait({RESP_CODE_OK, RESP_CODE_ERR}, self.timeout)
        if frame is None or frame[0] != RESP_CODE_OK:
            return False
        return self.out_path_len(pubkey) == 0

    def out_path_len(self, pubkey: bytes) -> int | None:
        self._send(bytes([CMD_GET_CONTACT_BY_KEY]) + pubkey)
        frame = self._wait({RESP_CODE_CONTACT, RESP_CODE_ERR}, self.timeout)
        if frame is None or frame[0] != RESP_CODE_CONTACT:
            return None
        return contact_out_path_len(frame)

    def login(self, pubkey: bytes, password: str) -> LoginResult:
        self._send(bytes([CMD_SEND_LOGIN]) + pubkey + password.encode("utf-8"))
        wait = self._expect_sent()
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

    def battery_mv(self, pubkey: bytes) -> int | None:
        """Ask for status; the battery in mV, or None if no answer came."""
        self._send(bytes([CMD_SEND_STATUS_REQ]) + pubkey)
        wait = self._expect_sent()
        prefix = pubkey[:6]
        frame = self._wait(
            {PUSH_CODE_STATUS_RESPONSE}, wait, match=lambda f: f[2:8] == prefix
        )
        if frame is None:
            return None
        return status_battery_mv(frame)
