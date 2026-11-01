"""A fake openHop companion TCP server: just the commands the plugin uses."""

from __future__ import annotations

import socket
import struct
import threading

from openhop_corescope_observer_plus import companion as c


class FakeCompanion:
    def __init__(self):
        self.contacts: dict[bytes, tuple[int, bytes]] = {}  # pubkey -> (out_path_len, path); -1 unknown
        self.keep_path_unknown = False  # ignore path updates, like a stale contact
        self.send_as_flood = False
        self.password = ""  # guest password; "" means a blank login works
        self.admin_password = None
        self.battery_mv = 3940
        self.answer_status = True
        self.answer_login = True
        self.login_fail_always = False  # answer every login with LOGIN_FAIL, like a misread reply
        self.answer_paths: set[bytes] | None = None  # only requests along these paths are answered
        self.status_after_login_only = False  # requests unanswered until a login succeeds
        self.ignore_status = False  # older firmware: a guest's status request is ignored
        self.logged_in = False
        self.logins = 0  # logins received
        self.telemetry: bytes | None = bytes([1, 116, 0x01, 0x8A, 1, 103, 0x01, 0x3B])  # 3.94 V, 31.5 °C
        self.commands: list[int] = []
        self.requests: list[tuple[int, int, bytes]] = []  # (request type, out_path_len, path) per binary request
        self._tag = 100
        self.public_key = bytes(range(32))
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self._stop = True
        self._srv.close()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    @staticmethod
    def _out(conn, payload: bytes):
        conn.sendall(bytes([c.FRAME_FROM_SERVER]) + struct.pack("<H", len(payload)) + payload)

    def _client(self, conn):
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 3:
                    (n,) = struct.unpack_from("<H", buf, 1)
                    if len(buf) < 3 + n:
                        break
                    frame, buf = buf[3 : 3 + n], buf[3 + n :]
                    self._handle(conn, frame)

    def _sent(self, conn):
        self._out(conn, bytes([c.RESP_CODE_SENT, 1 if self.send_as_flood else 0]) + struct.pack("<II", 7, 1000))

    def _handle(self, conn, f: bytes):
        cmd, data = f[0], f[1:]
        self.commands.append(cmd)
        # An unrelated push first, as a real server sends adverts at any time.
        self._out(conn, bytes([0x80]) + b"\x00" * 32)
        if cmd == c.CMD_APP_START:
            self._out(conn, bytes([c.RESP_CODE_SELF_INFO, 1, 22, 22]) + self.public_key + b"\x00" * 20)
        elif cmd == c.CMD_ADD_UPDATE_CONTACT:
            key = data[:32]
            opl = data[34]
            nbytes = (opl & 0x3F) * ((opl >> 6) + 1) if opl != 0xFF else 0
            path = data[35 : 35 + nbytes]
            self.contacts[key] = (-1, b"") if self.keep_path_unknown or opl == 0xFF else (opl, path)
            self._out(conn, bytes([c.RESP_CODE_OK]))
        elif cmd == c.CMD_GET_CONTACT_BY_KEY:
            key = data[:32]
            if key not in self.contacts:
                self._out(conn, bytes([c.RESP_CODE_ERR, c.ERR_CODE_NOT_FOUND]))
                return
            opl, path = self.contacts[key]
            self._out(conn, bytes([c.RESP_CODE_CONTACT]) + key + bytes([2, 0, 0xFF if opl < 0 else opl])
                      + path.ljust(64, b"\x00"))
        elif cmd == c.CMD_SEND_LOGIN:
            key, pw = data[:32], data[32:].decode()
            self.logins += 1
            self._sent(conn)
            if not self.answer_login:
                return
            # A dead path loses the login too, like any request along it.
            if self.answer_paths is not None and self.contacts.get(key, (-1, b""))[1] not in self.answer_paths:
                return
            if self.login_fail_always:
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_FAIL, 0]) + key[:6])
                return
            if self.admin_password is not None and pw == self.admin_password:
                self.logged_in = True
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_SUCCESS, 1]) + key[:6] + b"\x07\x00\x00\x00" + bytes([3, 13]))
            elif pw == self.password:
                self.logged_in = True
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_SUCCESS, 0]) + key[:6] + b"\x07\x00\x00\x00" + bytes([1, 13]))
            else:
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_FAIL, 0]) + key[:6])
        elif cmd == c.CMD_SEND_BINARY_REQ:
            key, req = data[:32], data[32:]
            opl, path = self.contacts.get(key, (-1, b""))
            self.requests.append((req[0], opl, path))
            self._tag += 1
            tag = struct.pack("<I", self._tag)
            self._out(conn, bytes([c.RESP_CODE_SENT, 1 if self.send_as_flood or opl < 0 else 0]) + tag
                      + struct.pack("<I", 1000))
            if self.answer_paths is not None and path not in self.answer_paths:
                return
            if req[0] == c.REQ_TYPE_GET_STATUS:
                if not self.answer_status or self.ignore_status or (self.status_after_login_only and not self.logged_in):
                    return
                stats = struct.pack("<HHhhIIIIIIIIHhHHII", self.battery_mv, 0, -116, -114, 81234, 20276,
                                    18301, 788089, 19211, 1065, 76198, 5012, 2, 5, 36, 56298, 73835, 28074)
                self._out(conn, bytes([c.PUSH_CODE_BINARY_RESPONSE, 0]) + tag + stats + b"\x00" * 4)
            elif req[0] == c.REQ_TYPE_GET_TELEMETRY and self.telemetry is not None:
                if self.status_after_login_only and not self.logged_in:
                    return
                self._out(conn, bytes([c.PUSH_CODE_BINARY_RESPONSE, 0]) + tag + self.telemetry + b"\x00" * 3)
