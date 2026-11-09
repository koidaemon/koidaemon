# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A logged-in session with a Dominion KX.

A session uses two TCP connections to port 5000:

  * the read socket, opened first.  After the handshake the switch sends
    everything (replies, notifications, video) here and we never write to it;
  * the write socket.  Every command we send goes here and we never read
    from it.

Connection order, as done by Raritan's client:

  1. read socket: CSC greeting in the clear, then SSL 3.0;
  2. read socket: handshake ack from the switch, which we echo back;
  3. write socket: the same CSC greeting and SSL 3.0;
  4. write socket: the handshake ack again, with the write-socket signature;
  5. read socket: an ACK saying both sockets are paired;
  6. write socket: IDENTIFY_REMOTE, then the login exchange (auth.py);
  7. both sockets switch from SSL to plain RC4 (on a stock switch).

After that the session can list and rename ports, connect to one, and
send keyboard and mouse packets to it.
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass

from koidaemon.backends.raritan_legacy import auth, packets
from koidaemon.backends.raritan_legacy.rc4 import RC4
from koidaemon.backends.raritan_legacy.ssl3 import SSL3Client, recv_exact

LOG = logging.getLogger(__name__)

DEFAULT_PORT = 5000
CONNECT_TIMEOUT = 10.0
REPLY_TIMEOUT = 20.0

# Raritan's client pings when nothing has been sent or received for 29 s,
# and the switch drops idle sessions after about a minute.
KEEPALIVE_INTERVAL = 29.0

# Raritan's client waits this long after login before switching from SSL to
# RC4, presumably to give the switch time to do the same.
ENCRYPTION_SWITCH_DELAY = 0.5

# After connecting to a port the switch spends about a second detecting the
# video mode and drops key presses meanwhile.  Wait for it to finish.
AUTOSENSE_TIMEOUT = 10.0

# The switch forwards input to the target asynchronously; logging out too
# soon after the last key press loses it.
INPUT_DRAIN_TIME = 0.5

# Compression parameters sent when connecting to a port.  These are the
# defaults of Raritan's client; we ignore the video anyway.
DEFAULT_COMPRESSION_PARAMS = struct.pack(
    ">IHHIIIIIIII",
    4,      # flags
    0,      # colour table
    0,      # cache depth
    2,      # compression mode
    0,      # speed
    0,      # minimum frame time
    0,      # maximum frame time
    6,      # smoothing
    0, 0, 0,
)

PORTS_QUERY = ("<Database><Get><Select>/System/Device[@Type='IP-Reach']/Port</Select>"
               "<Nodes>*</Nodes><SubNodes>*</SubNodes></Get></Database>")
DEVICE_QUERY = ("<Database><Get><Select>/System/Device[@id='%s']</Select>"
                "<Nodes>*</Nodes><SubNodes>*</SubNodes></Get></Database>")

# Changing a port's properties, as Raritan's admin client does it: select
# the port by id and send its name and both mouse scale factors.  The
# switch answers with the number of entries it changed.
PORT_UPDATE = ("<Database><Update><Select>/System/Device/Port[@id=\"%s\"]</Select>"
               "<Data><Name>%s</Name>"
               "<XMouseScaleFactor>%s</XMouseScaleFactor>"
               "<YMouseScaleFactor>%s</YMouseScaleFactor></Data></Update></Database>")

# The admin client's rules for port names.  None of these characters need
# escaping in XML.
PORT_NAME_MAX_LENGTH = 20
PORT_NAME_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 .+-/")

# Error codes for the two ways the switch can be busy.  It refuses a new
# login while too many sessions are logged in (8 on the switch this was
# tested with), and it refuses to connect a port while another session is
# connected to a different one, because it has a single video channel.
ERROR_TOO_MANY_SESSIONS = 11
ERROR_CHANNEL_BUSY = 20

TOO_MANY_SESSIONS_MESSAGE = "the switch is busy: too many sessions are logged in"
CHANNEL_BUSY_MESSAGE = ("the switch's video channel is in use: it can only connect one port "
                        "at a time, and another session is connected to a different port")

