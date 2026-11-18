# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import hashlib
import io

import pytest

from koidaemon.errors import BackendError
from koidaemon.uploads import CheckedReader, LocalImage, UploadError, file_digest


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_local_image(tmp_path):
    path = tmp_path / "disk.img"
    path.write_bytes(b"abc" * 1000)
    image = LocalImage(path)
    assert (image.name, image.size, image.digest()) == ("disk.img", 3000, digest(b"abc" * 1000))
    with image.open() as contents:
        assert contents.read() == b"abc" * 1000


def test_a_missing_local_image(tmp_path):
    image = LocalImage(tmp_path / "missing.img")
    for read in (lambda: image.size, image.digest, image.open):
        with pytest.raises(BackendError, match="cannot read .*missing.img"):
            read()


def test_file_digest(tmp_path):
    path = tmp_path / "x"
    path.write_bytes(b"x" * 3_000_000)
    assert file_digest(path) == digest(b"x" * 3_000_000)


def test_checked_reader_gives_exactly_the_size():
    data = b"0123456789"
    # More follows on the stream, such as the client's next request.
    reader = CheckedReader(io.BytesIO(data + b"next"), len(data), digest(data))
    assert reader.read(4) == b"0123"
    assert reader.read() == b"456789"
    assert reader.read() == b""
    assert reader.finished


def test_checked_reader_holds_back_a_bad_last_chunk():
    reader = CheckedReader(io.BytesIO(b"bbbb"), 4, digest(b"aaaa"))
    with pytest.raises(UploadError, match="does not match"):
        reader.read()


def test_checked_reader_on_a_short_stream():
    reader = CheckedReader(io.BytesIO(b"ab"), 4, digest(b"abcd"))
    assert reader.read() == b"ab"
    with pytest.raises(UploadError, match="2 bytes short"):
        reader.read()


class Broken:
    def read(self, size):
        raise ConnectionResetError("gone")


def test_checked_reader_on_a_broken_stream():
    with pytest.raises(UploadError, match="stopped arriving"):
        CheckedReader(Broken(), 4, digest(b"abcd")).read()


def test_drain_reads_what_is_left():
    stream = io.BytesIO(b"abcdefnext")
    reader = CheckedReader(stream, 6, digest(b"abcdef"))
    reader.read(2)
    reader.drain()
    assert reader.finished
    assert stream.read() == b"next"
