"""A fake openHop companion TCP server: just the commands the plugin uses."""

from __future__ import annotations

import socket
import struct
import threading

from openhop_neighbour_battery import companion as c


class FakeCompanion:
    def __init__(self):
        self.contacts: dict[bytes, int] = {}  # pubkey -> out_path_len (-1 unknown)
        self.keep_path_unknown = False  # ignore path updates, like a stale contact
        self.send_as_flood = False
        self.password = ""  # guest password; "" means a blank login works
        self.admin_password = None
        self.battery_mv = 3940
        self.answer_status = True
        self.answer_login = True
        self.commands: list[int] = []
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
            self.contacts[key] = -1 if self.keep_path_unknown else (-1 if opl == 0xFF else opl)
            self._out(conn, bytes([c.RESP_CODE_OK]))
        elif cmd == c.CMD_GET_CONTACT_BY_KEY:
            key = data[:32]
            if key not in self.contacts:
                self._out(conn, bytes([c.RESP_CODE_ERR, c.ERR_CODE_NOT_FOUND]))
                return
            opl = self.contacts[key]
            self._out(conn, bytes([c.RESP_CODE_CONTACT]) + key + bytes([2, 0, 0xFF if opl < 0 else opl]) + b"\x00" * 64)
        elif cmd == c.CMD_SEND_LOGIN:
            key, pw = data[:32], data[32:].decode()
            self._sent(conn)
            if not self.answer_login:
                return
            if self.admin_password is not None and pw == self.admin_password:
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_SUCCESS, 1]) + key[:6] + b"\x07\x00\x00\x00" + bytes([3, 13]))
            elif pw == self.password:
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_SUCCESS, 0]) + key[:6] + b"\x07\x00\x00\x00" + bytes([1, 13]))
            else:
                self._out(conn, bytes([c.PUSH_CODE_LOGIN_FAIL, 0]) + key[:6])
        elif cmd == c.CMD_SEND_STATUS_REQ:
            key = data[:32]
            self._sent(conn)
            if self.answer_status:
                stats = struct.pack("<HHhh", self.battery_mv, 0, -110, -90) + b"\x00" * 40
                self._out(conn, bytes([c.PUSH_CODE_STATUS_RESPONSE, 0]) + key[:6] + stats)
