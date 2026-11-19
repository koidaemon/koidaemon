# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Just enough of a WebSocket client (RFC 6455).

kvmd only runs its video streamer while a client is connected to its
WebSocket, so the pikvm backend holds one open while it needs video; it
sends nothing, and the client answers pings and throws away everything
else the server sends.  The linkr backend uploads disk images as binary
messages, and asks the client to keep what the server sends for it to
read.
"""

from __future__ import annotations

import base64
import hashlib
import os
import queue
import socket
import ssl
import struct
import threading
import urllib.parse

# From RFC 6455, section 1.3.
ACCEPT_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

MAX_HEADER_SIZE = 64 * 1024
# How long close() waits for the server to answer its close frame.
CLOSE_WAIT = 2.0


class WebSocketError(Exception):
    """The connection could not be made, or broke."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        # The HTTP status, when the server refused the upgrade.
        self.status = status


def accept_key(key: str) -> str:
    """The Sec-WebSocket-Accept a server must answer key with."""
    digest = hashlib.sha1(key.encode() + ACCEPT_GUID).digest()
    return base64.b64encode(digest).decode()


def apply_mask(payload: bytes, mask: bytes) -> bytes:
    """XOR payload with the 4-byte mask repeated, as one big integer, which
    is far quicker than byte by byte for megabytes of disk image."""
    if not payload:
        return payload
    length = len(payload)
    repeated = (mask * (length // 4 + 1))[:length]
    value = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return value.to_bytes(length, "big")


def encode_frame(opcode: int, payload: bytes = b"", mask: bytes | None = None) -> bytes:
    """One final frame.  Clients must mask what they send; servers must not."""
    first = 0x80 | opcode
    mask_bit = 0x80 if mask is not None else 0
    length = len(payload)
    if length < 126:
        header = struct.pack(">BB", first, mask_bit | length)
    elif length < 1 << 16:
        header = struct.pack(">BBH", first, mask_bit | 126, length)
    else:
        header = struct.pack(">BBQ", first, mask_bit | 127, length)
    if mask is None:
        return header + payload
    return header + mask + apply_mask(payload, mask)


def read_frame(stream) -> tuple[int, bytes]:
    """Read one frame from a file-like stream: (opcode, payload)."""
    _final, opcode, payload = read_frame_parts(stream)
    return opcode, payload


def read_frame_parts(stream) -> tuple[bool, int, bytes]:
    """Read one frame: (whether it ends its message, opcode, payload)."""
    first, second = _read_exactly(stream, 2)
    final = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", _read_exactly(stream, 2))
    elif length == 127:
        (length,) = struct.unpack(">Q", _read_exactly(stream, 8))
    mask = _read_exactly(stream, 4) if second & 0x80 else None
    payload = _read_exactly(stream, length)
    if mask is not None:
        payload = apply_mask(payload, mask)
    return final, opcode, payload


def _read_exactly(stream, size: int) -> bytes:
    data = stream.read(size)
    if data is None or len(data) < size:
        raise WebSocketError("the connection closed")
    return data


class WebSocket:
    """A WebSocket connection, kept alive by a background thread.

    Use it as a context manager: the connection is open inside the block
    and closed when it ends.  With keep_messages, text and binary messages
    from the server are kept for receive(); otherwise they are dropped.
    """

    def __init__(self, url: str, headers: dict | None = None,
                 tls: ssl.SSLContext | None = None, timeout: float = 30.0,
                 keep_messages: bool = False) -> None:
        self.url = url
        self.headers = headers or {}
        self.tls = tls
        self.timeout = timeout
        self.keep_messages = keep_messages
        # Whole messages from the server, then None once it has gone.
        self._messages: queue.Queue = queue.Queue()
        self._socket = None
        self._stream = None
        self._thread = None
        self._send_lock = threading.Lock()
        # Set when the server's close frame arrives, or the connection ends.
        self._server_closed = threading.Event()

    def __enter__(self) -> "WebSocket":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        secure = parts.scheme in ("wss", "https")
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        try:
            connection = socket.create_connection((parts.hostname, port), timeout=self.timeout)
            if secure:
                tls = self.tls or ssl.create_default_context()
                connection = tls.wrap_socket(connection, server_hostname=parts.hostname)
        except OSError as error:
            raise WebSocketError("cannot connect: %s" % error) from error
        self._socket = connection
        self._stream = connection.makefile("rb")

        try:
            self._handshake(parts.netloc, path)
        except (OSError, WebSocketError):
            self._close_socket()
            raise
        # Reads now wait for as long as the server stays quiet.
        connection.settimeout(None)
        self._thread = threading.Thread(target=self._read_frames, daemon=True)
        self._thread.start()

    def _handshake(self, host: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % host,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in self.headers.items():
            lines.append("%s: %s" % (name, value))
        request = "\r\n".join(lines) + "\r\n\r\n"
        self._socket.sendall(request.encode())

        status, headers = self._read_response_head()
        if status != 101:
            raise WebSocketError("the server answered HTTP %d" % status, status)
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("the server did not accept the WebSocket key")

    def _read_response_head(self) -> tuple[int, dict]:
        status_line = self._stream.readline(MAX_HEADER_SIZE).decode("latin-1")
        fields = status_line.split()
        if len(fields) < 2 or not fields[1].isdigit():
            raise WebSocketError("the server did not answer with HTTP")

        headers = {}
        total = len(status_line)
        while True:
            line = self._stream.readline(MAX_HEADER_SIZE).decode("latin-1")
            total += len(line)
            if total > MAX_HEADER_SIZE:
                raise WebSocketError("the server's answer is too long")
            if line in ("\r\n", "\n", ""):
                break
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return int(fields[1]), headers

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        frame = encode_frame(opcode, payload, mask=os.urandom(4))
        with self._send_lock:
            self._socket.sendall(frame)

    def send_binary(self, data: bytes) -> None:
        """Send one binary message."""
        try:
            self._send(OPCODE_BINARY, data)
        except OSError as error:
            raise WebSocketError("cannot send: %s" % error) from error

    def receive(self, timeout: float | None = None) -> bytes:
        """The next text or binary message the server sent (text as UTF-8
        bytes), waiting up to timeout seconds.  Needs keep_messages."""
        try:
            message = self._messages.get(timeout=self.timeout if timeout is None else timeout)
        except queue.Empty:
            raise WebSocketError("the server sent nothing for %g seconds"
                                 % (self.timeout if timeout is None else timeout)) from None
        if message is None:
            # Leave the marker for anyone else waiting.
            self._messages.put(None)
            raise WebSocketError("the connection closed")
        return message

    def _read_frames(self) -> None:
        parts: list[bytes] = []
        try:
            while True:
                final, opcode, payload = read_frame_parts(self._stream)
                if opcode == OPCODE_PING:
                    self._send(OPCODE_PONG, payload)
                elif opcode == OPCODE_CLOSE:
                    return
                elif opcode in (OPCODE_TEXT, OPCODE_BINARY, OPCODE_CONTINUATION):
                    if not self.keep_messages:
                        continue
                    parts.append(payload)
                    if final:
                        self._messages.put(b"".join(parts))
                        parts = []
        except (OSError, ValueError, WebSocketError):
            # The connection closed, from either end.
            return
        finally:
            self._messages.put(None)
            self._server_closed.set()

    def close(self) -> None:
        """Close the connection with the close handshake: send a close frame
        and wait for the server's, so that the server has seen the client
        leave before this returns."""
        if self._socket is None:
            return
        try:
            self._send(OPCODE_CLOSE, struct.pack(">H", 1000))
        except OSError:
            pass
        else:
            if self._thread is not None:
                self._server_closed.wait(CLOSE_WAIT)
        self._close_socket()

    def _close_socket(self) -> None:
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        # The shutdown ends the reader thread's read; let it finish with
        # the stream before closing it.
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._stream.close()
        self._socket.close()
        self._socket = None
