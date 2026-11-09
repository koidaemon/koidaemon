# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A fake Dominion KX that runs the protocol on localhost.

It plays the switch's side of everything KXSession does: the CSC greeting,
SSL 3.0, the handshake acknowledgements, the challenge-response login, the
switch to RC4, port listing, connecting a port, and receiving keyboard and
mouse packets.  It checks what the client sends as it goes and records
what it received, so tests can assert on it afterwards.
"""

from __future__ import annotations

import hashlib
import os
import socket
import struct
import threading
import xml.etree.ElementTree as ElementTree

from koidaemon.backends.raritan_legacy import packets
from koidaemon.backends.raritan_legacy.rc4 import RC4
from koidaemon.backends.raritan_legacy.ssl3 import recv_exact

from fake_ssl3_server import SSL3Server

ZERO_KEY = bytes(24)

PORTS_XML = (
    '<Database><Get><Data>\n'
    '<Port id="Admin" Class="HTML" Type="Admin" Status="1" index="0"><Name>Admin</Name></Port>\n'
    '<Port id="AA_FG0" Class="KVM" Type="FG" Status="1" index="0" Connection="P_TEST">'
    '<Name></Name></Port>\n'
    '</Data></Get></Database>'
)
SWITCH_XML = (
    '<Database><Get><Data>\n'
    '<Device id="P_TEST" Type="Paragon"><Name>Test</Name>\n'
    '<Port id="P_TEST_0" Class="KVM" Type="PC" Status="1" index="0"><Name>%s</Name>'
    '<XMouseScaleFactor>1.0</XMouseScaleFactor><YMouseScaleFactor>1.0</YMouseScaleFactor>'
    '</Port>\n'
    '<Port id="P_TEST_1" Class="KVM" Type="PC" Status="0" index="1"><Name>%s</Name>'
    '<XMouseScaleFactor>1.5</XMouseScaleFactor><YMouseScaleFactor>2.0</YMouseScaleFactor>'
    '</Port>\n'
    '</Device>\n'
    '</Data></Get></Database>'
)


class _Stream:
    """A socket that is plain, SSL or RC4, mirroring the client's Channel."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.ssl = None
        self.send_rc4 = None
        self.recv_rc4 = None

    def sendall(self, data: bytes) -> None:
        if self.ssl is not None:
            self.ssl.sendall(data)
        elif self.send_rc4 is not None:
            self.sock.sendall(self.send_rc4.crypt(data))
        else:
            self.sock.sendall(data)

    def recv_exact(self, length: int) -> bytes:
        if self.ssl is not None:
            return self.ssl.recv_exact(length)
        data = recv_exact(self.sock, length)
        if self.recv_rc4 is not None:
            data = self.recv_rc4.crypt(data)
        return data

    def read_packet(self) -> bytes:
        header = self.recv_exact(4)
        length = struct.unpack(">H", header[:2])[0]
        return header + self.recv_exact(length - 4)


def server_id(security_flags: int, options: int = 0x2) -> bytes:
    name = b"Fake-KX".ljust(16, b"\x00")
    data = struct.pack(">I", packets.SERVER_ID_SIZE) + name
    data += bytes([127, 0, 0, 1]) + struct.pack(">HHHH", 5000, 30, 20, 0)
    data += struct.pack(">III", 0, 0, security_flags)
    data += struct.pack(">I", options)
    data += bytes(packets.SERVER_ID_SIZE - len(data))
    return data


