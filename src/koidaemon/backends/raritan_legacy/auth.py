# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Login for the Dominion KX.

Login happens inside SSL and has two stages.

1. A mutual challenge-response.  Each side sends 128 random bytes (two
   64-byte halves) and proves it knows a shared 24-byte "private key".  On
   a stock switch that key is 24 zero bytes.  The second halves of the two
   random blocks also produce the RC4 key used after login.

2. An access request, which carries a RADIUS packet with the user name and
   either the password (PAP) or a CHAP response.  The switch says which one
   it wants in the server id.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass

from koidaemon.backends.raritan_legacy import packets

DEFAULT_PRIVATE_KEY = bytes(24)
CLIENT_LABEL = b"TeleReach Client"
SERVER_LABEL = b"TeleReach Server"

# RADIUS codes and attribute types (RFC 2865).
RADIUS_ACCESS_REQUEST = 1
RADIUS_ACCESS_ACCEPT = 2
RADIUS_ACCESS_REJECT = 3
RADIUS_ACCESS_CHALLENGE = 11
ATTR_USER_NAME = 1
ATTR_USER_PASSWORD = 2
ATTR_CHAP_PASSWORD = 3

# Set in the access response flags when the password must be changed.
FLAG_PASSWORD_EXPIRED = 0x1000


class LoginError(Exception):
    """The switch refused the login or did not prove its identity."""


@dataclass
class ClientChallenge:
    """Our half of the challenge: two 64-byte random blocks."""

    r1: bytes
    r2: bytes

    @classmethod
    def generate(cls) -> "ClientChallenge":
        return cls(os.urandom(64), os.urandom(64))

    def pack(self) -> bytes:
        flags = struct.pack(">I", 0)
        return packets.pack_command(packets.CMD_CLIENT_CHALLENGE, flags + self.r1 + self.r2)


@dataclass
class ServerChallenge:
    """The switch's half of the challenge, R3 and R4."""

    r3: bytes
    r4: bytes

    @classmethod
    def parse(cls, packet: bytes) -> "ServerChallenge":
        if len(packet) != 136 or packet[2] != packets.RSP_SERVER_CHALLENGE:
            raise LoginError("unexpected server challenge: %s" % packet[:8].hex())
        return cls(packet[8:72], packet[72:136])


@dataclass
class ServerResponse:
    """The switch's proof of identity, plus the CHAP challenge."""

    digest: bytes
    chap_challenge: bytes
    chap_id: int

    @classmethod
    def parse(cls, packet: bytes) -> "ServerResponse":
        if len(packet) != 41 or packet[2] != packets.RSP_SERVER_RESPONSE:
            raise LoginError("unexpected server response: %s" % packet[:8].hex())
        return cls(packet[8:24], packet[24:40], packet[40])


def client_response(ours: ClientChallenge, theirs: ServerChallenge,
                    private_key: bytes = DEFAULT_PRIVATE_KEY) -> bytes:
    """CLIENT_RESPONSE: MD5 over our R1 and the switch's R3."""
    digest = hashlib.md5(CLIENT_LABEL + ours.r1 + theirs.r3 + private_key).digest()
    flags = struct.pack(">I", 0)
    return packets.pack_command(packets.CMD_CLIENT_RESPONSE, flags + digest)


def check_server_response(response: ServerResponse, ours: ClientChallenge,
                          theirs: ServerChallenge,
                          private_key: bytes = DEFAULT_PRIVATE_KEY) -> None:
    """Make sure the switch knows the shared private key."""
    expected = hashlib.md5(SERVER_LABEL + ours.r2 + theirs.r4 + private_key).digest()
    if response.digest != expected:
        raise LoginError("the switch failed the challenge (wrong private key?)")


def session_key(ours: ClientChallenge, theirs: ServerChallenge,
                private_key: bytes = DEFAULT_PRIVATE_KEY) -> bytes:
    """The RC4 key used for the rest of the session."""
    return hashlib.md5(ours.r2 + theirs.r4 + private_key).digest()


def chap_password(chap_id: int, password: bytes, challenge: bytes) -> bytes:
    """RADIUS CHAP-Password value: id, then MD5(id + password + challenge)."""
    digest = hashlib.md5(bytes([chap_id]) + password + challenge).digest()
    return bytes([chap_id]) + digest


def pap_password(password: bytes) -> bytes:
    """RADIUS User-Password value as the switch expects it.

    There is no RADIUS shared secret here, so the password is sent as is,
    zero-padded to a multiple of 16 bytes (at least 16).  It is still
    protected by the SSL connection.
    """
    padded_length = max(16, (len(password) + 15) // 16 * 16)
    return password.ljust(padded_length, b"\x00")


def radius_attribute(attribute_type: int, value: bytes) -> bytes:
    return bytes([attribute_type, 2 + len(value)]) + value


def access_request(user: str, password: str, server: packets.ServerId,
                   response: ServerResponse) -> bytes:
    """ACCESS_REQUEST: flags, permissions, then a RADIUS Access-Request."""
    user_bytes = user.encode("utf-8")
    password_bytes = password.encode("utf-8")

    attributes = radius_attribute(ATTR_USER_NAME, user_bytes)
    if server.uses_chap:
        value = chap_password(response.chap_id, password_bytes, response.chap_challenge)
        attributes += radius_attribute(ATTR_CHAP_PASSWORD, value)
    else:
        attributes += radius_attribute(ATTR_USER_PASSWORD, pap_password(password_bytes))

    # Code, identifier, length, then a 16-byte authenticator that the switch
    # leaves as zeros.
    radius = struct.pack(">BBH", RADIUS_ACCESS_REQUEST, 0, 20 + len(attributes))
    radius += bytes(16) + attributes

    flags_and_permissions = struct.pack(">II", 0, 0)
    return packets.pack_command(packets.CMD_ACCESS_REQUEST, flags_and_permissions + radius)


def check_access_response(packet: bytes) -> None:
    """Raise LoginError unless the switch accepted the login."""
    if packet[2] != packets.RSP_ACCESS_RESPONSE:
        raise LoginError("unexpected access response: %s" % packet[:8].hex())

    flags = struct.unpack(">I", packet[4:8])[0]
    radius_code = packet[12]

    if radius_code == RADIUS_ACCESS_ACCEPT:
        if flags & FLAG_PASSWORD_EXPIRED:
            raise LoginError("login accepted but the password has expired; "
                             "change it with the Raritan client first")
        return
    if radius_code == RADIUS_ACCESS_REJECT:
        raise LoginError("wrong user name or password")
    raise LoginError("login not accepted (RADIUS code %d)" % radius_code)