# How often to try again while waiting for a busy switch.
BUSY_RETRY_INTERVAL = 0.5

# Mouse functions in a MOUSE packet.
MOUSE_LEFT_DOWN = 0
MOUSE_LEFT_UP = 1
MOUSE_RIGHT_DOWN = 2
MOUSE_RIGHT_UP = 3
MOUSE_MIDDLE_DOWN = 4
MOUSE_MIDDLE_UP = 5
MOUSE_MOVE = 6
MOUSE_WHEEL = 8

MOUSE_BUTTONS = {
    "left": (MOUSE_LEFT_DOWN, MOUSE_LEFT_UP),
    "right": (MOUSE_RIGHT_DOWN, MOUSE_RIGHT_UP),
    "middle": (MOUSE_MIDDLE_DOWN, MOUSE_MIDDLE_UP),
}


class SessionError(Exception):
    """Something went wrong talking to the switch."""


class CommandRefused(SessionError):
    """The switch answered a command with a NACK and an error code."""

    def __init__(self, command: int, error: int) -> None:
        super().__init__("the switch refused command %d (error %d)" % (command, error))
        self.command = command
        self.error = error


class SwitchBusyError(SessionError):
    """The switch is busy for now; trying again later can work."""


@dataclass
class TargetPort:
    """One KVM port of the switch, as shown in Raritan's client."""

    number: int         # 1-based, as printed on the switch and in the GUI
    target_id: str      # e.g. "P_HK673000_0"
    portal_id: str      # the video path ("frame grabber") that reaches it
    name: str
    active: bool        # a powered target is attached
    # Mouse scale factors, kept as the switch's own strings (e.g. "1.0") so
    # renaming a port can write them back unchanged.
    x_mouse_scale: str = "1.0"
    y_mouse_scale: str = "1.0"


def check_port_name(name: str) -> None:
    """Raise ValueError unless the switch would accept name for a port."""
    if not name:
        raise ValueError("a port name cannot be empty")
    if len(name) > PORT_NAME_MAX_LENGTH:
        raise ValueError("a port name can be at most %d characters" % PORT_NAME_MAX_LENGTH)
    bad = sorted(set(name) - PORT_NAME_CHARACTERS)
    if bad:
        raise ValueError("a port name can only use letters, digits, spaces and . + - /"
                         " (not %s)" % " ".join(repr(char) for char in bad))


