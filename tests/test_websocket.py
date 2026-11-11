# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import io
import socket
import threading
import time

import pytest

from koidaemon import websocket
from koidaemon.websocket import WebSocket, WebSocketError

from fake_kvmd import FakeKVMD

HEADERS = {"X-KVMD-User": "admin", "X-KVMD-Passwd": "secret"}


def test_accept_key():
    # The example in RFC 6455, section 1.3.
    assert websocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("length", [0, 5, 125, 126, 65535, 65536])
@pytest.mark.parametrize("mask", [None, b"\x01\x02\x03\x04"])
def test_frames_round_trip(length, mask):
    payload = bytes(index % 251 for index in range(length))
    frame = websocket.encode_frame(websocket.OPCODE_PING, payload, mask)
    assert websocket.read_frame(io.BytesIO(frame)) == (websocket.OPCODE_PING, payload)


def test_masked_frames_hide_the_payload():
    frame = websocket.encode_frame(0x1, b"hello", mask=b"\xff\xff\xff\xff")
    assert b"hello" not in frame


def test_a_short_frame():
    frame = websocket.encode_frame(0x1, b"hello")
    with pytest.raises(WebSocketError, match="closed"):
        websocket.read_frame(io.BytesIO(frame[:-1]))


@pytest.fixture
def kvmd():
    with FakeKVMD() as fake:
        yield fake


def ws_url(kvmd, query="stream=1"):
    return kvmd.url.replace("http", "ws", 1) + "/api/ws?" + query


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_connection_is_held_open_until_closed(kvmd):
    with WebSocket(ws_url(kvmd), HEADERS):
        assert kvmd.watchers == 1
        assert kvmd.streaming
    wait_for(lambda: kvmd.watchers == 0)


def test_pings_are_answered(kvmd):
    with WebSocket(ws_url(kvmd), HEADERS):
        wait_for(lambda: kvmd.pongs)
    assert kvmd.pongs == [b"still there?"]


def test_the_headers_are_sent(kvmd):
    with WebSocket(ws_url(kvmd), HEADERS):
        pass
    (request,) = kvmd.calls("ws")
    assert request.headers["X-KVMD-User"] == "admin"
    assert request.query == {"stream": "1"}


def test_refused(kvmd):
    with pytest.raises(WebSocketError, match="HTTP 401") as raised:
        WebSocket(ws_url(kvmd), {"X-KVMD-User": "admin"}).open()
    assert raised.value.status == 401


def test_nothing_listening():
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    with pytest.raises(WebSocketError, match="cannot connect"):
        WebSocket("ws://127.0.0.1:%d/" % port).open()


def serve_once(answer: bytes):
    """A server that answers one connection with answer, then hangs up."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(answer)
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return "ws://127.0.0.1:%d/" % listener.getsockname()[1], thread


def test_a_wrong_accept_key():
    url, thread = serve_once(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Sec-WebSocket-Accept: wrong\r\n\r\n")
    with pytest.raises(WebSocketError, match="did not accept"):
        WebSocket(url).open()
    thread.join()


def test_not_http():
    url, thread = serve_once(b"SSH-2.0-OpenSSH\r\n")
    with pytest.raises(WebSocketError, match="did not answer with HTTP"):
        WebSocket(url).open()
    thread.join()


def test_closing_twice(kvmd):
    connection = WebSocket(ws_url(kvmd), HEADERS)
    connection.open()
    connection.close()
    connection.close()
