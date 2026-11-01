"""Guest key packets: polling as an identity whose replies any observer holding it can read.

The key may be public, so its safety rules are enforced here, where packets are built.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from openhop_core.protocol import CryptoUtils
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_PATH,
    PAYLOAD_TYPE_RESPONSE,
    REQ_TYPE_GET_STATUS,
    TXT_TYPE_CLI_COMMAND,
)
from openhop_core.protocol.identity import Identity, LocalIdentity
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.packet_builder import PacketBuilder
from openhop_core.protocol.packet_utils import PathUtils

from .companion import PUSH_CODE_LOG_RX_DATA
from .models import JsonDict

# The only passwords a guest key may carry, by exact match. Neither is a secret:
# a real password must never go on air under a key others can read.
SHARED_PASSWORDS = ("", "hello")

# CMD_SEND_RAW_PACKET body is [priority][raw packet].
PRIORITY_NORMAL = 0

_RX_HEADER = 3  # PUSH_CODE_LOG_RX_DATA frame: [0x88][snr][rssi][raw packet]

# Only this admin_code is admin: a room server sends 2 for a plain guest.
LOGIN_ADMIN_CODE_ADMIN = 1
RESP_SERVER_LOGIN_OK = 0


class UnsafeSharedPassword(Exception):
    """A password that is not blank or "hello" was offered for the shared key."""


class SharedKeyError(Exception):
    """A shared-key packet could not be built."""


@dataclass(frozen=True)
class SharedKey:
    """A guest identity. ``private_key`` is a 32-byte seed or MeshCore's 64-byte key."""

    name: str
    version: int
    public_key: bytes
    private_key: bytes

    def __post_init__(self) -> None:
        if len(self.public_key) != 32:
            raise SharedKeyError("shared public key must be 32 bytes")
        if len(self.private_key) not in (32, 64):
            raise SharedKeyError("shared private key must be 32 or 64 bytes")

    @property
    def hash_byte(self) -> int:
        """The 1-byte routing hash: the first byte of the public key."""
        return self.public_key[0]

    @property
    def hex8(self) -> str:
        return self.public_key.hex()[:8].upper()

    def identity(self) -> LocalIdentity:
        return LocalIdentity(self.private_key)

    @classmethod
    def from_api(cls, body: JsonDict | None) -> SharedKey | None:
        """A SharedKey from CoreScope's payload, or None if it holds no usable keypair."""
        if not isinstance(body, dict):
            return None
        pub = str(body.get("public_key") or "")
        priv = str(body.get("private_key") or "")
        try:
            key = cls(
                str(body.get("name") or "shared guest"),
                int(body.get("version") or 0),
                bytes.fromhex(pub),
                bytes.fromhex(priv),
            )
        except (ValueError, SharedKeyError):
            return None
        # The public half must belong to the private half, or every request is
        # encrypted to a key no one holds and a setperm would name the wrong key.
        try:
            if key.identity().get_public_key() != key.public_key:
                return None
        except Exception:  # noqa: BLE001 - a bad key is simply no key
            return None
        return key


@dataclass(frozen=True)
class Reply:
    """A decoded reply to the key. ``data`` follows the 4-byte tag.

    Its kind can't be told from the bytes (a telemetry reply can look like an
    admin login once padded), so it is never guessed here.
    """

    repeater_pubkey: bytes
    tag: int  # the request timestamp the reply echoes
    data: bytes
    # The repeater flooded it: it had no stored route back to the key.
    flooded: bool = False


def _contact(repeater_pubkey: bytes, out_path: bytes, out_path_len: int) -> SimpleNamespace:
    """A minimal contact for the packet builder. -1 would flood, so it is refused."""
    if out_path_len < 0:
        raise SharedKeyError("the shared key is never flooded; pin a route first")
    return SimpleNamespace(
        public_key=repeater_pubkey.hex(),
        out_path=out_path,
        out_path_len=out_path_len,
        type=0,
    )


def _frame(packet: Packet) -> bytes:
    """A finished raw-packet command body: [priority][wire packet]."""
    return bytes([PRIORITY_NORMAL]) + bytes(packet.write_to())


def build_login(
    shared: SharedKey,
    repeater_pubkey: bytes,
    out_path: bytes,
    out_path_len: int,
    password: str = "",
) -> tuple[bytes, int]:
    """(command body, tag). The one place the never-a-real-password rule is enforced."""
    if password not in SHARED_PASSWORDS:
        raise UnsafeSharedPassword(
            "the shared guest key is only ever used with a blank or 'hello' password"
        )
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    packet = PacketBuilder.create_login_packet(contact, shared.identity(), password)
    # The timestamp the builder just used, which the reply echoes.
    return _frame(packet), int(PacketBuilder._last_unique_timestamp)


def build_status_request(
    shared: SharedKey, repeater_pubkey: bytes, out_path: bytes, out_path_len: int
) -> tuple[bytes, int]:
    """(command body, tag)."""
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    packet, tag = PacketBuilder.create_protocol_request(
        contact, shared.identity(), REQ_TYPE_GET_STATUS, route_type="direct"
    )
    return _frame(packet), tag


def build_telemetry_request(
    shared: SharedKey, repeater_pubkey: bytes, out_path: bytes, out_path_len: int
) -> tuple[bytes, int]:
    """(command body, tag)."""
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    packet, tag = PacketBuilder.create_telem_request(
        contact, shared.identity(), want_location=False, route_type="direct"
    )
    return _frame(packet), tag


