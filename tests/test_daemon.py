# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import base64
import grp
import hashlib
import json
import os
import pathlib
import socket
import stat
import threading
import time

import pytest

from koidaemon import commands, keys
from koidaemon.backends.base import Snapshot
from koidaemon.client import Client
from koidaemon.daemon import MAX_REQUEST, Daemon
from koidaemon.errors import BackendError, Busy, KoiError, UnknownTarget, Unsupported

from fakes import FAKE_PNG, RecordingBackend, short_socket_path


def request(command, target="board", **args):
    return {"command": command, "target": target, "args": args}


@pytest.fixture
def backend():
    return RecordingBackend()


@pytest.fixture
def daemon(backend):
    return Daemon({"board": backend})


@pytest.fixture
def served(daemon):
    """The daemon listening on a socket, and a client for it."""
    path = short_socket_path()
    daemon.listen(path)
    thread = threading.Thread(target=daemon.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    yield daemon, Client(path), path
    daemon.shutdown()
    daemon.close()


@pytest.mark.parametrize("command, args, call", [
    ("keyboard.type", {"text": "root\n"}, ("type_text", "root\n")),
    ("keyboard.press", {"keys": ["f2", "ctrl+c"]},
     ("press", [[0x3B], [keys.LEFT_CTRL, 0x06]])),
    ("keyboard.down", {"key": "shift"}, ("key_down", keys.LEFT_SHIFT)),
    ("keyboard.up", {"key": "shift"}, ("key_up", keys.LEFT_SHIFT)),
    ("mouse.move", {"x": 10, "y": 20}, ("mouse_move", 10, 20)),
    ("mouse.click", {"count": 2}, ("mouse_click", "left", 2, None, None)),
    ("mouse.click", {"button": "right", "x": 1, "y": 2}, ("mouse_click", "right", 1, 1, 2)),
    ("mouse.down", {}, ("mouse_down", "left")),
    ("mouse.up", {"button": "middle"}, ("mouse_up", "middle")),
    ("mouse.scroll", {"notches": -3}, ("mouse_scroll", -3)),
    ("storage.eject", {}, ("storage_eject",)),
])
def test_commands_reach_the_backend(daemon, backend, command, args, call):
    assert daemon.handle(request(command, **args)) == {"ok": True, "result": {}}
    assert backend.calls == [call]


def test_status_adds_capabilities(daemon):
    reply = daemon.handle(request("status"))
    assert reply["ok"]
    assert reply["result"]["online"] is True
    assert reply["result"]["capabilities"] == sorted(commands.ALL_CAPABILITIES)


def test_snapshot_is_base64(daemon):
    result = daemon.handle(request("snapshot"))["result"]
    assert result["format"] == "png"
    assert (result["width"], result["height"]) == (64, 48)
    assert base64.b64decode(result["data"]) == FAKE_PNG


def test_snapshot_converted_to_jpeg(daemon):
    result = daemon.handle(request("snapshot", format="jpeg"))["result"]
    assert result["format"] == "jpeg"
    assert (result["width"], result["height"]) == (64, 48)
    assert base64.b64decode(result["data"]).startswith(b"\xff\xd8")


def test_snapshot_in_the_kvms_own_format(daemon, backend, monkeypatch):
    monkeypatch.setattr(backend, "snapshot", lambda: Snapshot("jpeg", b"as sent", 1, 1))
    result = daemon.handle(request("snapshot", format="native"))["result"]
    assert result["format"] == "jpeg"
    assert base64.b64decode(result["data"]) == b"as sent"


def test_snapshot_in_an_unknown_format(daemon):
    reply = daemon.handle(request("snapshot", format="gif"))
    assert reply["error"]["code"] == "invalid"


def test_storage_insert_and_status(daemon, backend):
    result = daemon.handle(request("storage.insert", image="/images/a.iso", mode="cdrom"))
    assert result["result"] == {"inserted": True, "image": "a.iso", "mode": "cdrom",
                                "writable": False}
    assert backend.calls == [("storage_insert", pathlib.Path("/images/a.iso"), "cdrom", False)]

    status = daemon.handle(request("storage.status"))["result"]
    assert status["inserted"] is False


def test_targets(daemon):
    reply = daemon.handle({"command": "targets"})
    assert reply["result"] == {"targets": {"board": {
        "backend": "fake", "capabilities": sorted(commands.ALL_CAPABILITIES),
    }}}


@pytest.mark.parametrize("bad_request, code, message", [
    ("not an object", "invalid", "object with a command"),
    ({"target": "board"}, "invalid", "object with a command"),
    ({"command": "status"}, "invalid", "status needs a target"),
    ({"command": "status", "target": "nobody"}, "unknown-target", "no target called 'nobody'"),
    ({"command": "status", "target": "board", "args": [1]}, "invalid", "args must be an object"),
    ({"command": "mouse.move", "target": "board", "args": {"x": 1}}, "invalid", "needs y"),
    ({"command": "dance", "target": "board"}, "invalid", "unknown command"),
])
def test_bad_requests(daemon, backend, bad_request, code, message):
    reply = daemon.handle(bad_request)
    assert reply["ok"] is False
    assert reply["error"]["code"] == code
    assert message in reply["error"]["message"]
    assert backend.calls == []


def test_missing_capability_is_refused_before_the_backend():
    backend = RecordingBackend(capabilities={commands.KEYBOARD, commands.STORAGE_STICK})
    daemon = Daemon({"board": backend})

    reply = daemon.handle(request("mouse.move", x=1, y=1))
    assert reply["error"]["code"] == "unsupported"
    assert reply["error"]["message"] == "board (fake) cannot do mouse.move"

    reply = daemon.handle(request("storage.insert", image="/a.iso", mode="cdrom"))
    assert reply["error"]["code"] == "unsupported"
    assert backend.calls == []


@pytest.mark.parametrize("error, code", [
    (Busy("try later"), "busy"),
    (BackendError("it broke"), "failed"),
    (Unsupported("no"), "unsupported"),
])
def test_backend_errors_are_replies(daemon, backend, error, code):
    backend.error = error
    reply = daemon.handle(request("keyboard.type", text="x"))
    assert reply == {"ok": False, "error": {"code": code, "message": str(error)}}


def test_unexpected_exceptions_do_not_escape(daemon, backend):
    backend.error = ZeroDivisionError("oops")
    reply = daemon.handle(request("keyboard.type", text="x"))
    assert reply["error"] == {"code": "failed", "message": "internal error: oops"}


class SlowBackend(RecordingBackend):
    """Records how many of its calls overlap."""

    running = 0
    most_at_once = 0
    lock = threading.Lock()

    def type_text(self, text):
        with SlowBackend.lock:
            SlowBackend.running += 1
            SlowBackend.most_at_once = max(SlowBackend.most_at_once, SlowBackend.running)
        time.sleep(0.05)
        with SlowBackend.lock:
            SlowBackend.running -= 1


def run_at_once(daemon, targets):
    threads = []
    for target in targets:
        thread = threading.Thread(target=daemon.handle,
                                  args=(request("keyboard.type", target, text="x"),))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()


def test_targets_on_one_kvm_never_overlap():
    SlowBackend.most_at_once = 0
    # Two ports of the same switch: one resource.
    daemon = Daemon({
        "port-1": SlowBackend("port-1", resource="switch"),
        "port-2": SlowBackend("port-2", resource="switch"),
    })
    run_at_once(daemon, ["port-1", "port-2", "port-1", "port-2"])
    assert SlowBackend.most_at_once == 1


def test_separate_kvms_run_in_parallel():
    SlowBackend.most_at_once = 0
    daemon = Daemon({
        "a": SlowBackend("a", resource="kvm-a"),
        "b": SlowBackend("b", resource="kvm-b"),
    })
    run_at_once(daemon, ["a", "b"])
    assert SlowBackend.most_at_once == 2


# ----------------------------------------------------------------------
# Over the socket
# ----------------------------------------------------------------------

def test_client_over_the_socket(served, backend):
    _daemon, client, _path = served
    assert client.call("keyboard.type", "board", text="hi") == {}
    assert backend.calls == [("type_text", "hi")]
    assert client.targets()["board"]["backend"] == "fake"
    assert client.snapshot("board").data == FAKE_PNG


def test_client_raises_matching_errors(served, backend):
    _daemon, client, _path = served
    with pytest.raises(UnknownTarget, match="nobody"):
        client.call("status", "nobody")
    backend.error = Busy("switch in use")
    with pytest.raises(Busy, match="switch in use"):
        client.call("keyboard.type", "board", text="x")


def test_several_requests_on_one_connection(served):
    _daemon, _client, path = served
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(path))
        replies = connection.makefile("rb")
        for _ in range(3):
            connection.sendall(json.dumps(request("status")).encode() + b"\n")
            assert json.loads(replies.readline())["ok"] is True
        connection.sendall(b"not json\n")
        assert json.loads(replies.readline())["error"]["message"] == "not JSON"


