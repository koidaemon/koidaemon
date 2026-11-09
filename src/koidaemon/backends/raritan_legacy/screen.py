# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Screenshots from the switch's video stream.

Tested against a real switch, which produced readable screenshots of a
Linux text console and of test patterns.  How the stream works:

  * after connecting to a port the switch "autosenses" the video, then
    sends NEW_VIDEO_MODE, giving the screen size, the size of the cells the
    screen is cut into, and the pixel format ("colour table", CCT);
  * screen content then arrives as CELL_DATA packets.  Each carries a
    number of cells, usually LZO1X-compressed.  Every cell starts with an
    8-byte header (bytes 2-3 are the cell index), followed by its pixels
    row by row;
  * REFRESH asks the switch to send the whole screen again;
  * VIDEO_MARKER packets must be echoed back as VIDEO_SYNC (the session does
    that) or the switch stops sending.

There is no end-of-frame marker, so a frame is complete once every cell has
been received at least once.

The switch can lose sync with a signal it struggles with.  When it does, it
first sends a whole screen of black cells, then starts autosensing again
and sends a new NEW_VIDEO_MODE.  So a screenshot is the first complete
frame after a video mode, once it has gone SETTLE_TIME without the switch
starting over.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
import zlib
from dataclasses import dataclass

from koidaemon.backends.raritan_legacy import lzo, packets

LOG = logging.getLogger(__name__)

# Colour tables (pixel formats).  Only the 16-bit ones are decoded; the
# paletted ones need Raritan's palette tables, which cannot be checked
# without a video source.
CCT_RGB12 = 10
CCT_RGB15 = 12

# Ask for 15-bit colour when connecting to take a screenshot.
SCREENSHOT_COMPRESSION_PARAMS = struct.pack(
    ">IHHIIIIIIII",
    4,          # flags
    CCT_RGB15,  # colour table
    0,          # cache depth: cell caching must stay off
    2,          # compression mode: cells + LZO
    0, 0, 0,    # speed, minimum and maximum frame time
    6,          # smoothing
    0, 0, 0,
)

CELL_HEADER_SIZE = 8
CELL_DATA_HEADER_SIZE = 8
CELL_FLAG_LZO = 0x01
MAX_DECOMPRESSED_CELLS = 131072
NEW_VIDEO_MODE_SIZE = 65
VIDEO_FLAG_PUBLIC_VIEW_UPDATE = 0x2


class ScreenError(Exception):
    """A screenshot could not be taken."""


@dataclass
class VideoMode:
    """The contents of a NEW_VIDEO_MODE packet."""

    flags: int
    colour_table: int
    cell_width: int
    cell_height: int
    cell_count: int
    width: int              # visible size
    height: int
    buffer_width: int       # "logical" size: the frame buffer, cell aligned
    buffer_height: int
    device_id: int

    @classmethod
    def parse(cls, packet: bytes) -> "VideoMode":
        if len(packet) != NEW_VIDEO_MODE_SIZE:
            raise ScreenError("NEW_VIDEO_MODE is %d bytes, expected %d"
                              % (len(packet), NEW_VIDEO_MODE_SIZE))
        # Bytes 4-43 echo the compression parameters in effect; skip them.
        fields = struct.unpack(">HHHHHHHHHH", packet[44:64])
        (flags, colour_table, _refresh, cell_width, cell_height, cell_count,
         width, height, buffer_width, buffer_height) = fields
        return cls(flags, colour_table, cell_width, cell_height, cell_count,
                   width, height, buffer_width, buffer_height, packet[64])

    @property
    def cells_per_row(self) -> int:
        return math.ceil(self.buffer_width / self.cell_width)

    @property
    def total_cells(self) -> int:
        rows = math.ceil(self.buffer_height / self.cell_height)
        return self.cells_per_row * rows


