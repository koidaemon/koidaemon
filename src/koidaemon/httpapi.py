# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""An optional HTTP API, for clients that cannot reach koid's Unix socket.

Off unless the configuration enables it, because it puts the targets'
keyboards (and USB drives) on the network.  When enabled, every request
needs the bearer token from token_file, and the API listens on localhost
unless told otherwise:

    [http]
    enabled = true
    listen = "127.0.0.1:8470"           # default
    token_file = "/etc/koidaemon/token" # required
    cert_file = "koid.pem"              # optional: serve HTTPS
    key_file = "koid.key"

Requests and replies are the same JSON objects as on the socket:

    POST /v1/command    a request; the reply is the socket's reply
    GET  /v1/targets    the same as the targets command
    POST /v1/upload     storage.insert with the image's contents as the
                        body, and the request in a Koid-Request header

A storage.insert sent to /v1/command with "upload" is answered
{"ok": true, "send": SIZE} if the KVM needs the image; the client then
sends it to /v1/upload.

The HTTP status mirrors the reply: 200 when ok, otherwise 400 (invalid),
401 (no or wrong token), 404 (unknown target), 422 (unsupported),
503 (busy) or 500 (failed).
"""

from __future__ import annotations

import hmac
import http.server
import ipaddress
import json
import logging
import ssl
import threading

from koidaemon.daemon import MAX_REQUEST, Daemon
from koidaemon.uploads import READ_CHUNK

LOG = logging.getLogger(__name__)

DEFAULT_LISTEN = "127.0.0.1:8470"

STATUS_FOR_ERROR = {
    "invalid": 400,
    "unknown-target": 404,
    "unsupported": 422,
    "busy": 503,
    "failed": 500,
}


def split_listen(listen: str) -> tuple[str, int]:
    """Split "host:port" (or "[v6 address]:port") into its parts."""
    host, separator, port = listen.rpartition(":")
    if not separator or not port.isdigit():
        raise ValueError("listen must be host:port, not %r" % listen)
    return host.strip("[]"), int(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class HTTPApi:
    """Serves a Daemon's requests over HTTP, in a thread of its own."""

    def __init__(self, daemon: Daemon, listen: str, token: str,
                 tls: ssl.SSLContext | None = None) -> None:
        if not token:
            raise ValueError("the HTTP API needs a token")
        host, port = split_listen(listen)
        if not is_loopback(host) and tls is None:
            LOG.warning("HTTP API on %s without TLS: the token and every request "
                        "can be read on the network", listen)

        handler = _handler_class(daemon, token)
        self._server = http.server.ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        if tls is not None:
            self._server.socket = tls.wrap_socket(self._server.socket, server_side=True)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address[:2]

    def start(self, poll_interval: float = 0.5) -> None:
        """Serve in a thread; stop() takes up to poll_interval to finish."""
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        args=(poll_interval,), name="koid-http", daemon=True)
        self._thread.start()
        host, port = self.address
        LOG.info("HTTP API listening on %s:%d", host, port)

    def stop(self) -> None:
        # shutdown() waits for serve_forever() to return, so only call it
        # if the server was started.
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()


def _handler_class(daemon: Daemon, token: str) -> type:
    expected = ("Bearer " + token).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "koid"

        def log_message(self, format, *args) -> None:
            LOG.debug("http %s: " + format, self.client_address[0], *args)

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode() + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, code: str, message: str) -> None:
            self._reply(status, {"ok": False, "error": {"code": code, "message": message}})

        def _authorised(self) -> bool:
            given = self.headers.get("Authorization", "").encode()
            # compare_digest takes the same time however much matches.
            if hmac.compare_digest(given, expected):
                return True
            self._error(401, "unauthorised", "a valid bearer token is required")
            return False

        def _run(self, request) -> None:
            reply = daemon.handle(request)
            if reply["ok"]:
                status = 200
            else:
                status = STATUS_FOR_ERROR.get(reply["error"]["code"], 500)
            self._reply(status, reply)

        def do_GET(self) -> None:
            if not self._authorised():
                return
            if self.path != "/v1/targets":
                self._error(404, "invalid", "no such path")
                return
            self._run({"command": "targets"})

        def do_POST(self) -> None:
            if not self._authorised():
                return
            if self.path == "/v1/upload":
                self._upload()
                return
            if self.path != "/v1/command":
                self._error(404, "invalid", "no such path")
                return

            length = self.headers.get("Content-Length", "")
            if not length.isdigit() or int(length) > MAX_REQUEST:
                self._error(400, "invalid", "a request needs a Content-Length up to %d"
                            % MAX_REQUEST)
                return
            try:
                request = json.loads(self.rfile.read(int(length)))
            except ValueError:
                self._error(400, "invalid", "not JSON")
                return
            self._run(request)

        def _upload(self) -> None:
            """storage.insert, with the image as the body."""
            length = self.headers.get("Content-Length", "")
            header = self.headers.get("Koid-Request", "")
            try:
                request = json.loads(header)
                upload = request["args"]["upload"]
                size = upload["size"]
            except (ValueError, KeyError, TypeError):
                request = size = None
            if (not isinstance(request, dict) or request.get("command") != "storage.insert"
                    or not length.isdigit() or size != int(length)):
                # The body cannot be told apart from a next request.
                self.close_connection = True
                self._error(400, "invalid", "an upload needs a Koid-Request header holding a "
                                            "storage.insert with upload, and its size as the "
                                            "Content-Length")
                return

            asked = []

            def receive(wanted: int):
                asked.append(wanted)
                return self.rfile

            reply = daemon.handle(request, receive)
            if not asked:
                if reply["ok"]:
                    # The KVM already had the image; take the body anyway,
                    # so that the client gets to read the reply.
                    try:
                        _discard(self.rfile, size)
                    except OSError:
                        self.close_connection = True
                        return
                else:
                    self.close_connection = True
            status = 200 if reply["ok"] else STATUS_FOR_ERROR.get(reply["error"]["code"], 500)
            self._reply(status, reply)

    return Handler


def _discard(stream, size: int) -> None:
    while size > 0:
        data = stream.read(min(size, READ_CHUNK))
        if not data:
            return
        size -= len(data)