def test_oversized_request_is_refused(served):
    _daemon, _client, path = served
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(path))
        connection.sendall(b"x" * (MAX_REQUEST + 10) + b"\n")
        reply = json.loads(connection.makefile("rb").readline())
    assert reply["error"]["message"] == "request too large"


def test_socket_permissions(served):
    _daemon, _client, path = served
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o660


def test_socket_group(daemon):
    path = short_socket_path()
    group = grp.getgrgid(os.getgid()).gr_name
    daemon.listen(path, mode=0o600, group=group)
    try:
        assert os.stat(path).st_gid == os.getgid()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        daemon.close()


def test_a_stale_socket_is_replaced(daemon):
    path = short_socket_path()
    # A socket file nobody listens on, as left by a koid that crashed.
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()

    daemon.listen(path)
    daemon.close()
    assert not path.exists()


def test_a_live_socket_is_not_taken_over(served, backend):
    _daemon, _client, path = served
    second = Daemon({"board": backend})
    with pytest.raises(KoiError, match="already running"):
        second.listen(path)


def test_a_daemon_that_hangs_up(tmp_path):
    path = short_socket_path()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)

    def hang_up():
        connection, _address = listener.accept()
        connection.close()

    thread = threading.Thread(target=hang_up)
    thread.start()
    try:
        with pytest.raises(KoiError, match="without replying"):
            Client(path).call("targets")
    finally:
        thread.join()
        listener.close()