class Channel:
    """One of the two sockets, with whatever encryption is currently on it."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._ssl: SSL3Client | None = None
        self._send_rc4: RC4 | None = None
        self._recv_rc4: RC4 | None = None
        self._pending = b""
        self._send_lock = threading.Lock()

    def start_ssl(self) -> None:
        self._ssl = SSL3Client(self.sock)
        self._ssl.handshake()

    def stop_ssl(self, rc4_key: bytes | None) -> None:
        """Leave SSL and carry on in the clear, or with plain RC4."""
        if self._ssl is not None:
            # Keep anything SSL already decrypted for us.
            self._pending = self._ssl.leftover()
            self._ssl = None
        if rc4_key is not None:
            self._send_rc4 = RC4(rc4_key)
            self._recv_rc4 = RC4(rc4_key)

    def sendall(self, data: bytes) -> None:
        with self._send_lock:
            if self._ssl is not None:
                self._ssl.sendall(data)
                return
            if self._send_rc4 is not None:
                data = self._send_rc4.crypt(data)
            self.sock.sendall(data)

    def recv_exact(self, length: int) -> bytes:
        if self._ssl is not None:
            return self._ssl.recv_exact(length)

        data = self._pending[:length]
        self._pending = self._pending[length:]
        if len(data) < length:
            raw = recv_exact(self.sock, length - len(data))
            if self._recv_rc4 is not None:
                raw = self._recv_rc4.crypt(raw)
            data += raw
        return data

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def read_packet(channel: Channel) -> bytes:
    """Read one complete command packet (header included) from a channel."""
    header = channel.recv_exact(packets.COMMAND_HEADER_SIZE)
    length, command, _packet_id = packets.parse_command_header(header)

    if command == packets.RSP_DATABASE_RESPONSE:
        # Database replies carry their own size field, because the XML can
        # be longer than the 16-bit packet length allows.
        rest_of_header = channel.recv_exact(packets.DATABASE_RESPONSE_HEADER_SIZE - 4)
        size = struct.unpack(">I", rest_of_header[4:8])[0]
        return header + rest_of_header + channel.recv_exact(size)

    if length < packets.COMMAND_HEADER_SIZE:
        raise SessionError("bad packet length %d (command %d)" % (length, command))
    return header + channel.recv_exact(length - packets.COMMAND_HEADER_SIZE)


def _log_retry(attempt: int, reason: str, wait: float) -> None:
    """Say once that we are waiting for a busy switch; repeats are debug."""
    if attempt == 1:
        LOG.info("%s; waiting up to %g s", reason, wait)
    else:
        LOG.debug("still busy, attempt %d", attempt)


class KXSession:
    """A logged-in connection to a Dominion KX.

    Use it as a context manager so the session is always logged out:

        with KXSession("192.0.2.10", "admin", "raritan") as kx:
            kx.connect_port(1)
            Keyboard(kx).type_text("root\\n")

    wait is how many seconds to keep trying while the switch is busy: at
    login if too many sessions are logged in, and in connect_port() if
    another session holds the video channel.  0 fails straight away.
    """

    def __init__(self, host: str, user: str, password: str,
                 tcp_port: int = DEFAULT_PORT, wait: float = 0.0) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.tcp_port = tcp_port
        self.wait = wait

        self.server: packets.ServerId | None = None
        self.device_id: int | None = None

        self._read: Channel | None = None
        self._write: Channel | None = None
        self._reader: threading.Thread | None = None
        self._keepalive: threading.Thread | None = None
        self._closing = threading.Event()
        self._reader_error: Exception | None = None

        self._waiting: dict[int, queue.Queue] = {}
        self._waiting_lock = threading.Lock()
        self._next_packet_id = 1
        self._last_activity = time.monotonic()
        self._last_input: float | None = None
        self._autosense_done = threading.Event()
        self.no_video = False

        # Called from the reader thread with every video packet, when set.
        self.video_listener = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "KXSession":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Connecting and logging in
    # ------------------------------------------------------------------

    def _open_socket(self) -> socket.socket:
        sock = socket.create_connection((self.host, self.tcp_port), timeout=CONNECT_TIMEOUT)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def _negotiate_csc(self, sock: socket.socket) -> packets.ServerId:
        """The plain-text greeting that starts every connection."""
        greeting_length = packets.csc_length(recv_exact(sock, 4))
        recv_exact(sock, greeting_length)
        sock.sendall(packets.pack_csc("<CSC_Ack/>"))

        info_length = packets.csc_length(recv_exact(sock, 4))
        info = recv_exact(sock, info_length).decode("ascii", "replace")
        LOG.debug("CSC info: %s", info)
        if "Dominion_KX2" in info or "Dominion_KSX2" in info:
            raise SessionError("this is a Dominion KX II, which uses a different protocol")

        sock.sendall(packets.pack_csc('<CSC_Start_Session ProtocolID="IP-Reach"/>'))
        return packets.ServerId.parse(recv_exact(sock, packets.SERVER_ID_SIZE))

    def open(self) -> None:
        """Connect both sockets and log in, waiting while the switch is busy."""
        deadline = time.monotonic() + self.wait
        attempt = 0
        while True:
            try:
                self._open_and_login()
                return
            except SwitchBusyError:
                self._close_sockets()
                if time.monotonic() >= deadline:
                    raise
            except Exception:
                self._close_sockets()
                raise
            attempt += 1
            _log_retry(attempt, TOO_MANY_SESSIONS_MESSAGE, self.wait)
            time.sleep(BUSY_RETRY_INTERVAL)

    def _open_and_login(self) -> None:
        read_socket = self._open_socket()
        self.server = self._negotiate_csc(read_socket)
        LOG.info("connected to %s (protocol %d, security 0x%x, options 0x%x)",
                 self.server.name, self.server.protocol_version,
                 self.server.security_flags, self.server.options)

        if self.server.protocol_version <= 11 or self.server.oldest_protocol_version > 30:
            raise SessionError("unsupported protocol version %d" % self.server.protocol_version)

        encryption = self.server.encryption
        known = (packets.SECURITY_NO_SSL, packets.SECURITY_SSL_LOGIN_ONLY,
                 packets.SECURITY_SSL_LOGIN_THEN_RC4, packets.SECURITY_SSL_ALWAYS)
        if encryption not in known:
            raise SessionError("unknown encryption setting 0x%x" % encryption)

        self._read = Channel(read_socket)
        if encryption != packets.SECURITY_NO_SSL:
            self._read.start_ssl()

        # The switch opens with a handshake ack that we echo back.
        ack = packets.HandshakeAck.parse(self._read.recv_exact(packets.HANDSHAKE_ACK_SIZE))
        if ack.signature not in packets.SERVER_SIGNATURES:
            raise SessionError("unexpected handshake signature 0x%08x" % ack.signature)
        self._read.sendall(ack.with_signature(packets.READ_SOCKET_SIGNATURE).pack())

        # Second socket, paired with the first by the same handshake ack.
        write_socket = self._open_socket()
        self._negotiate_csc(write_socket)
        self._write = Channel(write_socket)
        if encryption != packets.SECURITY_NO_SSL:
            self._write.start_ssl()
        self._write.sendall(ack.with_signature(packets.WRITE_SOCKET_SIGNATURE).pack())

        reply = self._read.recv_exact(4)
        if reply[2] == packets.RSP_NACK:
            error = struct.unpack(">I", self._read.recv_exact(4))[0]
            if error == ERROR_TOO_MANY_SESSIONS:
                raise SwitchBusyError(TOO_MANY_SESSIONS_MESSAGE)
            raise SessionError("the switch refused the connection (error %d)" % error)
        if reply[2] != packets.RSP_ACK:
            raise SessionError("unexpected reply to the handshake: %s" % reply.hex())

        self._write.sendall(packets.identify_remote())
        rc4_key = self._login()

        time.sleep(ENCRYPTION_SWITCH_DELAY)
        if encryption == packets.SECURITY_SSL_LOGIN_THEN_RC4:
            self._read.stop_ssl(rc4_key)
            self._write.stop_ssl(rc4_key)
        elif encryption == packets.SECURITY_SSL_LOGIN_ONLY:
            self._read.stop_ssl(None)
            self._write.stop_ssl(None)

        # From now on a background thread owns the read socket.  It blocks
        # until the switch sends something; the keepalive covers idle time.
        self._read.sock.settimeout(None)
        self._last_activity = time.monotonic()
        self._reader = threading.Thread(target=self._read_loop, name="kx-reader", daemon=True)
        self._reader.start()
        self._keepalive = threading.Thread(target=self._keepalive_loop, name="kx-keepalive",
                                           daemon=True)
        self._keepalive.start()

    def _read_login_packet(self, expected: int) -> bytes:
        """Read the next login reply, answering NOPs while waiting."""
        while True:
            packet = read_packet(self._read)
            command = packet[2]
            if command == packets.RSP_NOP:
                self._write.sendall(packets.pack_command(packets.CMD_NOP_REPLY))
                continue
            if command == packets.RSP_PONG:
                continue
            if command == packets.RSP_NACK:
                error = struct.unpack(">I", packet[4:8])[0]
                raise auth.LoginError("the switch rejected the login step (error %d)" % error)
            if command != expected:
                raise auth.LoginError("expected reply %d during login, got %d"
                                      % (expected, command))
            return packet

    def _login(self) -> bytes:
        """Run the login exchange and return the RC4 session key."""
        ours = auth.ClientChallenge.generate()
        self._write.sendall(ours.pack())
        theirs = auth.ServerChallenge.parse(self._read_login_packet(packets.RSP_SERVER_CHALLENGE))

        self._write.sendall(auth.client_response(ours, theirs))
        server_response = auth.ServerResponse.parse(
            self._read_login_packet(packets.RSP_SERVER_RESPONSE))
        auth.check_server_response(server_response, ours, theirs)

        self._write.sendall(auth.access_request(self.user, self.password, self.server,
                                                server_response))
        auth.check_access_response(self._read_login_packet(packets.RSP_ACCESS_RESPONSE))
        LOG.info("logged in as %s", self.user)
        return auth.session_key(ours, theirs)

    # ------------------------------------------------------------------
    # Background threads
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Read everything the switch sends and route replies to callers."""
        try:
            while not self._closing.is_set():
                packet = read_packet(self._read)
                self._last_activity = time.monotonic()
                self._handle_packet(packet)
        except Exception as error:
            if not self._closing.is_set():
                LOG.error("connection to the switch lost: %s", error)
                self._reader_error = error
                self._fail_waiters(error)

    def _handle_packet(self, packet: bytes) -> None:
        command = packet[2]
        packet_id = packet[3]

        if command == packets.RSP_NOP:
            self.send(packets.pack_command(packets.CMD_NOP_REPLY))
            return

        if command == packets.RSP_NOTIFY and len(packet) >= 12:
            self._handle_notify(packet)
            return

        if command == packets.RSP_VIDEO_MARKER and len(packet) == 7:
            # Flow control: echo the marker back or the video stops.
            self.send(packets.pack_command(packets.CMD_VIDEO_SYNC, packet[4:7]))
            return

        if command in packets.VIDEO_PACKETS:
            if self.video_listener is not None:
                self.video_listener(packet)
            return

        # Replies to our requests carry the packet id we used.  Everything
        # else (video, keyboard LED state, event notifications) is ignored.
        if packet_id != 0:
            with self._waiting_lock:
                waiter = self._waiting.get(packet_id)
            if waiter is not None:
                waiter.put(packet)
                return
        LOG.debug("ignoring packet %d (%d bytes)", command, len(packet))

    def _handle_notify(self, packet: bytes) -> None:
        event, parameter = struct.unpack(">II", packet[4:12])
        LOG.debug("notify: event %d, parameter 0x%x", event, parameter)

        if event == packets.NOTIFY_AUTOSENSE and parameter == packets.AUTOSENSE_DONE:
            self._autosense_done.set()
        elif event == packets.NOTIFY_NO_VIDEO:
            self.no_video = parameter != 0

    def _fail_waiters(self, error: Exception) -> None:
        with self._waiting_lock:
            for waiter in self._waiting.values():
                waiter.put(error)

    def _keepalive_loop(self) -> None:
        while not self._closing.wait(1.0):
            if time.monotonic() - self._last_activity > KEEPALIVE_INTERVAL:
                try:
                    self.send(packets.pack_command(packets.CMD_PING))
                except OSError:
                    return

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send(self, packet: bytes) -> None:
        """Send one packet on the write socket."""
        if self._reader_error is not None:
            raise SessionError("connection lost: %s" % self._reader_error)
        self._write.sendall(packet)
        self._last_activity = time.monotonic()

    def _allocate_packet_id(self) -> int:
        with self._waiting_lock:
            packet_id = self._next_packet_id
            # Ids run 1..255; 0 means "no reply wanted".
            self._next_packet_id = packet_id % 255 + 1
            self._waiting[packet_id] = queue.Queue()
            return packet_id

    def request(self, command: int, payload: bytes, expected: int,
                timeout: float = REPLY_TIMEOUT) -> bytes:
        """Send a command and wait for its reply."""
        packet_id = self._allocate_packet_id()
        try:
            self.send(packets.pack_command(command, payload, packet_id))
            try:
                reply = self._waiting[packet_id].get(timeout=timeout)
            except queue.Empty:
                raise SessionError("no reply to command %d after %.0f s" % (command, timeout))
        finally:
            with self._waiting_lock:
                del self._waiting[packet_id]

        if isinstance(reply, Exception):
            raise SessionError("connection lost: %s" % reply)
        if reply[2] == packets.RSP_NACK:
            error = struct.unpack(">I", reply[4:8])[0]
            raise CommandRefused(command, error)
        if reply[2] != expected:
            raise SessionError("expected reply %d to command %d, got %d"
                               % (expected, command, reply[2]))
        return reply

    # ------------------------------------------------------------------
    # Ports
    # ------------------------------------------------------------------

    def query_database(self, query: str) -> ElementTree.Element:
        """Run one of the switch's XPath-like database queries."""
        body = query.encode("ascii") + b"\x00"
        payload = struct.pack(">IIIH", len(body), len(body), 0, 0) + body
        reply = self.request(packets.CMD_DATABASE_REQUEST, payload,
                             packets.RSP_DATABASE_RESPONSE)
        xml = reply[packets.DATABASE_RESPONSE_HEADER_SIZE:].split(b"\x00")[0]
        return ElementTree.fromstring(xml.decode("utf-8", "replace"))

    def list_ports(self) -> list[TargetPort]:
        """All KVM ports of the switch, in order."""
        system = self.query_database(PORTS_QUERY)

        # The switch's own ports include one "frame grabber" KVM port whose
        # Connection attribute names the internal switch that holds the
        # target ports.
        portal = None
        for port in system.iter("Port"):
            if port.get("Class") == "KVM" and port.get("Connection"):
                portal = port
                break
        if portal is None:
            raise SessionError("could not find the switch's KVM path")

        switch_id = portal.get("Connection")
        switch = self.query_database(DEVICE_QUERY % switch_id)

        ports = []
        for port in switch.iter("Port"):
            if port.get("Class") != "KVM":
                continue
            ports.append(TargetPort(
                number=int(port.get("index", "0")) + 1,
                target_id=port.get("id"),
                portal_id=portal.get("id"),
                name=(port.findtext("Name") or "").strip(),
                active=port.get("Status") == "1",
                x_mouse_scale=(port.findtext("XMouseScaleFactor") or "1.0").strip(),
                y_mouse_scale=(port.findtext("YMouseScaleFactor") or "1.0").strip(),
            ))
        return ports

    def rename_port(self, number: int, name: str) -> None:
        """Change the name the switch shows for a port (1-based).

        Needs a user allowed to change the switch's settings.  The switch
        accepts the change at once but applies it a few seconds later
        (within 5 s in testing), and only sessions opened after that see
        the new name.
        """
        check_port_name(name)
        ports = {port.number: port for port in self.list_ports()}
        if number not in ports:
            raise SessionError("port %d does not exist (the switch has %d ports)"
                               % (number, len(ports)))
        port = ports[number]

        update = PORT_UPDATE % (port.target_id, name, port.x_mouse_scale, port.y_mouse_scale)
        reply = self.query_database(update)

        # A good reply is <Database><Update>1</Update></Database>; a refusal
        # carries <Error>code</Error> instead.
        error = reply.find("Error")
        if error is None:
            error = reply.find("Update/Error")
        if error is not None:
            raise SessionError("the switch refused to rename port %d (error %s)"
                               % (number, (error.text or "?").strip()))

        changed = (reply.findtext("Update") or "").strip()
        if changed != "1":
            raise SessionError("the switch did not rename port %d (it changed %s entries)"
                               % (number, changed or "no"))
        LOG.info("renamed port %d from %r to %r", number, port.name, name)

    def connect_port(self, number: int, force: bool = False,
                     compression_params: bytes = DEFAULT_COMPRESSION_PARAMS) -> int:
        """Connect to a target port (1-based) and return its device id.

        force takes the port over if another user is connected to it.
        compression_params is the 40-byte video setup; see screen.py.
        While another session is connected to a different port this keeps
        trying for up to self.wait seconds.
        """
        ports = {port.number: port for port in self.list_ports()}
        if number not in ports:
            raise SessionError("port %d does not exist (the switch has %d ports)"
                               % (number, len(ports)))
        port = ports[number]
        if not port.active:
            LOG.warning("port %d (%s) has no powered target attached", number, port.name)

        connect = "<Connect ForceConnection=\"1\">" if force else "<Connect>"
        connect += "<Portal>//*[@id=%s]</Portal><Target>//*[@id=%s]</Target></Connect>" % (
            port.portal_id, port.target_id)
        payload = compression_params + connect.encode("ascii")
        self._autosense_done.clear()
        reply = self._request_video_connection(payload)

        self.device_id = reply[4]
        LOG.info("connected to port %d (%s), device id %d", number, port.name, self.device_id)

        if not self._autosense_done.wait(AUTOSENSE_TIMEOUT):
            LOG.warning("video autosense did not finish within %.0f s; "
                        "the first key presses may be lost", AUTOSENSE_TIMEOUT)
        if self.no_video:
            LOG.info("port %d has no video signal (keyboard and mouse still work)", number)
        return self.device_id

    def _request_video_connection(self, payload: bytes) -> bytes:
        """Send CONNECT_VIDEO_STREAM, trying again while the channel is busy."""
        deadline = time.monotonic() + self.wait
        attempt = 0
        while True:
            try:
                return self.request(packets.CMD_CONNECT_VIDEO_STREAM, payload,
                                    packets.RSP_VIDEO_CONNECTED)
            except CommandRefused as error:
                if error.error != ERROR_CHANNEL_BUSY:
                    raise
                if time.monotonic() >= deadline:
                    raise SwitchBusyError(CHANNEL_BUSY_MESSAGE) from error
            attempt += 1
            _log_retry(attempt, CHANNEL_BUSY_MESSAGE, self.wait)
            time.sleep(BUSY_RETRY_INTERVAL)

    def request_refresh(self) -> None:
        """Ask the switch to send the whole screen again."""
        device_id = self._require_port()
        # Flags 0 is a plain refresh; the last byte would request a video
        # autosense instead.
        payload = struct.pack(">IBB", 0, device_id, 0)
        self.send(packets.pack_command(packets.CMD_REFRESH, payload))

    def _require_port(self) -> int:
        if self.device_id is None:
            raise SessionError("connect to a port first")
        return self.device_id

    # ------------------------------------------------------------------
    # Keyboard and mouse
    # ------------------------------------------------------------------

    def send_key_bytes(self, key_bytes: bytes) -> None:
        """Send raw key bytes (scan codes or HID commands) to the port."""
        device_id = self._require_port()
        self.send(packets.pack_command(packets.CMD_KEYBOARD, bytes([device_id]) + key_bytes))
        self._last_input = time.monotonic()

    def send_mouse(self, function: int, x: int = 0, y: int = 0) -> None:
        """Send one mouse event.  For moves, x and y are screen pixels."""
        device_id = self._require_port()
        payload = struct.pack(">BBhh", device_id, function, x, y)
        self.send(packets.pack_command(packets.CMD_MOUSE, payload))
        self._last_input = time.monotonic()

    def wait_for_input_to_drain(self) -> None:
        """Give the switch time to deliver the last key or mouse event."""
        if self._last_input is None:
            return
        remaining = INPUT_DRAIN_TIME - (time.monotonic() - self._last_input)
        if remaining > 0:
            time.sleep(remaining)

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Disconnect from the port, log out and close both sockets."""
        if self._write is not None and self._reader_error is None:
            self.wait_for_input_to_drain()
            try:
                if self.device_id is not None:
                    self.request(packets.CMD_STOP_VIDEO_STREAM, bytes([self.device_id]),
                                 packets.RSP_ACK, timeout=5.0)
                self.send(packets.pack_command(packets.CMD_LOGOUT))
            except (OSError, SessionError) as error:
                LOG.debug("error while logging out: %s", error)

        self._closing.set()
        self._close_sockets()
        if self._reader is not None:
            self._reader.join(timeout=5.0)

    def _close_sockets(self) -> None:
        for channel in (self._read, self._write):
            if channel is not None:
                channel.close()