class FakeKX:
    """Run with `with FakeKX() as kx:`; connect a KXSession to kx.port."""

    def __init__(self, password: str = "secret", security_flags: int = 0x1401,
                 options: int = 0x2) -> None:
        self.password = password
        self.security_flags = security_flags
        self.options = options

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]

        self.login_user = None
        self.connect_xml = None
        self.connect_params = None
        self.port_names = ["Board A", "Board B"]
        # Be busy for this many logins (too many sessions) and port
        # connections (video channel in use) before accepting them.
        self.busy_logins = 0
        self.busy_connects = 0
        self.login_attempts = 0
        self.connect_attempts = 0
        # Database updates received, and what to answer them with.  None
        # applies the update the way the real switch does.
        self.updates: list[str] = []
        self.update_reply: str | None = None
        self.keyboard_packets: list[bytes] = []
        self.mouse_packets: list[bytes] = []
        self.logged_out = threading.Event()
        self.error: Exception | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "FakeKX":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._thread.join(timeout=10)
        self.listener.close()

    # ------------------------------------------------------------------

    def _greet(self, sock: socket.socket) -> _Stream:
        sock.sendall(packets.pack_csc("<CSC/>"))
        ack_length = packets.csc_length(recv_exact(sock, 4))
        assert recv_exact(sock, ack_length) == b"<CSC_Ack/>\x00"
        sock.sendall(packets.pack_csc('<CSC_Info><Device Type="IP-Reach"/></CSC_Info>'))
        start_length = packets.csc_length(recv_exact(sock, 4))
        assert b"CSC_Start_Session" in recv_exact(sock, start_length)
        sock.sendall(server_id(self.security_flags, self.options))

        stream = _Stream(sock)
        if self.security_flags & packets.SECURITY_ENCRYPTION_MASK != packets.SECURITY_NO_SSL:
            stream.ssl = SSL3Server(sock)
            stream.ssl.handshake()
        return stream

    def _run(self) -> None:
        try:
            self._serve()
        except Exception as error:  # surfaced to the test through self.error
            self.error = error
            self.logged_out.set()

    def _serve(self) -> None:
        # Like the real switch, a busy login is refused after the sockets
        # are paired, and the client has to start again from the top.
        while not self._serve_one_login():
            pass

    def _serve_one_login(self) -> bool:
        """Handle one login attempt; False if it was refused as busy."""
        self.login_attempts += 1
        read_sock, _ = self.listener.accept()
        read = self._greet(read_sock)

        ack = packets.HandshakeAck(packet_id=0, signature=packets.READ_SOCKET_SIGNATURE,
                                   client_ip=0x7F000001, timestamp=1234, index=0,
                                   random=0x11223344, connection_count=4, checksum=0,
                                   client_port=40000)
        ack.checksum = ack.compute_checksum()
        read.sendall(ack.pack())
        echoed = packets.HandshakeAck.parse(read.recv_exact(packets.HANDSHAKE_ACK_SIZE))
        assert echoed.checksum == echoed.compute_checksum()

        write_sock, _ = self.listener.accept()
        write = self._greet(write_sock)
        paired = packets.HandshakeAck.parse(write.recv_exact(packets.HANDSHAKE_ACK_SIZE))
        assert paired.signature == packets.WRITE_SOCKET_SIGNATURE
        assert paired.checksum == paired.compute_checksum()

        if self.busy_logins > 0:
            self.busy_logins -= 1
            busy = struct.pack(">I", 11)
            read.sendall(packets.pack_command(packets.RSP_NACK, busy))
            read_sock.close()
            write_sock.close()
            return False
        read.sendall(packets.pack_command(packets.RSP_ACK))

        identify = write.read_packet()
        assert identify[2] == packets.CMD_IDENTIFY_REMOTE

        if self._login(read, write):
            self._main_loop(read, write)
        return True

    def _login(self, read: _Stream, write: _Stream) -> bool:
        challenge = write.read_packet()
        assert challenge[2] == packets.CMD_CLIENT_CHALLENGE
        r1, r2 = challenge[8:72], challenge[72:136]
        r3, r4 = os.urandom(64), os.urandom(64)
        read.sendall(packets.pack_command(packets.RSP_SERVER_CHALLENGE,
                                          struct.pack(">I", 0) + r3 + r4))

        response = write.read_packet()
        expected = hashlib.md5(b"TeleReach Client" + r1 + r3 + ZERO_KEY).digest()
        assert response[8:24] == expected, "client response digest is wrong"

        chap_challenge = os.urandom(16)
        chap_id = 0x2E
        digest = hashlib.md5(b"TeleReach Server" + r2 + r4 + ZERO_KEY).digest()
        payload = struct.pack(">I", 0) + digest + chap_challenge + bytes([chap_id])
        read.sendall(packets.pack_command(packets.RSP_SERVER_RESPONSE, payload))

        request = write.read_packet()
        assert request[2] == packets.CMD_ACCESS_REQUEST
        accepted = self._check_credentials(request[12:], chap_id, chap_challenge)
        radius_code = 2 if accepted else 3
        radius = struct.pack(">BBH", radius_code, 0, 20) + bytes(16)
        read.sendall(packets.pack_command(packets.RSP_ACCESS_RESPONSE,
                                          struct.pack(">II", 0, 0) + radius))
        if not accepted:
            return False

        # Login done: both directions continue with RC4.
        key = hashlib.md5(r2 + r4 + ZERO_KEY).digest()
        encryption = self.security_flags & packets.SECURITY_ENCRYPTION_MASK
        for stream in (read, write):
            if encryption == packets.SECURITY_SSL_LOGIN_THEN_RC4:
                stream.ssl = None
                stream.send_rc4 = RC4(key)
                stream.recv_rc4 = RC4(key)
            elif encryption == packets.SECURITY_SSL_LOGIN_ONLY:
                stream.ssl = None
        return True

    def _check_credentials(self, radius: bytes, chap_id: int, chap_challenge: bytes) -> bool:
        """Parse the RADIUS attributes and check the password."""
        attributes = {}
        position = 20
        while position < len(radius):
            kind, length = radius[position], radius[position + 1]
            attributes[kind] = radius[position + 2:position + length]
            position += length

        self.login_user = attributes[1].decode()
        password = self.password.encode()
        if 3 in attributes:
            expected = bytes([chap_id]) + hashlib.md5(bytes([chap_id]) + password
                                                       + chap_challenge).digest()
            return attributes[3] == expected
        return attributes[2].rstrip(b"\x00") == password

    def _update(self, query: str) -> str:
        """Apply a port rename and answer it like the switch does."""
        self.updates.append(query)
        if self.update_reply is not None:
            return self.update_reply

        update = ElementTree.fromstring(query).find("Update")
        select = update.findtext("Select")
        name = update.findtext("Data/Name")
        changed = 0
        for index in range(len(self.port_names)):
            if '[@id="P_TEST_%d"]' % index in select:
                self.port_names[index] = name
                changed += 1
        return "<Database><Update>%d</Update></Database>" % changed

    def _main_loop(self, read: _Stream, write: _Stream) -> None:
        while True:
            packet = write.read_packet()
            command, packet_id = packet[2], packet[3]

            if command == packets.CMD_DATABASE_REQUEST:
                query = packet[18:].split(b"\x00")[0].decode()
                if "<Update>" in query:
                    xml = self._update(query)
                elif "P_TEST" in query:
                    xml = SWITCH_XML % tuple(self.port_names)
                else:
                    xml = PORTS_XML
                xml = xml.encode() + b"\x00"
                header = struct.pack(">IIIH", len(xml), len(xml), 0, 0)
                read.sendall(packets.pack_command(packets.RSP_DATABASE_RESPONSE,
                                                  header + xml, packet_id))
            elif command == packets.CMD_CONNECT_VIDEO_STREAM and self.busy_connects > 0:
                self.connect_attempts += 1
                self.busy_connects -= 1
                busy = struct.pack(">I", 20)
                read.sendall(packets.pack_command(packets.RSP_NACK, busy, packet_id))
            elif command == packets.CMD_CONNECT_VIDEO_STREAM:
                self.connect_attempts += 1
                self.connect_params = packet[4:44]
                self.connect_xml = packet[44:].decode()
                read.sendall(packets.pack_command(packets.RSP_VIDEO_CONNECTED, b"\x07", packet_id))
                # Autosense progress, then done, as a real switch does.
                for parameter in (0, 1, packets.AUTOSENSE_DONE):
                    read.sendall(packets.pack_command(packets.RSP_NOTIFY,
                                                      struct.pack(">IIB", 0, parameter, 7)))
            elif command == packets.CMD_KEYBOARD:
                self.keyboard_packets.append(packet)
            elif command == packets.CMD_MOUSE:
                self.mouse_packets.append(packet)
            elif command == packets.CMD_STOP_VIDEO_STREAM:
                read.sendall(packets.pack_command(packets.RSP_ACK, b"", packet_id))
            elif command == packets.CMD_PING:
                read.sendall(packets.pack_command(packets.RSP_PONG))
            elif command == packets.CMD_LOGOUT:
                self.logged_out.set()
                return
