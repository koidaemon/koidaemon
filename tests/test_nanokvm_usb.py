# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import os
import sys

import pytest

from koidaemon import keys
from koidaemon.backends import nanokvm_usb
from koidaemon.backends.base import Settings
from koidaemon.errors import BackendError, ConfigError, InvalidRequest

from fake_ch9329 import FakeCH9329
from fake_kvmd import make_jpeg

SHIFT = 0x02
CTRL = 0x01
ALT = 0x04
NO_KEYS = bytes(8)


def make_backend(tmp_path, serial, **settings) -> nanokvm_usb.NanoKVMUSBBackend:
    values = {"serial": serial, "video": str(tmp_path / "video0"), **settings}
    return nanokvm_usb.NanoKVMUSBBackend("board", Settings("board", values, tmp_path))


def report(modifiers: int = 0, *usages: int) -> bytes:
    return bytes([modifiers, 0, *usages] + [0] * (6 - len(usages)))


def mouse(buttons: int, x: int, y: int, wheel: int = 0) -> bytes:
    return (bytes([0x02, buttons]) + x.to_bytes(2, "little") + y.to_bytes(2, "little")
            + bytes([wheel & 0xFF]))


@pytest.fixture
def chip():
    with FakeCH9329() as fake:
        yield fake


@pytest.fixture
def backend(tmp_path, chip):
    backend = make_backend(tmp_path, chip.path, timeout=1, key_interval=0)
    yield backend
    backend.close()


# ----------------------------------------------------------------------
# Frames
# ----------------------------------------------------------------------

def test_encode_frame():
    # GET_INFO, as Sipeed's client sends it.
    assert nanokvm_usb.encode_frame(0x01) == bytes([0x57, 0xAB, 0x00, 0x01, 0x00, 0x03])


def test_decode_frame_skips_noise():
    frame = nanokvm_usb.encode_frame(0x82, b"\x00")
    assert nanokvm_usb.decode_frame(b"\x00\x11" + frame + b"\x57") == (0x82, b"\x00", 2 + len(frame))


def test_decode_frame_waits_for_the_whole_frame():
    frame = nanokvm_usb.encode_frame(0x81, bytes(8))
    assert nanokvm_usb.decode_frame(frame[:-1]) is None
    assert nanokvm_usb.decode_frame(b"\x57") is None


def test_decode_frame_with_a_bad_checksum():
    frame = bytearray(nanokvm_usb.encode_frame(0x82, b"\x00"))
    frame[-1] ^= 0xFF
    with pytest.raises(BackendError, match="bad checksum"):
        nanokvm_usb.decode_frame(bytes(frame))


@pytest.mark.parametrize("position, size, expected", [
    (0, 1920, 0), (1919, 1920, 4095), (-5, 1920, 0), (5000, 1920, 4095), (0, 1, 0),
])
def test_to_mouse_range(position, size, expected):
    assert nanokvm_usb.to_mouse_range(position, size) == expected


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def test_settings(tmp_path):
    backend = make_backend(tmp_path, "/dev/ttyACM2", width=1280, height=720)
    assert backend.resource == "nanokvm-usb:/dev/ttyACM2"
    assert (backend.width, backend.height) == (1280, 720)
    assert backend.describe()["serial"] == "/dev/ttyACM2"
    assert "storage.stick" not in backend.describe()["capabilities"]


def test_key_interval(tmp_path):
    assert make_backend(tmp_path, "/dev/ttyACM2").key_interval == nanokvm_usb.KEY_INTERVAL
    assert make_backend(tmp_path, "/dev/ttyACM2", key_interval=0.2).key_interval == 0.2
    with pytest.raises(ConfigError, match="cannot be negative"):
        make_backend(tmp_path, "/dev/ttyACM2", key_interval=-1)


def test_keys_are_spaced_out(tmp_path, chip, monkeypatch):
    pauses = []
    monkeypatch.setattr(nanokvm_usb.time, "sleep", pauses.append)
    backend = make_backend(tmp_path, chip.path, timeout=1, key_interval=0.07)
    try:
        backend.press([[A]])
    finally:
        backend.close()
    assert pauses == [0.07, 0.07]


