# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A stand-in for the Radxa Linkr: its public API, and the private API
its web interface uses for disk images, answering as a Linkr running
firmware 1.6.0 did."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading

from koidaemon import websocket
from koidaemon.backends import linkr_storage as proto

from fake_kvmd import make_jpeg


class FakeLinkr:
    def __init__(self, token: str = "radxa_linkr_secret", user: str = "admin",
                 password: str = "hunter2") -> None:
        self.token = token
        self.user = user
        self.password = password
        self.screen = (1920, 1080)
        # Each list of events posted to /api/public/control, in order.
        self.controls: list[list] = []
        self.snapshots = 0
        self.headers: list = []
        # Set to make the control request answer this, e.g. {"code": 1, ...}.
        self.control_answer: dict | None = None
        # Served instead of a JPEG of the screen's size, when set.
        self.snapshot_data: bytes | None = None

        # The private API.
        self.logins = 0
        self.jwts: set[str] = set()
        # Stored images by name, and what each upload in progress has.
        self.images: dict[str, bytes] = {}
        self.partial: dict[str, dict[int, bytes]] = {}
        self.mounted = ""
        self.space = 10 * 1024 * 1024 * 1024
        # Requests to /api/storage, as (method, path, body).
        self.storage_calls: list[tuple] = []
        # Every WsMessage the client sent, decoded.
        self.ws_messages: list[dict] = []
        # Misbehaviour: expire the token after this many chunks, or say
        # there is no space at the start of an upload.
        self.expire_after_chunks: int | None = None
        self.no_space = False

        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

            def do_DELETE(self) -> None:
                fake._handle(self, "DELETE")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.02,),
                                        daemon=True)
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self) -> "FakeLinkr":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    def expire_tokens(self) -> None:
        self.jwts.clear()

    def _send(self, handler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _json(self, handler, status: int, answer: dict) -> None:
        self._send(handler, status, json.dumps(answer).encode(), "application/json")

    def _ok(self, handler, data=None) -> None:
        self._json(handler, 200, {"code": 0, "data": {} if data is None else data})

    def _handle(self, handler, method: str) -> None:
        if handler.path == "/ws" and handler.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket(handler)
            return
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        self.headers.append(handler.headers)

        if handler.path.startswith("/api/public/"):
            self._public(handler, method, body)
        elif handler.path == "/api/account/login" and method == "POST":
            self._login(handler, body)
        elif handler.path.startswith("/api/storage/"):
            if handler.headers.get("Authorization", "")[7:] not in self.jwts:
                self._json(handler, 403, {"code": 403, "message": "No Permisson"})
                return
            self._storage(handler, method, handler.path, json.loads(body) if body else None)
        else:
            self._json(handler, 404, {"code": 404, "data": None, "message": "Not Found"})

    # Public API.

    def _public(self, handler, method: str, body: bytes) -> None:
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

    # Private API.

    def _login(self, handler, body: bytes) -> None:
        request = json.loads(body)
        expected = hashlib.sha256(("RADXA_FIXED_SALT_v1" + self.password).encode()).hexdigest()
        if request.get("username") != self.user or request.get("passwd") != expected:
            self._json(handler, 200, {"code": 1, "message": "wrong user or password"})
            return
        self.logins += 1
        jwt = "jwt-%d" % self.logins
        self.jwts.add(jwt)
        self._ok(handler, {"token": jwt})

    def _storage(self, handler, method: str, path: str, body) -> None:
        self.storage_calls.append((method, path, body))
        if method == "GET" and path == "/api/storage/list":
            listed = [{"name": name, "md5": "", "size": len(data),
                       "mod_time": "2021-01-01T12:00:00Z"}
                      for name, data in self.images.items()]
            listed += [{"name": name, "md5": "", "size": sum(map(len, parts.values())),
                        "mod_time": "2021-01-01T12:00:00Z"}
                       for name, parts in self.partial.items() if name not in self.images]
            self._ok(handler, {"list": listed, "mounted": self.mounted})
        elif method == "GET" and path == "/api/storage/space":
            used = sum(map(len, self.images.values()))
            self._ok(handler, {"total": self.space, "used": used, "free": self.space - used})
        elif method == "POST" and path == "/api/storage/mount":
            if body["filename"] not in self.images:
                self._json(handler, 200, {"code": 1, "msg": "no such file"})
                return
            self.mounted = body["filename"]
            self._ok(handler)
        elif method == "POST" and path == "/api/storage/unmount":
            self.mounted = ""
            self._ok(handler)
        elif method == "DELETE" and path == "/api/storage/file":
            self.images.pop(body["filename"], None)
            self._ok(handler)
        else:
            self._json(handler, 404, {"code": 404, "message": "Not Found"})

    # The WebSocket, for uploads.

    def _websocket(self, handler) -> None:
        handler.send_response(101)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept",
                            websocket.accept_key(handler.headers["Sec-WebSocket-Key"]))
        handler.end_headers()
        handler.close_connection = True
        # Like a Linkr, say something unasked first.
        self._reply(handler, {proto.MESSAGE_TYPE: 7, proto.MESSAGE: "wifi"})
        chunks_seen = 0
        while True:
            try:
                opcode, payload = websocket.read_frame(handler.rfile)
            except (OSError, websocket.WebSocketError):
                return
            if opcode == websocket.OPCODE_CLOSE:
                handler.wfile.write(websocket.encode_frame(websocket.OPCODE_CLOSE, payload))
                return
            if opcode != websocket.OPCODE_BINARY:
                continue
            message = proto.decode(payload)
            kind = message.get(proto.MESSAGE_TYPE, 0)
            request_field, answer_field = proto.FIELDS.get(kind, (None, None))
            request = proto.decode(message.get(request_field, b"")) if request_field else {}
            self.ws_messages.append({"type": kind, "token": message.get(proto.TOKEN, b"").decode(),
                                     "request": request})
            if message.get(proto.TOKEN, b"").decode() not in self.jwts:
                self._reply(handler, {proto.MESSAGE_TYPE: kind, proto.MESSAGE_CODE: 403,
                                      proto.MESSAGE: "token expired"})
                continue
            name = request.get(2, b"").decode()
            md5 = request.get(3, b"").decode()
            if kind == proto.UPLOAD_START:
                if self.no_space:
                    self._reply(handler, {proto.MESSAGE_TYPE: kind, proto.MESSAGE_CODE: 1,
                                          proto.MESSAGE: "no_space"})
                    continue
                parts = self.partial.setdefault(name, {})
                self._answer(handler, kind, answer_field,
                             [(2, md5), (3, len(parts))])
            elif kind == proto.UPLOAD_CHUNK:
                index = request.get(4, 0)
                self.partial[name][index] = request.get(5, b"")
                chunks_seen += 1
                if self.expire_after_chunks is not None and chunks_seen >= self.expire_after_chunks:
                    self.expire_after_chunks = None
                    self.expire_tokens()
                self._answer(handler, kind, answer_field, [(1, md5), (2, index + 1)])
            elif kind == proto.UPLOAD_END:
                parts = self.partial.pop(name)
                data = b"".join(parts[index] for index in sorted(parts))
                if hashlib.md5(data).hexdigest() != md5:
                    self._reply(handler, {proto.MESSAGE_TYPE: kind, proto.MESSAGE_CODE: 1,
                                          proto.MESSAGE: "md5 mismatch"})
                    continue
                self.images[name] = data
                self._answer(handler, kind, answer_field, [(2, md5)])

    def _answer(self, handler, kind: int, field: int, fields: list) -> None:
        self._reply(handler, {proto.MESSAGE_TYPE: kind, proto.MESSAGE: "success",
                              field: proto.encode(fields)})

    def _reply(self, handler, fields: dict) -> None:
        message = proto.encode(sorted(fields.items()))
        handler.wfile.write(websocket.encode_frame(websocket.OPCODE_BINARY, message))
        handler.wfile.flush()
