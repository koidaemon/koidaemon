# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""The daemon: one Unix socket, many clients, one queue per KVM device.

The protocol is a JSON object per line in each direction.  A request:

    {"command": "keyboard.type", "target": "board-a", "args": {"text": "root\\n"}}

and its reply, either

    {"ok": true, "result": {...}}
    {"ok": false, "error": {"code": "unsupported", "message": "..."}}

A client may send several requests on one connection, one after another.
docs/protocol.md lists the commands.

storage.insert with "upload" instead of "image" has the client send the
image's contents: if the KVM needs them, koid answers {"ok": true,
"send": SIZE} first, the client sends exactly SIZE bytes, and koid then
sends the reply.
"""

from __future__ import annotations

import base64
import grp
import json
import logging
import os
import pathlib
import socket
import socketserver
import threading

from koidaemon import commands, images
from koidaemon.backends.base import Backend
from koidaemon.errors import InvalidRequest, KoiError, UnknownTarget, Unsupported
from koidaemon.uploads import LocalImage, PushedImage, SendImage

LOG = logging.getLogger(__name__)

# Requests are small (a pushed image's contents come after the request,
# not in it), so refuse anything larger than this.
MAX_REQUEST = 64 * 1024

# How long a client sending an image may go quiet before koid gives up on
# it.  The KVM stays locked while an image arrives.
UPLOAD_IDLE_TIMEOUT = 60.0


def _ask_for_another_request(size: int):
    raise SendImage(size)


class Daemon:
    """Runs commands on targets.  handle() works without a socket."""

    def __init__(self, targets: dict[str, Backend]) -> None:
        self.targets = targets
        # One lock per KVM device, so its commands never overlap.
        self._locks = {backend.resource: threading.Lock() for backend in targets.values()}
        self._server: socketserver.UnixStreamServer | None = None
        self._serving = threading.Event()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def handle(self, request, receive=None) -> dict:
        """Run one request and return the reply.

        receive(size) is how a pushed image's contents are asked for: it
        returns the stream they arrive on.  Without it, a request that
        needs them is answered {"ok": true, "send": SIZE}, for the client
        to send them in a request of their own.
        """
        try:
            result = self._run(request, receive or _ask_for_another_request)
        except SendImage as send:
            return {"ok": True, "send": send.size}
        except KoiError as error:
            return {"ok": False, "error": {"code": error.code, "message": str(error)}}
        except Exception as error:
            LOG.exception("request %r failed", request)
            message = "internal error: %s" % error
            return {"ok": False, "error": {"code": "failed", "message": message}}
        return {"ok": True, "result": result}

    def _run(self, request, receive) -> dict:
        if not isinstance(request, dict) or not isinstance(request.get("command"), str):
            raise InvalidRequest("a request is an object with a command")
        name = request["command"]
        if name == "targets":
            return self._list_targets()

        target = request.get("target")
        if not isinstance(target, str):
            raise InvalidRequest("%s needs a target" % name)
        if target not in self.targets:
            raise UnknownTarget("no target called %r" % target)
        backend = self.targets[target]

        if not isinstance(request.get("args", {}), dict):
            raise InvalidRequest("args must be an object")
        args = commands.validate(name, request.get("args"))
        needed = commands.required_capability(name, args)
        if not commands.has_capability(backend.capabilities, needed):
            raise Unsupported("%s (%s) cannot do %s" % (target, backend.name, name))

        LOG.info("%s: %s", target, name)
        with self._locks[backend.resource]:
            return self._dispatch(backend, name, args, receive)

    def _list_targets(self) -> dict:
        targets = {}
        for name, backend in sorted(self.targets.items()):
            targets[name] = backend.describe()
        return {"targets": targets}

    def _dispatch(self, backend: Backend, name: str, args: dict, receive=None) -> dict:
        """Call the backend method for a validated command."""
        if name == "status":
            status = backend.status()
            status["capabilities"] = sorted(backend.capabilities)
            return status
        if name == "snapshot":
            snapshot = images.convert(backend.snapshot(), args["format"])
            return {
                "format": snapshot.format,
                "width": snapshot.width,
                "height": snapshot.height,
                "data": base64.b64encode(snapshot.data).decode("ascii"),
            }

        if name == "keyboard.type":
            backend.type_text(args["text"])
        elif name == "keyboard.press":
            backend.press(args["combinations"])
        elif name == "keyboard.down":
            backend.key_down(args["usage"])
        elif name == "keyboard.up":
            backend.key_up(args["usage"])
        elif name == "mouse.move":
            backend.mouse_move(args["x"], args["y"])
        elif name == "mouse.click":
            backend.mouse_click(args["button"], args["count"], args["x"], args["y"])
        elif name == "mouse.down":
            backend.mouse_down(args["button"])
        elif name == "mouse.up":
            backend.mouse_up(args["button"])
        elif name == "mouse.scroll":
            backend.mouse_scroll(args["notches"])
        elif name == "storage.insert":
            return self._insert(backend, args, receive or _ask_for_another_request)
        elif name == "storage.eject":
            backend.storage_eject()
        elif name == "storage.status":
            return backend.storage_status()
        else:
            # commands.validate() only lets known commands through.
            raise InvalidRequest("unknown command %r" % name)
        return {}

    def _insert(self, backend: Backend, args: dict, receive) -> dict:
        if args["upload"] is None:
            image = LocalImage(pathlib.Path(args["image"]))
            return backend.storage_insert(image, args["mode"], args["writable"])

        upload = args["upload"]
        image = PushedImage(upload["name"], upload["size"], upload["sha256"], receive)
        try:
            return backend.storage_insert(image, args["mode"], args["writable"])
        finally:
            # If the backend stopped reading part way, take the rest of what
            # the client sends so that the reply is not lost in it.
            if image.reader is not None:
                image.reader.drain()
                if not image.reader.finished:
                    _connection_lost(receive)

    # ------------------------------------------------------------------
    # Socket
    # ------------------------------------------------------------------

    def listen(self, path: pathlib.Path, mode: int = 0o660, group: str | None = None) -> None:
        """Create the socket.  Fails if another koid is already using it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        _remove_stale_socket(path)

        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                daemon._serve_connection(self.rfile, self.wfile, self.connection)

        self._server = _Server(str(path), Handler)
        os.chmod(path, mode)
        if group is not None:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        LOG.info("listening on %s", path)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Serve until shutdown(), which takes up to poll_interval to notice."""
        self._serving.set()
        self._server.serve_forever(poll_interval)

    def shutdown(self) -> None:
        """Stop serving; safe to call from another thread."""
        # shutdown() waits for serve_forever() to return, so only call it
        # once serving has started.
        if self._serving.is_set():
            self._server.shutdown()

    def close(self) -> None:
        if self._server is not None:
            path = self._server.server_address
            self._server.server_close()
            try:
                os.unlink(path)
            except OSError:
                pass
        for backend in self.targets.values():
            backend.close()

    def _serve_connection(self, rfile, wfile, connection=None) -> None:
        while True:
            line = rfile.readline(MAX_REQUEST + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST:
                reply = {"ok": False, "error": {"code": "invalid", "message": "request too large"}}
                wfile.write(json.dumps(reply).encode() + b"\n")
                return
            channel = _SocketChannel(rfile, wfile, connection)
            try:
                request = json.loads(line)
            except ValueError:
                reply = {"ok": False, "error": {"code": "invalid", "message": "not JSON"}}
            else:
                reply = self.handle(request, channel)
            if connection is not None:
                connection.settimeout(None)
            wfile.write(json.dumps(reply).encode() + b"\n")
            wfile.flush()
            if channel.lost:
                # Part of an image is still unread; nothing more on this
                # connection can be understood.
                return


class _SocketChannel:
    """Asks a socket client for a pushed image, on the same connection."""

    def __init__(self, rfile, wfile, connection) -> None:
        self._rfile = rfile
        self._wfile = wfile
        self._connection = connection
        self.lost = False

    def __call__(self, size: int):
        self._wfile.write(json.dumps({"ok": True, "send": size}).encode() + b"\n")
        self._wfile.flush()
        if self._connection is not None:
            self._connection.settimeout(UPLOAD_IDLE_TIMEOUT)
        return self._rfile


def _connection_lost(receive) -> None:
    if isinstance(receive, _SocketChannel):
        receive.lost = True


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _remove_stale_socket(path: pathlib.Path) -> None:
    """Remove a socket left behind by a koid that died, but not a live one."""
    if not path.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        os.unlink(path)
        return
    finally:
        probe.close()
    raise KoiError("%s is in use: is koid already running?" % path)
