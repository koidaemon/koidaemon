# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""The Radxa Linkr's disk images, through the API its web interface uses.

None of this is documented by Radxa; it was read from the web interface a
Linkr running firmware 1.6.0 serves, and tried on one.  See docs/linkr.md.

The API needs a login with the Linkr's own user and password, not the
access token the public API takes.  Images are listed, mounted, unmounted
and deleted over HTTP, and uploaded in chunks as protobuf messages over
the Linkr's WebSocket.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import urllib.error
import urllib.request
from typing import BinaryIO

from koidaemon.errors import BackendError
from koidaemon.uploads import READ_CHUNK, DiskImage, LocalImage, UploadError
from koidaemon.websocket import WebSocket, WebSocketError

LOG = logging.getLogger(__name__)

# The Linkr hashes the password with this in front before it is sent.
PASSWORD_SALT = "RADXA_FIXED_SALT_v1"

# Images go up in chunks of this size, as the web interface sends them.
CHUNK_SIZE = 2 * 1024 * 1024

# Images are stored under a name carrying the start of their SHA-256, so
# one already on the Linkr is not sent again.
IMAGE_PREFIX = "koid-"

# How long the Linkr may take to check a whole image's MD5 once it has
# every chunk.
END_WAIT = 600.0

# WsMessage's MessageType values, and the fields holding each request and
# its answer.
UPLOAD_START, UPLOAD_CHUNK, UPLOAD_END, UPLOAD_CANCEL = 8, 9, 10, 11
FIELDS = {
    UPLOAD_START: (20, 21),
    UPLOAD_CHUNK: (22, 23),
    UPLOAD_END: (24, 25),
    UPLOAD_CANCEL: (26, 27),
}
# WsMessage fields.
MESSAGE_TYPE, TOKEN, MESSAGE_CODE, MESSAGE = 1, 2, 3, 4
# FileUploadType: a virtual disk image (rather than a firmware update).
VIRTUAL_DISK = 0
# An answer's MessageCode when the token has expired.
CODE_EXPIRED = 403


def hash_password(password: str) -> str:
    """The password as the Linkr's login takes it."""
    return hashlib.sha256((PASSWORD_SALT + password).encode("utf-8")).hexdigest()


