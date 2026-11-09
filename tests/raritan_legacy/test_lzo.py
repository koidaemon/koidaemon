# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import ctypes
import ctypes.util
import os
import random

import pytest

from koidaemon.backends.raritan_legacy import lzo

# Streams made by the reference LZO library (liblzo2), with their input.
VECTORS = [
    # lzo1x_1_compress
    (b"hello hello hello hello, world! hello world!",
     "0368656c6c6f202f140000032c20776f726c64212068656c6c6f20776f726c6421110000"),
    # lzo1x_1_compress: a long run, encoded as a match that overlaps itself
    (b"\x7f\xff" * 300,
     "037fff7fff7fff2000002514000b7fff7fff7fff7fff7fff7fff7fff110000"),
    # lzo1x_999_compress: uses more of the match encodings
    (b"abc" + bytes(40) + b"abcabcabc" + b"\x01\x02" * 50 + b"end",
     "1561626300200600004805aa00010220410700656e64110000"),
]


@pytest.mark.parametrize("plain, compressed", VECTORS)
def test_reference_vectors(plain, compressed):
    assert lzo.decompress(bytes.fromhex(compressed)) == plain


def test_truncated_input_is_an_error():
    compressed = bytes.fromhex(VECTORS[0][1])
    with pytest.raises(lzo.LZOError):
        lzo.decompress(compressed[:-3])


def test_trailing_garbage_is_an_error():
    compressed = bytes.fromhex(VECTORS[0][1])
    with pytest.raises(lzo.LZOError):
        lzo.decompress(compressed + b"\x00")


def test_match_before_the_start_is_an_error():
    # A literal of 4 bytes, then an M2 match reaching 300 bytes back.
    with pytest.raises(lzo.LZOError):
        lzo.decompress(bytes([17 + 4]) + b"abcd" + bytes([0x40 | 0x1C, 0x25]) + b"\x11\x00\x00")


def test_output_limit():
    with pytest.raises(lzo.LZOError):
        lzo.decompress(bytes.fromhex(VECTORS[1][1]), max_output=100)


# ----------------------------------------------------------------------
# Round trips through the real library, when it is installed.
# ----------------------------------------------------------------------

def _load_liblzo2():
    name = ctypes.util.find_library("lzo2")
    if name is None:
        return None
    try:
        return ctypes.CDLL(name)
    except OSError:
        return None


LIBLZO2 = _load_liblzo2()
needs_liblzo2 = pytest.mark.skipif(LIBLZO2 is None, reason="liblzo2 is not installed")


def reference_compress(data: bytes, function: str) -> bytes:
    output = ctypes.create_string_buffer(len(data) + len(data) // 16 + 64 + 3)
    output_length = ctypes.c_ulong(0)
    work_memory = ctypes.create_string_buffer(14 * 16384 * ctypes.sizeof(ctypes.c_void_p))
    status = getattr(LIBLZO2, function)(data, ctypes.c_ulong(len(data)), output,
                                        ctypes.byref(output_length), work_memory)
    assert status == 0
    return output.raw[:output_length.value]


def sample_inputs():
    generator = random.Random(1)
    pixels = b"".join(generator.choice([b"\x7f\xff", b"\x00\x00", b"\x12\x34"])
                      * generator.randint(1, 40) for _ in range(2000))
    text = open(lzo.__file__, "rb").read()
    return {
        "one byte": b"a",
        "zeros": bytes(70000),
        "random": os.urandom(5000),
        "pixels": pixels,
        "text": text,
        "two symbols": bytes(generator.choice(b"ab") for _ in range(20000)),
    }


@needs_liblzo2
@pytest.mark.parametrize("function", ["lzo1x_1_compress", "lzo1x_999_compress"])
@pytest.mark.parametrize("name", sorted(sample_inputs()))
def test_round_trip_with_liblzo2(function, name):
    data = sample_inputs()[name]
    assert lzo.decompress(reference_compress(data, function)) == data