def test_no_daemon(tmp_path):
    with pytest.raises(KoiError, match="cannot connect to koid"):
        Client(tmp_path / "nothing.sock").call("targets")


def test_socket_path_too_long(tmp_path):
    long_path = tmp_path / ("x" * 120) / "koid.sock"
    with pytest.raises(KoiError, match="cannot connect to koid at .*: AF_UNIX path too long"):
        Client(long_path).call("targets")


# ----------------------------------------------------------------------
# Pushed images
# ----------------------------------------------------------------------

def upload_request(contents: bytes, name: str = "disk.img", **args) -> dict:
    upload = {"name": name, "size": len(contents),
              "sha256": hashlib.sha256(contents).hexdigest()}
    return request("storage.insert", upload=upload, **args)


def test_a_pushed_image_without_a_way_to_receive_it(daemon, backend):
    # As over HTTP's /v1/command: the client is told to send it separately.
    reply = daemon.handle(upload_request(b"x" * 600))
    assert reply == {"ok": True, "send": 600}
    assert backend.calls == []


def test_client_pushes_an_image_over_the_socket(served, backend, tmp_path):
    _daemon, client, _path = served
    contents = bytes(range(256)) * 50
    image = tmp_path / "disk.img"
    image.write_bytes(contents)
    backend.read_size = 1000

    status = client.storage_insert("board", image, mode="stick", writable=True, push=True)

    assert status["image"] == "disk.img"
    assert backend.calls == [("storage_insert", ("pushed", "disk.img", contents), "stick", True)]


def test_an_image_the_kvm_has_is_not_sent(served, backend, tmp_path):
    _daemon, client, _path = served
    image = tmp_path / "disk.img"
    image.write_bytes(b"x" * 600)
    backend.has_image = True
    client.storage_insert("board", image, push=True)
    assert backend.calls == [("storage_insert", ("pushed", "disk.img", None), "stick", False)]


def test_client_sends_a_path_by_default(served, backend, tmp_path):
    _daemon, client, _path = served
    image = tmp_path / "disk.img"
    image.write_bytes(b"x" * 600)
    client.storage_insert("board", image)
    assert backend.calls == [("storage_insert", image, "stick", False)]


def push_raw(path: pathlib.Path, announced: bytes, sent: bytes, shut: bool = False):
    """Push sent while announcing announced; return the replies, and a
    status request's reply on the same connection afterwards (or None)."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(path))
        replies = connection.makefile("rb")
        connection.sendall(json.dumps(upload_request(announced)).encode() + b"\n")
        first = json.loads(replies.readline())
        connection.sendall(sent)
        if shut:
            connection.shutdown(socket.SHUT_WR)
        reply = json.loads(replies.readline())
        after = None
        if not shut:
            connection.sendall(json.dumps(request("status")).encode() + b"\n")
            line = replies.readline()
            after = json.loads(line) if line else None
    return first, reply, after


def test_an_image_that_does_not_match_its_digest(served, backend):
    _daemon, _client, path = served
    first, reply, after = push_raw(path, b"a" * 600, b"b" * 600)
    assert first == {"ok": True, "send": 600}
    assert reply["error"] == {"code": "failed", "message": "the image does not match its SHA-256"}
    # Every byte was read, so the connection carries on.
    assert after["ok"] is True


def test_an_image_that_ends_short(served, backend):
    _daemon, _client, path = served
    _first, reply, _after = push_raw(path, b"a" * 600, b"a" * 100, shut=True)
    assert reply["error"]["message"] == "the image ended 500 bytes short"


class GivesUpBackend(RecordingBackend):
    """Reads part of a pushed image, then fails, as a KVM might."""

    def storage_insert(self, image, mode, writable):
        image.open().read(100)
        raise BackendError("the KVM is full")


def test_the_rest_of_an_image_is_read_when_the_backend_fails():
    daemon = Daemon({"board": GivesUpBackend()})
    path = short_socket_path()
    daemon.listen(path)
    threading.Thread(target=daemon.serve_forever, args=(0.02,), daemon=True).start()
    try:
        contents = b"c" * 5000
        _first, reply, after = push_raw(path, contents, contents)
    finally:
        daemon.shutdown()
        daemon.close()
    assert reply["error"]["message"] == "the KVM is full"
    assert after["ok"] is True