def test_bad_size(tmp_path):
    with pytest.raises(ConfigError, match="must be positive"):
        make_backend(tmp_path, "/dev/ttyACM2", width=0)


def test_unknown_setting(tmp_path):
    with pytest.raises(ConfigError, match="unknown settings colour"):
        make_backend(tmp_path, "/dev/ttyACM2", colour="red")


def test_missing_serial_port(tmp_path):
    backend = make_backend(tmp_path, str(tmp_path / "nothing"))
    with pytest.raises(BackendError, match="cannot open"):
        backend.status()


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------

def test_status(chip, backend):
    assert backend.status() == {
        "online": True,
        "chip_version": "V1.8",
        "usb_connected": True,
        "num_lock": True,
        "caps_lock": False,
        "scroll_lock": False,
        "video": {"width": 1920, "height": 1080},
    }
    assert chip.frames == [(nanokvm_usb.CMD_GET_INFO, b"")]


def test_no_answer(chip, backend):
    chip.status = None
    backend.timeout = 0.2
    with pytest.raises(BackendError, match="did not answer"):
        backend.status()


def test_refused(chip, backend):
    chip.status = 0xE5
    with pytest.raises(BackendError, match="a parameter was wrong"):
        backend.key_down(keys.usage_for_name("a"))


def test_port_is_kept_open(chip, backend):
    backend.status()
    fd = backend._fd
    backend.status()
    assert backend._fd == fd
    backend.close()
    assert backend._fd is None


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------

A = keys.usage_for_name("a")
B = keys.usage_for_name("b")
ONE = keys.usage_for_name("1")
ENTER = keys.usage_for_name("enter")
DELETE = keys.usage_for_name("delete")


def test_type_text(chip, backend):
    backend.type_text("aA!\n")
    assert chip.keyboard_reports() == [
        report(0, A), NO_KEYS,
        report(SHIFT, A), NO_KEYS,
        report(SHIFT, ONE), NO_KEYS,
        report(0, ENTER), NO_KEYS,
    ]


def test_type_text_it_cannot(chip, backend):
    with pytest.raises(InvalidRequest, match="cannot type"):
        backend.type_text("aé")
    assert chip.frames == []


def test_type_text_keeps_held_keys(chip, backend):
    backend.key_down(keys.LEFT_CTRL)
    backend.type_text("a")
    assert chip.keyboard_reports()[1:] == [report(CTRL, A), report(CTRL)]


def test_press(chip, backend):
    backend.press([[keys.LEFT_CTRL, keys.LEFT_ALT, DELETE], [ENTER]])
    assert chip.keyboard_reports() == [
        report(CTRL), report(CTRL | ALT), report(CTRL | ALT, DELETE),
        report(CTRL | ALT), report(CTRL), NO_KEYS,
        report(0, ENTER), NO_KEYS,
    ]


def test_down_and_up(chip, backend):
    backend.key_down(A)
    backend.key_down(keys.LEFT_SHIFT)
    backend.key_down(B)
    backend.key_up(A)
    backend.key_up(keys.LEFT_SHIFT)
    backend.key_up(B)
    assert chip.keyboard_reports() == [
        report(0, A), report(SHIFT, A), report(SHIFT, A, B),
        report(SHIFT, B), report(0, B), NO_KEYS,
    ]


def test_at_most_six_keys(chip, backend):
    for name in "abcdef":
        backend.key_down(keys.usage_for_name(name))
    with pytest.raises(InvalidRequest, match="at most 6 keys"):
        backend.key_down(keys.usage_for_name("g"))
    # Modifiers still fit.
    backend.key_down(keys.LEFT_SHIFT)


# ----------------------------------------------------------------------
# Mouse
# ----------------------------------------------------------------------

