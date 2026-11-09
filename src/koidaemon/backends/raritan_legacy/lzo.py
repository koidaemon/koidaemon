# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""LZO1X decompression, in pure Python.

The switch compresses screen data with LZO1X.  Python has no LZO module in
its standard library, and the data is small (one packet at a time), so the
classic decompressor is written out here.  The structure follows
lzo1x_decompress() from the LZO library: a literal run, then a sequence of
matches, each optionally followed by up to three literal bytes.

Match encodings, chosen by the first byte t of the instruction:

  t >= 64   M2: length 3-8, distance up to 2 KiB, one extra byte
  t >= 32   M3: length 3+, distance up to 16 KiB, two extra bytes
  t >= 16   M4: length 3+, distance 16-48 KiB, two extra bytes;
            a distance of zero marks the end of the stream
  t < 16    M1: length 2 (or 3 straight after a literal run), short distance

The bottom two bits of the last byte of every match say how many literal
bytes (0-3) follow it.
"""

from __future__ import annotations


class LZOError(Exception):
    """The compressed data is corrupt or truncated."""


class _Reader:
    """Cursor over the input, with bounds checks that raise LZOError."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise LZOError("input ends in the middle of an instruction")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def peek(self) -> int:
        if self.pos >= len(self.data):
            raise LZOError("input ends in the middle of an instruction")
        return self.data[self.pos]

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise LZOError("literal run goes past the end of the input")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def long_length(self, base: int) -> int:
        """Lengths that do not fit the instruction byte: runs of zero bytes
        each add 255, then a final non-zero byte is added to base."""
        length = 0
        while self.peek() == 0:
            length += 255
            self.pos += 1
        return length + base + self.byte()


def _copy_match(out: bytearray, distance_back: int, length: int) -> None:
    start = len(out) - distance_back
    if start < 0:
        raise LZOError("match refers to data before the start of the output")
    # Copy one byte at a time: a match may overlap the bytes it produces,
    # which is how LZO encodes runs.
    for index in range(length):
        out.append(out[start + index])


def decompress(data: bytes, max_output: int | None = None) -> bytes:
    """Decompress one raw LZO1X stream (no header, no checksum)."""
    src = _Reader(data)
    out = bytearray()

    # The first byte may be a literal run that does not follow the usual
    # instruction encoding.
    state = "literal_or_match"
    first = src.peek()
    if first > 17:
        src.byte()
        count = first - 17
        if count < 4:
            out += src.take(count)
            state = "match"
        else:
            out += src.take(count)
            state = "after_first_literal_run"

    while True:
        if max_output is not None and len(out) > max_output:
            raise LZOError("output is larger than %d bytes" % max_output)

        if state == "literal_or_match":
            t = src.byte()
            if t >= 16:
                state = ("match", t)
                continue
            if t == 0:
                t = src.long_length(15)
            out += src.take(t + 3)
            state = "after_first_literal_run"
            continue

        if state == "after_first_literal_run":
            t = src.byte()
            if t >= 16:
                state = ("match", t)
                continue
            # A 3-byte match from up to 3 KiB back, only valid straight
            # after a literal run.
            distance = 1 + 0x0800 + (t >> 2) + (src.byte() << 2)
            _copy_match(out, distance, 3)
            state = "trailing_literals"
            continue

        if state == "match":
            state = ("match", src.byte())
            continue

        if isinstance(state, tuple):
            t = state[1]
            if t >= 64:
                distance = 1 + ((t >> 2) & 7) + (src.byte() << 3)
                length = (t >> 5) - 1 + 2
            elif t >= 32:
                length = t & 31
                if length == 0:
                    length = src.long_length(31)
                low = src.byte()
                high = src.byte()
                distance = 1 + (low >> 2) + (high << 6)
                length += 2
            elif t >= 16:
                length = t & 7
                if length == 0:
                    length = src.long_length(7)
                low = src.byte()
                high = src.byte()
                distance = ((t & 8) << 11) + (low >> 2) + (high << 6)
                if distance == 0:
                    break  # end of stream marker
                distance += 0x4000
                length += 2
            else:
                distance = 1 + (t >> 2) + (src.byte() << 2)
                length = 2

            _copy_match(out, distance, length)
            state = "trailing_literals"
            continue

        if state == "trailing_literals":
            # Up to three literals, counted in the low bits of the byte just
            # before the current position.
            count = src.data[src.pos - 2] & 3
            if count == 0:
                state = "literal_or_match"
                continue
            out += src.take(count)
            state = "match"
            continue

    if src.pos != len(data):
        raise LZOError("%d bytes of input left after the end marker" % (len(data) - src.pos))
    return bytes(out)