def image_name(file_name: str, digest: str) -> str:
    """The name an image is stored under on the Linkr."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", file_name)
    return "%s%s-%s" % (IMAGE_PREFIX, digest[:12], safe)


# ----------------------------------------------------------------------
# Just enough protobuf: varints, strings and bytes.
# ----------------------------------------------------------------------

def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("negative varints are not needed here")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def encode(fields: list[tuple[int, int | str | bytes]]) -> bytes:
    """A protobuf message from (field number, value) pairs.  Zeros and
    empty values are left out, as protobuf does with defaults."""
    out = bytearray()
    for number, value in fields:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            if value:
                out += _varint(number << 3) + _varint(value)
            continue
        if isinstance(value, str):
            value = value.encode("utf-8")
        if value:
            out += _varint(number << 3 | 2) + _varint(len(value)) + value
    return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("the message ends inside a number")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, offset


def decode(data: bytes) -> dict[int, int | bytes]:
    """A protobuf message's fields by number: varints as ints, everything
    length-delimited as bytes.  Other wire types are skipped."""
    fields: dict[int, int | bytes] = {}
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            fields[number], offset = _read_varint(data, offset)
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            if offset + length > len(data):
                raise ValueError("the message ends inside a field")
            fields[number] = data[offset:offset + length]
            offset += length
        elif wire_type == 5:
            offset += 4
        elif wire_type == 1:
            offset += 8
        else:
            raise ValueError("unknown wire type %d" % wire_type)
    return fields


# ----------------------------------------------------------------------

class LinkrStorage:
    """The Linkr's disk images, for one Linkr and one user."""

    def __init__(self, url: str, user: str, password: str, tls, timeout: float) -> None:
        self.url = url
        self.user = user
        self._password = password
        self._tls = tls
        self.timeout = timeout
        self._jwt: str | None = None

    # -- HTTP ------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 jwt: str | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if jwt:
            request.add_header("Authorization", "Bearer %s" % jwt)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self._tls) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise _Refused() from error
            raise BackendError("%s: HTTP %d for %s" % (self.url, error.code, path)) from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise BackendError("cannot reach %s: %s" % (self.url, reason)) from error
        try:
            answer = json.loads(raw)
        except ValueError as error:
            raise BackendError("%s sent something that is not JSON for %s"
                               % (self.url, path)) from error
        if not isinstance(answer, dict):
            raise BackendError("%s sent an odd answer for %s" % (self.url, path))
        if answer.get("code") == 403:
            raise _Refused()
        if answer.get("code") != 0:
            raise BackendError("%s: %s" % (self.url, answer.get("message") or answer.get("msg")
                                           or "%s failed" % path))
        return answer.get("data") or {}

    def login(self) -> str:
        try:
            data = self._request("POST", "/api/account/login",
                                 {"username": self.user, "passwd": hash_password(self._password)})
        except _Refused as error:
            raise BackendError("%s refused the login for %s" % (self.url, self.user)) from error
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise BackendError("%s accepted the login for %s but sent no token"
                               % (self.url, self.user))
        self._jwt = token
        return token

    def _jwt_or_login(self) -> str:
        return self._jwt or self.login()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        """A request with the login token, logging in again once if the
        Linkr has forgotten it."""
        try:
            return self._request(method, path, body, self._jwt_or_login())
        except _Refused:
            self._jwt = None
        try:
            return self._request(method, path, body, self.login())
        except _Refused as error:
            raise BackendError("%s refused the login for %s" % (self.url, self.user)) from error

    def images(self) -> tuple[list[dict], str]:
        """The stored images and the name of the mounted one ("" if none)."""
        data = self._call("GET", "/api/storage/list")
        listed = data.get("list") if isinstance(data, dict) else None
        mounted = data.get("mounted") if isinstance(data, dict) else ""
        return [image for image in (listed or []) if isinstance(image, dict)], mounted or ""

    def free_space(self) -> int:
        return int(self._call("GET", "/api/storage/space").get("free", 0))

    def mount(self, name: str) -> None:
        self._call("POST", "/api/storage/mount", {"filename": name})

    def unmount(self) -> None:
        self._call("POST", "/api/storage/unmount")

    def delete(self, name: str) -> None:
        self._call("DELETE", "/api/storage/file", {"filename": name})

    # -- Upload ----------------------------------------------------------

    def upload(self, image: DiskImage, name: str) -> None:
        """Upload an image under name.

        The Linkr wants the image's MD5 before any of it, so a file koid
        can read is read twice; an image a client sends is kept in a
        temporary file while its MD5 is worked out.
        """
        if isinstance(image, LocalImage):
            md5 = _file_md5(image)
            contents = image.open()
        else:
            contents, md5 = _spool(image)
        try:
            try:
                self._send(contents, name, image.size, md5)
            except _Expired:
                # The Linkr carries on where the upload stopped.
                self._jwt = None
                self.login()
                try:
                    self._send(contents, name, image.size, md5)
                except _Expired as error:
                    raise BackendError("%s keeps refusing the login token during the upload"
                                       % self.url) from error
        finally:
            contents.close()

    def _send(self, contents: BinaryIO, name: str, size: int, md5: str) -> None:
        chunks = max(1, -(-size // CHUNK_SIZE))
        url = self.url.replace("http", "ws", 1) + "/ws"
        LOG.info("uploading %s (%d bytes) to %s", name, size, self.url)
        try:
            with WebSocket(url, tls=self._tls, timeout=self.timeout,
                           keep_messages=True) as socket:
                upload = _Upload(socket, self._jwt_or_login(), self.url, self.timeout)
                start = upload.ask(UPLOAD_START, [(1, VIRTUAL_DISK), (2, name), (3, md5),
                                                  (4, size), (5, chunks)])
                index = int(start.get(3, 0))
                while index < chunks:
                    contents.seek(index * CHUNK_SIZE)
                    data = contents.read(CHUNK_SIZE)
                    if not data:
                        raise BackendError("%s ended before chunk %d" % (name, index))
                    answer = upload.ask(UPLOAD_CHUNK, [(1, VIRTUAL_DISK), (2, name), (3, md5),
                                                       (4, index), (5, data)])
                    following = int(answer.get(2, 0))
                    if following <= index:
                        raise BackendError("the Linkr asked for chunk %d again" % following)
                    index = following
                upload.ask(UPLOAD_END, [(1, VIRTUAL_DISK), (2, name), (3, md5)],
                           timeout=max(self.timeout, END_WAIT))
        except WebSocketError as error:
            raise BackendError("%s: the upload of %s broke off: %s"
                               % (self.url, name, error)) from error


class _Refused(Exception):
    """The Linkr refused the login token."""


class _Expired(Exception):
    """The Linkr said the login token had expired, part way through."""


class _Upload:
    """Requests and answers over the Linkr's WebSocket."""

    def __init__(self, socket: WebSocket, jwt: str, url: str, timeout: float) -> None:
        self.socket = socket
        self.jwt = jwt
        self.url = url
        self.timeout = timeout

    def ask(self, kind: int, fields: list, timeout: float | None = None) -> dict:
        """Send one request and return the fields of the Linkr's answer."""
        request_field, answer_field = FIELDS[kind]
        message = encode([(MESSAGE_TYPE, kind), (TOKEN, self.jwt)])
        message += encode([(request_field, encode(fields))])
        self.socket.send_binary(message)
        while True:
            try:
                answer = decode(self.socket.receive(timeout=timeout or self.timeout))
            except ValueError as error:
                raise BackendError("%s sent a message koid cannot read" % self.url) from error
            code = answer.get(MESSAGE_CODE, 0)
            if code == CODE_EXPIRED:
                raise _Expired()
            if answer.get(MESSAGE_TYPE, 0) != kind:
                # Something else the Linkr says, such as Wi-Fi news.
                continue
            text = answer.get(MESSAGE, b"")
            text = text.decode("utf-8", "replace") if isinstance(text, bytes) else ""
            if code:
                if text == "no_space":
                    raise BackendError("the Linkr has no space left for the image")
                raise BackendError("the Linkr refused the upload: %s" % (text or "code %d" % code))
            reply = answer.get(answer_field, b"")
            return decode(reply) if isinstance(reply, bytes) else {}


def _file_md5(image: LocalImage) -> str:
    digest = hashlib.md5()
    with image.open() as contents:
        while True:
            data = contents.read(READ_CHUNK)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _spool(image: DiskImage) -> tuple[BinaryIO, str]:
    """Copy an image a client sends into a temporary file: (file, MD5)."""
    # Opening may raise SendImage, for a client that sends the contents in
    # a request of its own; nothing is made until then.
    contents = image.open()
    spooled = tempfile.TemporaryFile(prefix="koid-linkr-")
    digest = hashlib.md5()
    try:
        while True:
            data = contents.read(READ_CHUNK)
            if not data:
                break
            digest.update(data)
            spooled.write(data)
    except (UploadError, OSError):
        spooled.close()
        raise
    finally:
        close = getattr(contents, "close", None)
        if close is not None:
            close()
    if spooled.tell() != image.size:
        spooled.close()
        raise UploadError("%s is %d bytes, not %d" % (image.name, spooled.tell(), image.size))
    spooled.seek(0)
    return spooled, digest.hexdigest()

