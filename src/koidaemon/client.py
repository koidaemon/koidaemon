# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Talking to koid from Python, over its Unix socket or its HTTP API.

    from koidaemon.client import Client

    koi = Client()      # the socket; Client(url=..., token=...) for HTTP
    koi.call("keyboard.type", "board-a", text="root\\n")
    snapshot = koi.snapshot("board-a")

Errors from the daemon are raised as the matching koidaemon.errors class.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import ssl
import urllib.error
import urllib.request

from koidaemon.backends.base import Snapshot
from koidaemon.config import DEFAULT_SOCKET
from koidaemon.errors import ERRORS_BY_CODE, KoiError


def default_socket() -> pathlib.Path:
    """$KOID_SOCKET if set, else koid's default socket."""
    return pathlib.Path(os.environ.get("KOID_SOCKET", DEFAULT_SOCKET))


class Client:
    """A connection to koid.  With a url it uses the HTTP API, which needs
    the token configured in koid's [http] section."""

    def __init__(self, socket_path: pathlib.Path | None = None, url: str | None = None,
                 token: str | None = None, ca_file: pathlib.Path | None = None,
                 timeout: float | None = None) -> None:
        self.socket_path = socket_path or default_socket()
        self.url = url.rstrip("/") if url else None
        if self.url and not token:
            raise KoiError("the HTTP API needs a token")
        self.token = token
        self.ca_file = ca_file
        # No timeout by default: uploading a storage image can take minutes.
        self.timeout = timeout

    def call(self, command: str, target: str | None = None, **args) -> dict:
        """Run one command and return its result."""
        request = {"command": command, "args": args}
        if target is not None:
            request["target"] = target

        if self.url:
            reply = self._call_http(request)
        else:
            reply = self._call_socket(request)

        if reply.get("ok"):
            return reply.get("result", {})
        error = reply.get("error", {})
        error_class = ERRORS_BY_CODE.get(error.get("code"), KoiError)
        raise error_class(error.get("message", "unknown error"))

    def _call_socket(self, request: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            try:
                connection.connect(str(self.socket_path))
            except OSError as error:
                raise KoiError("cannot connect to koid at %s: %s"
                               % (self.socket_path, error.strerror)) from error
            try:
                connection.sendall(json.dumps(request).encode() + b"\n")
                with connection.makefile("rb") as replies:
                    line = replies.readline()
            except OSError:
                # The daemon went away mid-request; treat it like no reply.
                line = b""

        if not line:
            raise KoiError("koid closed the connection without replying")
        return json.loads(line)

    def _call_http(self, request: dict) -> dict:
        http_request = urllib.request.Request(
            self.url + "/v1/command",
            data=json.dumps(request).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        context = None
        if self.url.startswith("https://"):
            cafile = str(self.ca_file) if self.ca_file else None
            context = ssl.create_default_context(cafile=cafile)

        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout,
                                        context=context) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            # Failed commands come back with an error status and the same
            # JSON reply as on the socket.
            body = error.read()
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise KoiError("cannot reach koid at %s: %s" % (self.url, reason)) from error

        try:
            return json.loads(body)
        except ValueError as error:
            raise KoiError("koid at %s sent something that is not JSON" % self.url) from error

    def targets(self) -> dict:
        return self.call("targets")["targets"]

    def snapshot(self, target: str) -> Snapshot:
        result = self.call("snapshot", target)
        data = base64.b64decode(result["data"])
        return Snapshot(result["format"], data, result.get("width"), result.get("height"))
