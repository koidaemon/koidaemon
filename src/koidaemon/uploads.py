# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Disk images for storage.insert.

An image reaches koid one of two ways:

* LocalImage: a file koid opens itself, named by an absolute path.  The
  client and koid must see the same file at the same path.
* PushedImage: a client sends the image's name, size and SHA-256 first,
  and its contents only if the KVM does not already have it.  Nothing
  has to be shared between the client and koid.

A backend asks an image for its name, size and digest, and calls open()
only when it needs the contents.  For a pushed image that is when the
client is asked to send them, and they are checked against the digest
as they are read.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from typing import BinaryIO, Callable

from koidaemon.errors import BackendError, KoiError

READ_CHUNK = 1024 * 1024
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Images are stored on a KVM under a name made from their contents, so an
# image that is already there is not uploaded again.
IMAGE_PREFIX = "koid-"

# Linux's USB mass storage gadget will not present a CD smaller than 300
# sectors of 2048 bytes.
MIN_CD_SIZE = 300 * 2048


class UploadError(KoiError):
    """A pushed image did not arrive whole and as described."""

    code = "failed"


class SendImage(Exception):
    """Raised when a client has to send the image in a request of its own.

    Over HTTP a request cannot be answered half way and then carry on, so
    koid replies {"ok": true, "send": SIZE} and the client sends the image
    to /v1/upload.
    """

    def __init__(self, size: int) -> None:
        super().__init__("send %d bytes" % size)
        self.size = size


def image_name(file_name: str, digest: str) -> str:
    """The name an image is stored under on a KVM."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", file_name)
    return "%s%s-%s" % (IMAGE_PREFIX, digest[:12], safe)


def file_digest(path: pathlib.Path) -> str:
    """The SHA-256 of a file, as lowercase hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as image:
        while True:
            chunk = image.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class DiskImage:
    """What a backend needs to know about an image."""

    name: str
    size: int

    def digest(self) -> str:
        raise NotImplementedError

    def open(self) -> BinaryIO:
        """A reader that gives exactly size bytes of the image."""
        raise NotImplementedError


class LocalImage(DiskImage):
    """A file koid can read."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.name = path.name
        self._digest: str | None = None

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError as error:
            raise BackendError("cannot read %s: %s" % (self.path, error.strerror)) from error

    def digest(self) -> str:
        if self._digest is None:
            try:
                self._digest = file_digest(self.path)
            except OSError as error:
                raise BackendError("cannot read %s: %s"
                                   % (self.path, error.strerror)) from error
        return self._digest

    def open(self) -> BinaryIO:
        try:
            return open(self.path, "rb")
        except OSError as error:
            raise BackendError("cannot read %s: %s" % (self.path, error.strerror)) from error


class PushedImage(DiskImage):
    """An image a client sends.

    receive(size) asks the client for the contents and returns the stream
    they arrive on, or raises SendImage if they have to come in another
    request.
    """

    def __init__(self, name: str, size: int, digest: str,
                 receive: Callable[[int], BinaryIO]) -> None:
        self.name = name
        self.size = size
        self._digest = digest
        self._receive = receive
        self.reader: CheckedReader | None = None

    def digest(self) -> str:
        return self._digest

    def open(self) -> BinaryIO:
        self.reader = CheckedReader(self._receive(self.size), self.size, self._digest)
        return self.reader


class CheckedReader:
    """Reads size bytes from a client's stream, checking them on the way.

    The last chunk is only handed on once the whole image matches its
    digest; otherwise reading fails, so whoever is passing the bytes on
    (an upload to the KVM) is cut short rather than finishing a bad image.
    """

    def __init__(self, stream: BinaryIO, size: int, digest: str) -> None:
        self._stream = stream
        self._remaining = size
        self._expected = digest
        self._hash = hashlib.sha256()

    @property
    def finished(self) -> bool:
        return self._remaining == 0

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        size = min(size, READ_CHUNK)
        try:
            data = self._stream.read(size)
        except OSError as error:
            raise UploadError("the image stopped arriving: %s" % error) from error
        if not data:
            raise UploadError("the image ended %d bytes short" % self._remaining)
        self._remaining -= len(data)
        self._hash.update(data)
        if self._remaining == 0 and self._hash.hexdigest() != self._expected:
            raise UploadError("the image does not match its SHA-256")
        return data

    def drain(self) -> None:
        """Read and drop whatever has not been read, so the client's
        connection is ready for the reply."""
        while self._remaining > 0:
            try:
                data = self._stream.read(min(self._remaining, READ_CHUNK))
            except OSError:
                return
            if not data:
                return
            self._remaining -= len(data)