def build_route_update(
    shared: SharedKey,
    repeater_pubkey: bytes,
    out_path: bytes,
    out_path_len: int,
    return_path: bytes = b"",
    return_path_len: int | None = None,
) -> bytes:
    """A PATH packet teaching the repeater its route back to the key.

    A key freshly added by login has no return route, so the repeater floods its
    replies until this arrives. ``return_path`` is the hops from the repeater
    back (empty for a direct neighbour); ``out_path`` is how this packet gets there.
    """
    if out_path_len < 0:
        raise SharedKeyError("the shared key is never flooded; pin a route first")
    secret = Identity(repeater_pubkey).calc_shared_secret(shared.identity().get_private_key())
    packet = PacketBuilder.create_path_return(
        dest_hash=repeater_pubkey[0],
        src_hash=shared.hash_byte,
        secret=secret,
        path=return_path,
        path_len_encoded=return_path_len,
    )
    # create_path_return marks the packet flood, and nothing we send is ever
    # flooded: send it direct, carrying the outer path if it is one hop.
    packet.header = PacketBuilder._create_header(PAYLOAD_TYPE_PATH, route_type="direct")
    if out_path_len > 0 and out_path:
        packet.set_path(out_path, out_path_len)
    return _frame(packet)


def build_admin_removal(
    shared: SharedKey, repeater_pubkey: bytes, out_path: bytes, out_path_len: int
) -> bytes:
    """``setperm <key> 0``: deletes the key's own ACL entry, undoing an admin grant.

    Admin-only, which is why it is sent while the key still has the rights it is
    giving up.
    """
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    command = f"setperm {shared.public_key.hex()} 0"
    # The text-message builder validates the route as a list of hop bytes (the
    # other builders take bytes); with raw bytes it refused every one-hop route.
    contact.out_path = list(contact.out_path)
    packet, _crc = PacketBuilder.create_text_message(
        contact, shared.identity(), command, message_type="direct", txt_type=TXT_TYPE_CLI_COMMAND
    )
    return _frame(packet)


def decode_rx(frame: bytes, shared: SharedKey, repeaters: list[bytes]) -> Reply | None:
    """The reply in a raw RX push, if it is addressed to the key and a candidate decrypts it.

    The 2-byte MAC rules out the wrong repeater among those sharing the 1-byte hash.
    """
    if len(frame) <= _RX_HEADER or frame[0] != PUSH_CODE_LOG_RX_DATA:
        return None
    packet = Packet()
    try:
        if not packet.read_from(frame[_RX_HEADER:]):
            return None
    except (ValueError, IndexError):
        return None
    payload = packet.get_payload()
    if len(payload) < 4 or payload[0] != shared.hash_byte:
        return None  # not addressed to the shared key
    ptype = packet.get_payload_type()
    if ptype not in (PAYLOAD_TYPE_RESPONSE, PAYLOAD_TYPE_PATH):
        return None
    src_hash = payload[1]
    encrypted = payload[2:]
    local_private = shared.identity().get_private_key()
    for pubkey in repeaters:
        if pubkey[0] != src_hash:
            continue
        secret = Identity(pubkey).calc_shared_secret(local_private)
        try:
            plaintext = CryptoUtils.mac_then_decrypt(secret[:16], secret, encrypted)
        except ValueError:
            continue  # wrong repeater for this hash, or not ours
        if ptype == PAYLOAD_TYPE_PATH:
            plaintext = _unwrap_path(plaintext)
            if plaintext is None:
                return None
        reply = _parse_response(pubkey, plaintext)
        if reply is not None and packet.is_route_flood():
            reply = Reply(reply.repeater_pubkey, reply.tag, reply.data, flooded=True)
        return reply
    return None


def _unwrap_path(plaintext: bytes) -> bytes | None:
    """The inner RESPONSE of a PATH-wrapped reply, or None if it carries none."""
    if len(plaintext) < 1 or not PathUtils.is_valid_path_len(plaintext[0]):
        return None
    path_bytes = PathUtils.get_path_byte_len(plaintext[0])
    inner = 1 + path_bytes + 1
    if len(plaintext) <= inner or (plaintext[1 + path_bytes] & 0x0F) != PAYLOAD_TYPE_RESPONSE:
        return None
    return plaintext[inner:]


def _parse_response(pubkey: bytes, plaintext: bytes) -> Reply | None:
    """Split a decrypted RESPONSE into its tag and the bytes after it."""
    if len(plaintext) < 4:
        return None
    return Reply(pubkey, int.from_bytes(plaintext[:4], "little"), plaintext[4:])


@dataclass(frozen=True)
class LoginResult:
    is_admin: bool
    permissions: int


def login_result(reply: Reply) -> LoginResult | None:
    """Read a reply as a login answer, or None if its shape isn't one.

    A login reply carries the repeater's clock, not the request tag, so it is
    told by shape: response code, keep-alive, admin code, permissions. The
    response code is always 0, which rules out telemetry: CayenneLPP's voltage
    high byte is 1 for 2.56-5.11 V and once read as "admin". Callers still skip
    replies whose tag is one of their own requests'.
    """
    body = reply.data
    if len(body) < 4:
        return None
    response_code, _keep_alive, admin_code, permissions = body[0], body[1], body[2], body[3]
    if response_code != RESP_SERVER_LOGIN_OK or admin_code > 3:
        return None
    return LoginResult(is_admin=admin_code == LOGIN_ADMIN_CODE_ADMIN, permissions=permissions)
