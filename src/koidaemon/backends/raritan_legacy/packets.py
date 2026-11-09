# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Wire formats used by the Dominion KX on TCP port 5000.

Everything is big-endian.  There are two framings:

CSC messages
    Used in the clear right after connecting.  A 4-byte length (which
    counts itself) followed by a NUL-terminated XML string.

Commands
    Everything after the CSC exchange.  A 2-byte length (which counts the
    whole packet), a 1-byte command id, a 1-byte packet id, then the
    payload.  Client-to-switch command ids and switch-to-client response
    ids are separate number spaces: 43 means one thing when we send it and
    another when the switch does.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

COMMAND_HEADER_SIZE = 4

# Command ids sent by the client.
CMD_NOP_REPLY = 2           # the ACK we send in answer to a NOP
CMD_PING = 3
CMD_CLIENT_CHALLENGE = 4
CMD_CLIENT_RESPONSE = 5
CMD_ACCESS_REQUEST = 6
CMD_LOGOUT = 7
CMD_STOP_VIDEO_STREAM = 10
CMD_KEYBOARD = 17
CMD_MOUSE = 18
CMD_IDENTIFY_REMOTE = 42
CMD_HANDSHAKE_ACK = 43
CMD_REFRESH = 47
CMD_VIDEO_SYNC = 48
CMD_DATABASE_REQUEST = 54
CMD_CONNECT_VIDEO_STREAM = 55

# Response ids sent by the switch.
RSP_NOP = 0
RSP_ACK = 2
RSP_NACK = 3
RSP_PONG = 5
RSP_SERVER_CHALLENGE = 7
RSP_ACCESS_RESPONSE = 9
RSP_SERVER_RESPONSE = 28
RSP_VIDEO_MARKER = 31
RSP_KEYBOARD_STATUS = 32
RSP_DATABASE_RESPONSE = 36
RSP_VIDEO_CONNECTED = 37
RSP_NEW_VIDEO_MODE = 39
RSP_CELL_DATA = 45

VIDEO_PACKETS = (RSP_NEW_VIDEO_MODE, RSP_CELL_DATA)
RSP_HANDSHAKE_ACK = 43      # only during the handshake
RSP_NOTIFY = 43             # after login: event notifications

# NOTIFY events.  After a port is connected the switch "autosenses" the
# video, reporting progress as NOTIFY_AUTOSENSE with an increasing
# parameter, then AUTOSENSE_DONE.  Key presses sent before that are lost.
NOTIFY_AUTOSENSE = 0
NOTIFY_NO_VIDEO = 3
AUTOSENSE_DONE = 0xFFFFFFFF

DATABASE_RESPONSE_HEADER_SIZE = 18

# Signatures in the handshake acknowledgement.  The switch sends the first
# one; the client echoes it on the read socket and sends the second one on
# the write socket.
READ_SOCKET_SIGNATURE = 0x5C3D98A2
WRITE_SOCKET_SIGNATURE = 0x47BBF83D
SERVER_SIGNATURES = (0x5C3D98A2, 0x8B434EC7)

# securityFlags in the server id.
SECURITY_ENCRYPTION_MASK = 0x0F00
SECURITY_NO_SSL = 0x0100
SECURITY_SSL_LOGIN_ONLY = 0x0200
SECURITY_SSL_LOGIN_THEN_RC4 = 0x0400
SECURITY_SSL_ALWAYS = 0x0800
SECURITY_LOGIN_MASK = 0xF000
SECURITY_LOGIN_PAP = 0x0000

# options in the server id: set when the switch wants USB HID key codes,
# clear when it wants PS/2 scan codes.
OPTION_HID_KEYBOARD = 0x4

PROTOCOL_VERSION = 30


class ProtocolError(Exception):
    """The switch sent something we did not expect."""


# ----------------------------------------------------------------------
# CSC messages
# ----------------------------------------------------------------------

def pack_csc(text: str) -> bytes:
    """Frame a CSC message: 4-byte length, the text, a NUL."""
    body = text.encode("ascii") + b"\x00"
    return struct.pack(">I", 4 + len(body)) + body


def csc_length(header: bytes) -> int:
    """Body length of a CSC message, given its 4-byte header."""
    total = struct.unpack(">I", header)[0]
    if total < 4:
        raise ProtocolError("CSC message length %d is too short" % total)
    return total - 4


