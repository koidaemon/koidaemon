# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Screenshot decoding, on synthetic packets.

The packets are built to the layout Raritan's client expects and a real
switch sends, and the packet sequences follow what a real switch did.
"""

import struct
import time
import zlib

import pytest

from koidaemon.backends.raritan_legacy import packets, screen

RED = 0x7C00
GREEN = 0x03E0
BLUE = 0x001F
WHITE = 0x7FFF


def new_video_mode(width, height, buffer_width, buffer_height, cell=4,
                   colour_table=screen.CCT_RGB15, flags=1):
    cells = (buffer_width // cell) * (buffer_height // cell)
    payload = bytes(40)  # compression parameters in effect
    payload += struct.pack(">HHHHHHHHHH", flags, colour_table, 60, cell, cell, cells,
                           width, height, buffer_width, buffer_height)
    payload += b"\x00"  # device id
    return packets.pack_command(packets.RSP_NEW_VIDEO_MODE, payload)


def cell(index, colour, size=4):
    header = struct.pack(">HHI", 0, index, 0)
    return header + struct.pack(">H", colour) * (size * size)


def cell_data(cells, compressed=False):
    payload = b"".join(cells)
    flags = 0
    if compressed:
        # The simplest valid LZO1X stream: one literal run, then the end
        # marker.  Fine for up to 238 bytes.
        assert len(payload) <= 238
        payload = bytes([17 + len(payload)]) + payload + b"\x11\x00\x00"
        flags = screen.CELL_FLAG_LZO
    header = struct.pack(">HBB", len(cells), 0, flags)
    return packets.pack_command(packets.RSP_CELL_DATA, header + payload)


def notify(event, parameter):
    return packets.pack_command(packets.RSP_NOTIFY, struct.pack(">IIB", event, parameter, 0))


SYNC_LOST = notify(packets.NOTIFY_AUTOSENSE, packets.AUTOSENSE_STARTED)


def decode_png(data):
    """Return (width, height, rows of RGB bytes) from our own PNG output."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position = 8
    chunks = {}
    while position < len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        crc = struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == crc
        chunks[kind] = body
        position += 12 + length
    width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = 1 + width * 3
    rows = [raw[row * stride + 1:(row + 1) * stride] for row in range(height)]
    return width, height, rows


def test_video_mode_parsing():
    mode = screen.VideoMode.parse(new_video_mode(1024, 768, 1024, 768, cell=16))
    assert (mode.width, mode.height) == (1024, 768)
    assert (mode.cell_width, mode.cell_height) == (16, 16)
    assert mode.cells_per_row == 64
    assert mode.total_cells == 64 * 48
    assert mode.colour_table == screen.CCT_RGB15


def test_video_mode_must_be_65_bytes():
    with pytest.raises(screen.ScreenError):
        screen.VideoMode.parse(bytes(64))


@pytest.mark.parametrize("value, rgb", [
    (0x0000, (0, 0, 0)),
    (WHITE, (255, 255, 255)),
    (RED, (255, 0, 0)),
    (GREEN, (0, 255, 0)),
    (BLUE, (0, 0, 255)),
    (0x4210, (132, 132, 132)),  # 10000 in each channel
])
def test_rgb555(value, rgb):
    assert screen.rgb555_to_rgb(value) == rgb


def test_rgb444():
    assert screen.rgb444_to_rgb(0x0F00) == (255, 0, 0)
    assert screen.rgb444_to_rgb(0x0888) == (136, 136, 136)


def test_unsupported_colour_table():
    mode = screen.VideoMode.parse(new_video_mode(8, 4, 8, 4, colour_table=8))
    with pytest.raises(screen.ScreenError, match="colour table 8"):
        screen.FrameBuffer(mode)


@pytest.mark.parametrize("compressed", [False, True])
def test_frame_from_two_cells(compressed):
    mode = screen.VideoMode.parse(new_video_mode(7, 3, 8, 4))
    frame = screen.FrameBuffer(mode)
    assert not frame.complete

    frame.apply_cell_data(cell_data([cell(0, RED), cell(1, BLUE)], compressed))
    assert frame.complete

    width, height, rows = decode_png(frame.to_png())
    # Cropped to the visible 7x3; left cell red, right cell blue.
    assert (width, height) == (7, 3)
    for row in rows:
        assert row[0:12] == bytes((255, 0, 0)) * 4
        assert row[12:21] == bytes((0, 0, 255)) * 3


def test_cells_can_arrive_in_separate_packets():
    mode = screen.VideoMode.parse(new_video_mode(8, 4, 8, 4))
    frame = screen.FrameBuffer(mode)
    frame.apply_cell_data(cell_data([cell(1, GREEN)]))
    assert frame.cells_seen == {1}
    frame.apply_cell_data(cell_data([cell(0, WHITE)]))
    assert frame.complete


