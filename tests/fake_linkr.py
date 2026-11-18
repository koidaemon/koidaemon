# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A stand-in for the Radxa Linkr's public API."""

from __future__ import annotations

import http.server
import json
import threading

from fake_kvmd import make_jpeg


class FakeLinkr:
    def __init__(self, token: str = "radxa_linkr_secret") -> None:
        self.token = token
        self.screen = (1920, 1080)
        # Each list of events posted to /api/public/control, in order.
        self.controls: list[list] = []
        self.snapshots = 0
        self.headers: list = []
        # Set to make the control request answer this, e.g. {"code": 1, ...}.
        self.control_answer: dict | None = None
        # Served instead of a JPEG of the screen's size, when set.
        self.snapshot_data: bytes | None = None

        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.02,),
                                        daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self) -> "FakeLinkr":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _send(self, handler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _json(self, handler, status: int, answer: dict) -> None:
        self._send(handler, status, json.dumps(answer).encode(), "application/json")

    def _handle(self, handler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        self.headers.append(handler.headers)

        if handler.headers.get("Authorization") != "token %s" % self.token:
            self._json(handler, 403, {"code": 403, "data": None, "message": "No Permission"})
            return

        if method == "GET" and handler.path == "/api/public/snapshot":
            self.snapshots += 1
            data = self.snapshot_data or make_jpeg(*self.screen)
            self._send(handler, 200, data, "image/jpeg")
        elif method == "POST" and handler.path == "/api/public/control":
            events = json.loads(body)["events"]
            self.controls.append(events)
            self._json(handler, 200, self.control_answer
                       or {"code": 0, "data": None, "message": "success"})
        else:
            self._json(handler, 404, {"code": 404, "data": None, "message": "Not Found"})
