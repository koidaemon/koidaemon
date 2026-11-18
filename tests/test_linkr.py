# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import ssl

import pytest

from koidaemon import keys
from koidaemon.backends import linkr
from koidaemon.backends.base import Settings
from koidaemon.errors import BackendError, ConfigError, InvalidRequest

from fake_kvmd import make_jpeg
from fake_linkr import FakeLinkr


def make_backend(tmp_path, url, token="radxa_linkr_secret", **settings) -> linkr.LinkrBackend:
    (tmp_path / "token").write_text(token + "\n")
    values = {"url": url, "token_file": "token", **settings}
    return linkr.LinkrBackend("board", Settings("board", values, tmp_path))


@pytest.fixture
def device():
    with FakeLinkr() as fake:
        yield fake


@pytest.fixture
def backend(tmp_path, device):
    return make_backend(tmp_path, device.url)


# ----------------------------------------------------------------------
# Settings and connection
# ----------------------------------------------------------------------

def test_settings(tmp_path):
    backend = make_backend(tmp_path, "https://linkr.lab/", timeout=5)
    assert backend.url == "https://linkr.lab"
    assert backend.token == "radxa_linkr_secret"
    assert backend.resource == "linkr:https://linkr.lab"
    assert backend.describe()["url"] == "https://linkr.lab"
    assert "storage.cdrom" not in backend.describe()["capabilities"]
    assert backend._tls.verify_mode == ssl.CERT_REQUIRED


def test_bad_url(tmp_path):
    with pytest.raises(ConfigError, match="must start with https://"):
        make_backend(tmp_path, "linkr.lab")


def test_tls_check_can_be_turned_off(tmp_path):
    backend = make_backend(tmp_path, "https://linkr.lab", verify_tls=False)
    assert backend._tls.verify_mode == ssl.CERT_NONE


def test_unknown_setting(tmp_path):
    with pytest.raises(ConfigError, match="unknown settings colour"):
        make_backend(tmp_path, "https://linkr.lab", colour="red")


def test_wrong_token(tmp_path, device):
    backend = make_backend(tmp_path, device.url, token="radxa_linkr_wrong")
    with pytest.raises(BackendError, match="refused the access token"):
        backend.status()


def test_unreachable(tmp_path):
    backend = make_backend(tmp_path, "http://127.0.0.1:1")
    with pytest.raises(BackendError, match="cannot reach"):
        backend.status()


def test_command_fails(device, backend):
    device.control_answer = {"code": 1, "data": None, "message": "HID not ready"}
    with pytest.raises(BackendError, match="HID not ready"):
        backend.key_down(keys.usage_for_name("a"))


# ----------------------------------------------------------------------
# Status and snapshot
# ----------------------------------------------------------------------

def test_status(device, backend):
    assert backend.status() == {"online": True, "video": None}
    assert device.controls == [[]]
    assert device.snapshots == 0
    backend.snapshot()
    assert backend.status()["video"] == {"width": 1920, "height": 1080}


def test_snapshot(device, backend):
    device.screen = (1280, 720)
    snapshot = backend.snapshot()
    assert snapshot.format == "jpeg"
    assert snapshot.data == make_jpeg(1280, 720)
    assert (snapshot.width, snapshot.height) == (1280, 720)
    assert device.headers[0]["Authorization"] == "token radxa_linkr_secret"


def test_snapshot_that_is_not_a_jpeg(device, backend):
    device.snapshot_data = b"\x89PNG"
    with pytest.raises(BackendError, match="not a JPEG"):
        backend.snapshot()


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------

def test_type_text(device, backend):
    backend.type_text("root\n")
    assert device.controls == [[["text", "root\n"]]]


def test_type_long_text(device, backend):
    text = "x" * 65
    backend.type_text(text)
    assert device.controls == [[
        ["text", "x" * 30], ["delay", 1000], ["text", "x" * 30], ["delay", 1000],
        ["text", "x" * 5],
    ]]


def test_type_very_long_text_in_several_requests(device, backend):
    backend.type_text("y" * 300)
    assert len(device.controls) == 3
    assert device.controls[1] == [["delay", 1000]]
    typed = "".join(event[1] for events in device.controls for event in events
                    if event[0] == "text")
    assert typed == "y" * 300


def test_type_text_it_cannot(device, backend):
    with pytest.raises(InvalidRequest, match="cannot type"):
        backend.type_text("café")
    assert device.controls == []


def test_press(device, backend):
    backend.press([keys.parse_combination("ctrl+alt+delete"), [keys.usage_for_name("enter")]])
    assert device.controls == [[
        ["keyboard", "ControlLeft", True], ["keyboard", "AltLeft", True],
        ["keyboard", "Delete", True], ["keyboard", "Delete", False],
        ["keyboard", "AltLeft", False], ["keyboard", "ControlLeft", False],
        ["keyboard", "Enter", True], ["keyboard", "Enter", False],
    ]]


def test_down_and_up(device, backend):
    backend.key_down(keys.usage_for_name("rshift"))
    backend.key_up(keys.usage_for_name("rshift"))
    assert device.controls == [[["keyboard", "ShiftRight", True]],
                               [["keyboard", "ShiftRight", False]]]


# ----------------------------------------------------------------------
# Mouse
# ----------------------------------------------------------------------

def test_move_takes_a_snapshot_for_the_screen_size(device, backend):
    device.screen = (1001, 501)
    backend.mouse_move(500, 500)
    backend.mouse_move(2000, -3)
    assert device.snapshots == 1
    assert device.controls == [[["mouse_abs", 0, 0.5, 1.0, 0, 0]],
                               [["mouse_abs", 0, 1.0, 0.0, 0, 0]]]


def test_click(device, backend):
    backend.snapshot()
    backend.mouse_click("left", 2, 0, 1079)
    assert device.controls == [
        [["mouse_abs", 0, 0.0, 1.0, 0, 0]],
        [["mouse_abs", 1, 0.0, 1.0, 0, 0], ["mouse_abs", 0, 0.0, 1.0, 0, 0]],
        [["mouse_abs", 1, 0.0, 1.0, 0, 0], ["mouse_abs", 0, 0.0, 1.0, 0, 0]],
    ]


def test_click_where_the_pointer_is_needs_no_snapshot(device, backend):
    backend.mouse_click("right", 1, None, None)
    assert device.snapshots == 0
    assert device.controls == [[["mouse_abs", 2, 0.0, 0.0, 0, 0],
                                ["mouse_abs", 0, 0.0, 0.0, 0, 0]]]


def test_down_and_up_buttons(device, backend):
    backend.mouse_down("left")
    backend.mouse_down("middle")
    backend.mouse_up("left")
    backend.mouse_up("middle")
    assert [events[0][1] for events in device.controls] == [1, 5, 4, 0]


def test_scroll(device, backend):
    backend.mouse_scroll(-45)
    assert device.controls == [[
        ["mouse_abs", 0, 0.0, 0.0, -20, 0], ["mouse_abs", 0, 0.0, 0.0, -20, 0],
        ["mouse_abs", 0, 0.0, 0.0, -5, 0],
    ]]