def rgb555_to_rgb(value: int) -> tuple[int, int, int]:
    """0RRRRRGG GGGBBBBB to 8-bit channels, replicating the top bits."""
    red = (value >> 10) & 0x1F
    green = (value >> 5) & 0x1F
    blue = value & 0x1F
    return (red << 3 | red >> 2, green << 3 | green >> 2, blue << 3 | blue >> 2)


def rgb444_to_rgb(value: int) -> tuple[int, int, int]:
    """0000RRRR GGGGBBBB to 8-bit channels."""
    red = (value >> 8) & 0xF
    green = (value >> 4) & 0xF
    blue = value & 0xF
    return (red * 17, green * 17, blue * 17)


PIXEL_DECODERS = {
    CCT_RGB15: rgb555_to_rgb,
    CCT_RGB12: rgb444_to_rgb,
}


class FrameBuffer:
    """The screen, assembled from cells."""

    def __init__(self, mode: VideoMode) -> None:
        if mode.colour_table not in PIXEL_DECODERS:
            raise ScreenError("the switch picked colour table %d; only 15-bit (12) and "
                              "12-bit (10) colour are supported" % mode.colour_table)
        self.mode = mode
        self.decode_pixel = PIXEL_DECODERS[mode.colour_table]
        self.pixels = bytearray(mode.buffer_width * mode.buffer_height * 3)
        self.cells_seen: set[int] = set()

    @property
    def complete(self) -> bool:
        return len(self.cells_seen) >= self.mode.total_cells

    def apply_cell_data(self, packet: bytes) -> None:
        """Decode one CELL_DATA packet into the frame buffer."""
        cell_count = struct.unpack(">H", packet[4:6])[0]
        flags = packet[7]
        payload = packet[CELL_DATA_HEADER_SIZE:]
        if flags & CELL_FLAG_LZO:
            payload = lzo.decompress(payload, MAX_DECOMPRESSED_CELLS)

        mode = self.mode
        pixels_per_cell = mode.cell_width * mode.cell_height
        cell_size = CELL_HEADER_SIZE + pixels_per_cell * 2
        offset = 0

        for _ in range(cell_count):
            if offset + cell_size > len(payload):
                raise ScreenError("cell data ends early (%d cells announced)" % cell_count)
            index = struct.unpack(">H", payload[offset + 2:offset + 4])[0]
            pixel_data = payload[offset + CELL_HEADER_SIZE:offset + cell_size]
            offset += cell_size
            if index >= mode.total_cells:
                LOG.warning("ignoring cell %d of %d", index, mode.total_cells)
                continue
            self._put_cell(index, pixel_data)
            self.cells_seen.add(index)

    def _put_cell(self, index: int, pixel_data: bytes) -> None:
        mode = self.mode
        left = (index % mode.cells_per_row) * mode.cell_width
        top = (index // mode.cells_per_row) * mode.cell_height
        values = struct.unpack(">%dH" % (len(pixel_data) // 2), pixel_data)

        for row in range(mode.cell_height):
            y = top + row
            if y >= mode.buffer_height:
                break
            for column in range(mode.cell_width):
                x = left + column
                if x >= mode.buffer_width:
                    continue
                red, green, blue = self.decode_pixel(values[row * mode.cell_width + column])
                position = (y * mode.buffer_width + x) * 3
                self.pixels[position:position + 3] = bytes((red, green, blue))

    def to_png(self) -> bytes:
        """The visible part of the screen as a PNG image."""
        mode = self.mode
        rows = []
        for y in range(mode.height):
            start = y * mode.buffer_width * 3
            # Each PNG row starts with a filter type byte; 0 means "none".
            rows.append(b"\x00" + bytes(self.pixels[start:start + mode.width * 3]))
        return encode_png(mode.width, mode.height, b"".join(rows))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def encode_png(width: int, height: int, filtered_rows: bytes) -> bytes:
    """A minimal 8-bit RGB PNG writer."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(filtered_rows, 6))
            + _png_chunk(b"IEND", b""))


# How long a complete frame must go without the switch restarting its
# autosense before it is trusted.  On a real switch, losing sync came
# seconds after a good frame, never within a second.
SETTLE_TIME = 1.0


class ScreenGrabber:
    """Collects video packets from a session and keeps the first whole frame.

    Pass handle_video_packet as the session's video_listener before
    connecting to the port, then call grab().
    """

    def __init__(self, session, settle_time: float = SETTLE_TIME) -> None:
        self.session = session
        self.settle_time = settle_time
        self.frame: FrameBuffer | None = None
        self.error: Exception | None = None
        # The first complete frame since the latest video mode: its PNG, its
        # mode and when it completed.
        self.candidate: tuple[bytes, VideoMode, float] | None = None
        # The mode of the frame grab() returned.
        self.mode: VideoMode | None = None
        self._changed = threading.Condition()

    def handle_video_packet(self, packet: bytes) -> None:
        """Called by the session's reader thread for video and notifications."""
        try:
            command = packet[2]
            if command == packets.RSP_NEW_VIDEO_MODE:
                self._new_video_mode(packet)
            elif command == packets.RSP_CELL_DATA and self.frame is not None:
                self._cell_data(packet)
            elif command == packets.RSP_NOTIFY:
                self._notify(packet)
        except Exception as error:
            self.error = error
        with self._changed:
            self._changed.notify_all()

    def _new_video_mode(self, packet: bytes) -> None:
        mode = VideoMode.parse(packet)
        if self.frame is not None and mode.flags & VIDEO_FLAG_PUBLIC_VIEW_UPDATE:
            return
        LOG.info("video mode %dx%d, cells %dx%d, colour table %d",
                 mode.width, mode.height, mode.cell_width, mode.cell_height,
                 mode.colour_table)
        self.frame = FrameBuffer(mode)
        self.candidate = None
        # A new mode means a new screen: ask for all of it.
        self.session.request_refresh()

    def _cell_data(self, packet: bytes) -> None:
        self.frame.apply_cell_data(packet)
        # Keep the first complete frame.  Later cells can be the black
        # screen the switch sends as it loses sync.
        if self.candidate is None and self.frame.complete:
            self.candidate = (self.frame.to_png(), self.frame.mode, time.monotonic())

    def _notify(self, packet: bytes) -> None:
        event, parameter = struct.unpack(">II", packet[4:12])
        if event != packets.NOTIFY_AUTOSENSE or parameter != packets.AUTOSENSE_STARTED:
            return
        # The switch lost sync.  A frame that completed only just before
        # may already be the black screen it sends first.
        if self.candidate is not None and not self._settled(self.candidate):
            LOG.info("the switch lost sync; waiting for the next frame")
            self.candidate = None
        # Cells sent before the next video mode belong to no frame.
        self.frame = None

    def _settled(self, candidate: tuple[bytes, VideoMode, float]) -> bool:
        _png, _mode, completed = candidate
        return time.monotonic() - completed >= self.settle_time

    def grab(self, timeout: float = 30.0) -> bytes:
        """Wait for a whole frame that has settled and return it as a PNG.

        If the time runs out with a complete frame that has not settled,
        that frame is returned anyway.
        """
        self.session.request_refresh()
        deadline = time.monotonic() + timeout

        with self._changed:
            while True:
                if self.error is not None:
                    raise ScreenError("video decoding failed: %s" % self.error)

                candidate = self.candidate
                if candidate is not None and self._settled(candidate):
                    break
                if time.monotonic() > deadline:
                    if candidate is not None:
                        LOG.warning("returning a frame that had not settled")
                        break
                    if self.session.no_video:
                        raise ScreenError("the port has no video signal")
                    raise ScreenError("no complete frame after %.0f s; the switch may not "
                                      "lock onto this video mode" % timeout)
                self._changed.wait(0.2)

        png, self.mode, _completed = candidate
        return png
