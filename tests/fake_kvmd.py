# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A stand-in for PiKVM's kvmd HTTP API, enough to test the pikvm backend.

It follows kvmd where the backend depends on its behaviour: the JSON
envelope, the login headers, refusing to change the drive while it is
connected, and quietly dropping the CD flag for images too big to be CDs.
"""

from __future__ import annotations

import http.server
import json
import threading
import urllib.parse
from dataclasses import dataclass


def make_jpeg(width: int, height: int) -> bytes:
    """A JPEG with just the headers that carry its size."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
            + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: object         # case-insensitive, as HTTP headers are
    body: bytes


class FakeKVMD:
    def __init__(self, user: str = "admin", password: str = "secret") -> None:
        self.user = user
        self.password = password
        self.requests: list[Request] = []

        # Video: None means the streamer is not running.
        self.resolution: tuple[int, int] | None = (1920, 1080)
        self.snapshot_size: tuple[int, int] | None = (1920, 1080)
        # Served instead of a JPEG of snapshot_size, when set.
        self.snapshot_data: bytes | None = None

        # Mass storage.
        self.msd_enabled = True
        self.images: dict[str, dict] = {}
        self.uploads: dict[str, bytes] = {}
        self.drive = {"image": None, "connected": False, "cdrom": False, "rw": False}
        self.cdrom_limit = 1000

        # Misbehaviour, for testing how the backend copes.
        self.answer_everything_with: tuple[int, bytes] | None = None
        self.ignore_connect = False
        self.refuse_writable = False

        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll interval makes shutting down quick.
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.02,),
                                        daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self) -> "FakeKVMD":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    def calls(self, path: str) -> list[Request]:
        """The requests made to one API path, e.g. "hid/print"."""
        return [request for request in self.requests if request.path == "/api/" + path]

    # ------------------------------------------------------------------

    def _handle(self, handler, method: str) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        request = Request(method, parsed.path, query, handler.headers, body)
        self.requests.append(request)

        if self.answer_everything_with is not None:
            status, data = self.answer_everything_with
            handler.send_response(status)
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return

        if (handler.headers.get("X-KVMD-User") != self.user
                or handler.headers.get("X-KVMD-Passwd") != self.password):
            self._json(handler, 401, False, {"error": "ForbiddenError", "error_msg": "Forbidden"})
            return

        route = getattr(self, "_route_" + parsed.path[len("/api/"):].replace("/", "_"), None)
        if route is None:
            self._json(handler, 404, False, {"error": "NotFound", "error_msg": "no route"})
            return
        route(handler, request)

    def _json(self, handler, status: int, ok: bool, result: dict) -> None:
        data = json.dumps({"ok": ok, "result": result}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _ok(self, handler, result: dict | None = None) -> None:
        self._json(handler, 200, True, result or {})

    def _fail(self, handler, error: str, message: str) -> None:
        self._json(handler, 400, False, {"error": error, "error_msg": message})

    # Video.

    def _route_streamer(self, handler, request) -> None:
        if self.resolution is None:
            self._ok(handler, {"streamer": None})
            return
        width, height = self.resolution
        source = {"online": True, "resolution": {"width": width, "height": height}}
        self._ok(handler, {"streamer": {"source": source}})

    def _route_streamer_snapshot(self, handler, request) -> None:
        if self.snapshot_size is None:
            self._json(handler, 503, False, {"error": "UnavailableError",
                                             "error_msg": "Service Unavailable"})
            return
        if self.snapshot_data is not None:
            data = self.snapshot_data
        else:
            data = make_jpeg(*self.snapshot_size)
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    # Keyboard and mouse: accepted and recorded.

    def _route_hid_print(self, handler, request) -> None:
        self._ok(handler)

    _route_hid_events_send_shortcut = _route_hid_print
    _route_hid_events_send_key = _route_hid_print
    _route_hid_events_send_mouse_button = _route_hid_print
    _route_hid_events_send_mouse_move = _route_hid_print
    _route_hid_events_send_mouse_wheel = _route_hid_print

    # Mass storage.

    def _route_msd(self, handler, request) -> None:
        if not self.msd_enabled:
            self._ok(handler, {"enabled": False, "online": False, "drive": None,
                               "storage": None})
            return
        drive = dict(self.drive)
        if drive["image"] is not None:
            drive["image"] = {"name": drive["image"], "size": self.images[drive["image"]]["size"]}
        self._ok(handler, {"enabled": True, "online": True, "busy": False, "drive": drive,
                           "storage": {"images": dict(self.images), "parts": {}}})

    def _route_msd_write(self, handler, request) -> None:
        name = request.query["image"]
        self.images[name] = {"size": len(request.body), "complete": True}
        self.uploads[name] = request.body
        self._ok(handler, {"image": {"name": name, "size": len(request.body), "written": 0}})

    def _route_msd_set_params(self, handler, request) -> None:
        if self.drive["connected"]:
            self._fail(handler, "MsdConnectedError", "the drive is connected")
            return
        name = request.query.get("image")
        if name not in self.images:
            self._fail(handler, "MsdUnknownImageError", "no such image")
            return
        self.drive["image"] = name
        size = self.images[name]["size"]
        cdrom = request.query.get("cdrom") == "1"
        # Like kvmd, quietly refuse to make an oversized image a CD, and
        # never make a CD writable.
        self.drive["cdrom"] = cdrom and size <= self.cdrom_limit
        writable = request.query.get("rw") == "1" and not self.drive["cdrom"]
        self.drive["rw"] = writable and not self.refuse_writable
        self._ok(handler)

    def _route_msd_set_connected(self, handler, request) -> None:
        connect = request.query.get("connected") == "1"
        if connect == self.drive["connected"]:
            error = "MsdConnectedError" if connect else "MsdDisconnectedError"
            self._fail(handler, error, "already in that state")
            return
        if connect and self.drive["image"] is None:
            self._fail(handler, "MsdImageNotSelected", "no image selected")
            return
        if not self.ignore_connect:
            self.drive["connected"] = connect
        self._ok(handler)
