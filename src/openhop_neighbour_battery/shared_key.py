"""The shared guest key: poll a repeater as one public guest identity.

A request sent under a shared, publicly known guest key is encrypted to that
key, so the reply is too. Any observer that holds the key can then decode the
reply off the air and feed CoreScope, whether or not it reached the observer
that asked. This is opt-in per repeater and deliberately guest-only.

The key's private half is public by design, so it must never be let near
admin. Two rules keep it safe and both live here, at the point the packets are
built, not in the caller:

  * A login under the shared key only ever carries a password in
    ``SHARED_PASSWORDS`` (blank, or the throwaway "hello"). ``build_login``
    refuses anything else. A repeater with a real saved password therefore
    can't be polled this way, and no real password is ever put on the air under
    a key everyone can read.
  * If a repeater ever answers the shared key as admin (because someone, once,
    logged in to it with its admin password under this key), the caller stops
    using the key there and runs the removal drill: ``build_admin_removal``
    builds the one CLI command that deletes the key's own ACL entry, sent while
    the key still has the admin rights needed to delete them, then a fresh login
    confirms it is back to guest.

Everything is sent as a finished raw packet (CMD_SEND_RAW_PACKET): the plugin
builds and encrypts each one itself with openhop_core's packet builder and a
LocalIdentity of the shared key, and openHop transmits it verbatim — no
contact lookup, no flood, no retries of its own. Replies are read straight off
the air from the raw RX log push (0x88).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from openhop_core.protocol import CryptoUtils
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_PATH,
    PAYLOAD_TYPE_RESPONSE,
    REQ_TYPE_GET_STATUS,
    REQ_TYPE_GET_TELEMETRY_DATA,
    TXT_TYPE_CLI_COMMAND,
)
from openhop_core.protocol.constants import PAYLOAD_TYPE_PATH as _PT_PATH
from openhop_core.protocol.identity import Identity, LocalIdentity
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.packet_builder import PacketBuilder
from openhop_core.protocol.packet_utils import PathUtils

# The only passwords the shared key is ever allowed to carry. Blank means "am I
# already known here?" and keeps whatever role the repeater already holds for
# the key; "hello" is a throwaway a careless owner might set as a guest password
# and is tolerated so those repeaters still work. Neither is ever a real secret,
# and a real admin password is never in this set. Checked by exact match.
SHARED_PASSWORDS = ("", "hello")

# CMD_SEND_RAW_PACKET body is [priority][raw packet]; a status/telemetry poll is
# not urgent, so it goes at normal priority behind anything already queued.
PRIORITY_NORMAL = 0

# PUSH_CODE_LOG_RX_DATA frame: [0x88][snr][rssi][raw packet].
PUSH_CODE_LOG_RX_DATA = 0x88
_RX_HEADER = 3

# A login reply's admin_code; only this one value is admin (a room server sends
# 2 for a plain guest, so bool() would misread it).
LOGIN_ADMIN_CODE_ADMIN = 1
RESP_SERVER_LOGIN_OK = 0

class UnsafeSharedPassword(Exception):
    """A password that is not blank or "hello" was offered for the shared key."""


class SharedKeyError(Exception):
    """A shared-key packet could not be built."""


@dataclass(frozen=True)
class SharedKey:
    """The shared guest identity: a name, a version and an Ed25519 keypair.

    ``private_key`` is a 32-byte seed or MeshCore's 64-byte expanded key;
    LocalIdentity accepts either. The public half is the routing address
    everyone shares.
    """

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
    def from_api(cls, body: dict) -> "SharedKey | None":
        """A SharedKey from CoreScope's /api/shared-guest/key payload, or None.

        CoreScope is the one holder of the key; the plugin fetches it rather
        than keeping its own. None when the payload is missing a usable keypair,
        so the caller simply has no shared key and falls back to its own.
        """
        if not isinstance(body, dict):
            return None
        pub = str(body.get("public_key") or "")
        priv = str(body.get("private_key") or "")
        try:
            key = cls(str(body.get("name") or "shared guest"), int(body.get("version") or 0),
                      bytes.fromhex(pub), bytes.fromhex(priv))
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
    """A decoded reply addressed to the shared key, from one repeater.

    ``data`` is the bytes after the 4-byte tag: RepeaterStats, CayenneLPP or a
    login reply struct. Which it is can't be told from the bytes alone (a
    telemetry reply can look like an admin login once padded), so the kind is
    never guessed here. The caller knows from the tag it sent, or from the
    request it overheard, and calls ``login_result`` only for a login's tag.
    """

    repeater_pubkey: bytes
    tag: int  # the request timestamp the reply echoes
    data: bytes
    # The repeater flooded it: it had no stored route back to the key.
    flooded: bool = False


def _contact(repeater_pubkey: bytes, out_path: bytes, out_path_len: int) -> SimpleNamespace:
    """A minimal contact for the packet builder: just key and route.

    ``out_path_len`` is the encoded path-len byte (0 for a direct, zero-hop
    neighbour); -1 would flood, which the shared key never does, so a route
    must always be given.
    """
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
    return bytes([PRIORITY_NORMAL]) + packet.write_to()


def build_login(shared: SharedKey, repeater_pubkey: bytes, out_path: bytes, out_path_len: int,
                password: str = "") -> tuple[bytes, int]:
    """A login packet under the shared key. Returns (command body, tag).

    Refuses any password outside SHARED_PASSWORDS: the one place the never-a-real-
    password rule is enforced, so no caller can route around it.
    """
    if password not in SHARED_PASSWORDS:
        raise UnsafeSharedPassword(
            "the shared guest key is only ever used with a blank or 'hello' password"
        )
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    packet = PacketBuilder.create_login_packet(contact, shared.identity(), password)
    return _frame(packet), _login_tag(packet)


def _login_tag(packet: Packet) -> int:
    """The timestamp a login carries, echoed back in its reply, for matching.

    An anon login's plaintext begins with the 4-byte timestamp after the
    sender's public key; rather than re-parse the ciphertext we read it back
    from the builder's monotonic clock, which is what it used.
    """
    return PacketBuilder._last_unique_timestamp


def build_status_request(shared: SharedKey, repeater_pubkey: bytes, out_path: bytes,
                         out_path_len: int) -> tuple[bytes, int]:
    """A repeater-stats request under the shared key. Returns (command body, tag)."""
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    packet, tag = PacketBuilder.create_protocol_request(
        contact, shared.identity(), REQ_TYPE_GET_STATUS, route_type="direct"
    )
    return _frame(packet), tag


def build_telemetry_request(shared: SharedKey, repeater_pubkey: bytes, out_path: bytes,
                            out_path_len: int) -> tuple[bytes, int]:
    """A telemetry request under the shared key. Returns (command body, tag)."""
    contact = _contact(repeater_pubkey, out_path, out_path_len)
    packet, tag = PacketBuilder.create_telem_request(
        contact, shared.identity(), want_location=False, route_type="direct"
    )
    return _frame(packet), tag


def build_route_update(shared: SharedKey, repeater_pubkey: bytes, out_path: bytes, out_path_len: int,
                       return_path: bytes = b"", return_path_len: int | None = None) -> bytes:
    """A PATH packet teaching the repeater its route back to the shared key.

    A key freshly added by login has no return route (firmware
    ClientACL.putClient sets OUT_PATH_UNKNOWN), so the repeater floods its first
    replies. Sending this once, direct, after the first login makes it reply
    direct instead. ``return_path`` is the hops from the repeater back to the
    shared key (empty for a direct neighbour); ``out_path``/``out_path_len`` is
    how this packet reaches the repeater (zero-hop direct, or one relay).

    MeshCore builds a PATH return as a flood; this node's neighbours are in
    range, so it is sent direct to keep it off the wider mesh.
    """
    if out_path_len < 0:
        raise SharedKeyError("the shared key is never flooded; pin a route first")
    secret = Identity(repeater_pubkey).calc_shared_secret(shared.identity().get_private_key())
    packet = PacketBuilder.create_path_return(
        dest_hash=repeater_pubkey[0], src_hash=shared.hash_byte, secret=secret,
        path=return_path, path_len_encoded=return_path_len,
    )
    # create_path_return marks the packet flood; send it direct to the repeater
    # instead (it is a neighbour), and carry the outer path if it is one hop.
    packet.header = PacketBuilder._create_header(_PT_PATH, route_type="direct")
    if out_path_len > 0 and out_path:
        packet.set_path(out_path, out_path_len)
    return _frame(packet)


def build_admin_removal(shared: SharedKey, repeater_pubkey: bytes, out_path: bytes,
                        out_path_len: int) -> bytes:
    """The CLI command that deletes the shared key's own ACL entry.

    Only built as part of the removal drill, after a repeater has answered the
    shared key as admin. ``setperm <key> 0`` removes an ACL entry and saves
    (simple_repeater ClientACL), undoing the admin grant for everyone. It is an
    admin-only command, which is exactly why it is sent while the key still has
    the admin rights it is giving up.
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
    """Decode a raw RX push (0x88) if it is a reply addressed to the shared key.

    Returns None for anything else: a different hash, a packet type we don't
    read, or a payload that no candidate repeater's key decrypts (the 2-byte
    MAC rules out the wrong repeater among those sharing the 1-byte hash).
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

    A login reply carries the repeater's own clock first, not the request tag
    (simple_repeater MyMesh.cpp), so it can't be matched by tag; instead it is
    told apart by shape. After that 4-byte clock it is response_code(1) +
    keep_alive(1) + admin_code(1) + permissions(1) + random(4), and the
    response code is always RESP_SERVER_LOGIN_OK (0). That 0 is what rules out
    a telemetry reply: CayenneLPP starts with channel 1, then the voltage type
    and its high byte, which is 1 for 2.56–5.11 V and so read as "admin" when
    only codes 0 and 1 were accepted. Callers still skip replies whose tag is
    one of their own requests'.
    """
    body = reply.data
    if len(body) < 4:
        return None
    response_code, _keep_alive, admin_code, permissions = body[0], body[1], body[2], body[3]
    if response_code != RESP_SERVER_LOGIN_OK or admin_code > 3:
        return None
    return LoginResult(is_admin=admin_code == LOGIN_ADMIN_CODE_ADMIN, permissions=permissions)