# ----------------------------------------------------------------------
# Server id
# ----------------------------------------------------------------------

SERVER_ID_SIZE = 74


@dataclass
class ServerId:
    """The 74-byte description of the switch sent before SSL starts."""

    name: str
    ip_address: str
    port: int
    protocol_version: int
    oldest_protocol_version: int
    security_flags: int
    options: int

    @classmethod
    def parse(cls, data: bytes) -> "ServerId":
        if len(data) != SERVER_ID_SIZE:
            raise ProtocolError("server id is %d bytes, expected %d" % (len(data), SERVER_ID_SIZE))

        name = data[4:20].split(b"\x00")[0].decode("ascii", "replace")
        ip_address = ".".join(str(octet) for octet in data[20:24])
        port, version, oldest = struct.unpack(">HHH", data[24:30])
        security_flags, options = struct.unpack(">II", data[40:48])

        return cls(name, ip_address, port, version, oldest, security_flags, options)

    @property
    def encryption(self) -> int:
        return self.security_flags & SECURITY_ENCRYPTION_MASK

    @property
    def uses_chap(self) -> bool:
        return (self.security_flags & SECURITY_LOGIN_MASK) != SECURITY_LOGIN_PAP

    @property
    def wants_hid_keycodes(self) -> bool:
        return bool(self.options & OPTION_HID_KEYBOARD)


# ----------------------------------------------------------------------
# Handshake acknowledgement
# ----------------------------------------------------------------------

HANDSHAKE_ACK_SIZE = 34


@dataclass
class HandshakeAck:
    """The 34-byte handshake packet exchanged on both sockets.

    The switch sends one on the read socket.  The client sends it back on
    the read socket, then sends a copy with a different signature on the
    write socket.  Only the signature and checksum change.
    """

    packet_id: int
    signature: int
    client_ip: int
    timestamp: int
    index: int
    random: int
    connection_count: int
    checksum: int
    client_port: int

    @classmethod
    def parse(cls, data: bytes) -> "HandshakeAck":
        if len(data) != HANDSHAKE_ACK_SIZE:
            raise ProtocolError("handshake ack is %d bytes" % len(data))

        length, command, packet_id = struct.unpack(">HBB", data[:4])
        if length != HANDSHAKE_ACK_SIZE or command != RSP_HANDSHAKE_ACK:
            raise ProtocolError("not a handshake ack: %s" % data[:4].hex())

        fields = struct.unpack(">IIIIIIIH", data[4:])
        return cls(packet_id, *fields)

    def compute_checksum(self) -> int:
        total = (self.signature + self.client_ip + self.client_port + self.timestamp
                 + self.index + self.random + self.connection_count)
        return total & 0xFFFFFFFF

    def with_signature(self, signature: int) -> "HandshakeAck":
        """A copy with a new signature and a matching checksum."""
        copy = HandshakeAck(**self.__dict__)
        copy.signature = signature
        copy.checksum = copy.compute_checksum()
        return copy

    def pack(self) -> bytes:
        header = struct.pack(">HBB", HANDSHAKE_ACK_SIZE, CMD_HANDSHAKE_ACK, self.packet_id)
        body = struct.pack(">IIIIIIIH", self.signature, self.client_ip, self.timestamp,
                           self.index, self.random, self.connection_count,
                           self.checksum, self.client_port)
        return header + body


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def pack_command(command: int, payload: bytes = b"", packet_id: int = 0) -> bytes:
    """Frame a command: total length, command id, packet id, payload."""
    length = COMMAND_HEADER_SIZE + len(payload)
    if length > 0xFFFF:
        raise ValueError("command payload too large: %d bytes" % len(payload))
    return struct.pack(">HBB", length, command, packet_id) + payload


def parse_command_header(header: bytes) -> tuple[int, int, int]:
    """Split a 4-byte command header into (length, command, packet id)."""
    return struct.unpack(">HBB", header)


def identify_remote() -> bytes:
    """IDENTIFY_REMOTE: tells the switch which protocol version we speak."""
    payload = struct.pack(">I", PROTOCOL_VERSION) + bytes(28)
    return pack_command(CMD_IDENTIFY_REMOTE, payload)