def test_move(chip, backend):
    backend.mouse_move(1919, 0)
    backend.mouse_move(960, 540)
    assert chip.mouse_reports() == [mouse(0, 4095, 0), mouse(0, 2049, 2049)]


def test_click(chip, backend):
    backend.mouse_click("left", 2, 0, 1079)
    assert chip.mouse_reports() == [
        mouse(0, 0, 4095),
        mouse(1, 0, 4095), mouse(0, 0, 4095),
        mouse(1, 0, 4095), mouse(0, 0, 4095),
    ]


def test_click_where_the_pointer_is(chip, backend):
    backend.mouse_move(10, 10)
    backend.mouse_click("right", 1, None, None)
    assert chip.mouse_reports()[1:] == [mouse(2, 21, 38), mouse(0, 21, 38)]


def test_down_and_up_buttons(chip, backend):
    backend.mouse_down("left")
    backend.mouse_down("middle")
    backend.mouse_move(0, 0)
    backend.mouse_up("left")
    backend.mouse_up("middle")
    assert chip.mouse_reports() == [
        mouse(1, 0, 0), mouse(5, 0, 0), mouse(5, 0, 0), mouse(4, 0, 0), mouse(0, 0, 0),
    ]


def test_scroll(chip, backend):
    backend.mouse_scroll(3)
    backend.mouse_scroll(-200)
    assert chip.mouse_reports() == [mouse(0, 0, 0, 3), mouse(0, 0, 0, -127),
                                    mouse(0, 0, 0, -73)]


# ----------------------------------------------------------------------
# Snapshot
# ----------------------------------------------------------------------

FAKE_GST = """\
import os, sys
arguments = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
if os.environ.get("FAKE_GST_FAIL"):
    print("ERROR: Could not open device")
    sys.exit(1)
caps = [a for a in sys.argv if a.startswith("image/jpeg")][0]
width = int(caps.split("width=")[1].split(",")[0])
height = int(caps.split("height=")[1])
sys.path.insert(0, %r)
from fake_kvmd import make_jpeg
for frame in range(int(arguments["num-buffers"])):
    with open(arguments["location"] %% frame, "wb") as output:
        output.write(make_jpeg(width + frame, height))
"""


@pytest.fixture
def gst(tmp_path):
    path = tmp_path / "gst-launch"
    path.write_text("#!%s\n%s" % (sys.executable, FAKE_GST % os.path.dirname(__file__)))
    path.chmod(0o755)
    (tmp_path / "video0").write_bytes(b"")
    return path


def test_snapshot(tmp_path, gst):
    backend = make_backend(tmp_path, "/dev/null", gst_launch=str(gst), width=800, height=600)
    snapshot = backend.snapshot()
    assert snapshot.format == "jpeg"
    # The last frame is kept.
    last = nanokvm_usb.SNAPSHOT_FRAMES - 1
    assert snapshot.data == make_jpeg(800 + last, 600)
    assert (snapshot.width, snapshot.height) == (800 + last, 600)


def test_snapshot_fails(tmp_path, gst, monkeypatch):
    monkeypatch.setenv("FAKE_GST_FAIL", "1")
    backend = make_backend(tmp_path, "/dev/null", gst_launch=str(gst))
    with pytest.raises(BackendError, match="Could not open device"):
        backend.snapshot()


def test_snapshot_without_gstreamer(tmp_path, gst):
    backend = make_backend(tmp_path, "/dev/null", gst_launch=str(tmp_path / "missing"))
    with pytest.raises(BackendError, match="is GStreamer installed"):
        backend.snapshot()


def test_snapshot_without_the_device(tmp_path):
    backend = make_backend(tmp_path, "/dev/null")
    with pytest.raises(BackendError, match="does not exist"):
        backend.snapshot()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open anything")
def test_snapshot_without_permission(tmp_path, gst):
    (tmp_path / "video0").chmod(0)
    backend = make_backend(tmp_path, "/dev/null", gst_launch=str(gst))
    with pytest.raises(BackendError, match="permission denied"):
        backend.snapshot()