def test_short_cell_data_is_an_error():
    mode = screen.VideoMode.parse(new_video_mode(8, 4, 8, 4))
    frame = screen.FrameBuffer(mode)
    packet = cell_data([cell(0, RED)])
    # Claim two cells while carrying one.
    packet = packet[:4] + struct.pack(">H", 2) + packet[6:]
    with pytest.raises(screen.ScreenError):
        frame.apply_cell_data(packet)


def test_out_of_range_cells_are_ignored():
    mode = screen.VideoMode.parse(new_video_mode(8, 4, 8, 4))
    frame = screen.FrameBuffer(mode)
    frame.apply_cell_data(cell_data([cell(5, RED)]))
    assert frame.cells_seen == set()


class FakeSession:
    def __init__(self) -> None:
        self.refreshes = 0
        self.no_video = False

    def request_refresh(self) -> None:
        self.refreshes += 1


def test_grabber_builds_a_frame_from_video_packets():
    session = FakeSession()
    grabber = screen.ScreenGrabber(session, settle_time=0)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    assert session.refreshes == 1  # a new mode asks for a full repaint
    grabber.handle_video_packet(cell_data([cell(0, RED), cell(1, RED)]))

    width, height, rows = decode_png(grabber.grab(timeout=1))
    assert (width, height) == (8, 4)
    assert rows[0] == bytes((255, 0, 0)) * 8


def test_grabber_reports_no_video():
    session = FakeSession()
    session.no_video = True
    grabber = screen.ScreenGrabber(session)
    with pytest.raises(screen.ScreenError, match="no video signal"):
        grabber.grab(timeout=0.3)


def test_grabber_reports_decoding_errors():
    grabber = screen.ScreenGrabber(FakeSession())
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4, colour_table=8))
    with pytest.raises(screen.ScreenError, match="decoding failed"):
        grabber.grab(timeout=1)


def red_frame():
    return cell_data([cell(0, RED), cell(1, RED)])


def first_row_colour(png):
    _width, _height, rows = decode_png(png)
    return tuple(rows[0][:3])


def test_grabber_keeps_the_first_complete_frame():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=0)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(red_frame())
    # The black screen the switch sends as it loses sync.
    grabber.handle_video_packet(cell_data([cell(0, 0), cell(1, 0)]))

    assert first_row_colour(grabber.grab(timeout=1)) == (255, 0, 0)
    assert (grabber.mode.width, grabber.mode.height) == (8, 4)


def test_losing_sync_discards_an_unsettled_frame():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=0.3)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(cell_data([cell(0, 0), cell(1, 0)]))
    grabber.handle_video_packet(SYNC_LOST)
    assert grabber.candidate is None

    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(cell_data([cell(0, GREEN), cell(1, GREEN)]))
    assert first_row_colour(grabber.grab(timeout=2)) == (0, 255, 0)


def test_a_settled_frame_survives_losing_sync_later():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=0.05)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(red_frame())
    time.sleep(0.1)
    grabber.handle_video_packet(SYNC_LOST)
    assert first_row_colour(grabber.grab(timeout=1)) == (255, 0, 0)


def test_cells_between_losing_sync_and_the_next_mode_are_ignored():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=0)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(SYNC_LOST)
    grabber.handle_video_packet(red_frame())
    assert grabber.frame is None
    assert grabber.candidate is None
    assert grabber.error is None


def test_other_notifications_change_nothing():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=10)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(red_frame())
    grabber.handle_video_packet(notify(packets.NOTIFY_AUTOSENSE, 5))
    grabber.handle_video_packet(notify(packets.NOTIFY_AUTOSENSE, packets.AUTOSENSE_DONE))
    assert grabber.candidate is not None


def test_an_unsettled_frame_is_returned_when_time_runs_out():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=10)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(red_frame())
    assert first_row_colour(grabber.grab(timeout=0.3)) == (255, 0, 0)


def test_an_incomplete_frame_is_not_returned():
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=0)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(cell_data([cell(0, RED)]))
    with pytest.raises(screen.ScreenError, match="may not lock onto this video mode"):
        grabber.grab(timeout=0.3)


def test_a_switch_losing_sync_as_a_real_one_does():
    # What a Dominion KX sent for a screen it struggled to lock onto: the
    # screen, the screen again, a whole screen of black cells, then a
    # restart of its autosense.
    grabber = screen.ScreenGrabber(FakeSession(), settle_time=0)
    grabber.handle_video_packet(new_video_mode(8, 4, 8, 4))
    grabber.handle_video_packet(cell_data([cell(0, WHITE), cell(1, BLUE)]))
    grabber.handle_video_packet(cell_data([cell(0, WHITE), cell(1, BLUE)]))
    grabber.handle_video_packet(cell_data([cell(0, 0), cell(1, 0)]))
    grabber.handle_video_packet(SYNC_LOST)

    _width, _height, rows = decode_png(grabber.grab(timeout=1))
    assert rows[0][:3] == bytes((255, 255, 255))
    assert rows[0][-3:] == bytes((0, 0, 255))
