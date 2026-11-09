# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""The raritan-legacy backend, end to end against the fake switch."""

import struct

import pytest

from koidaemon import commands, keys
from koidaemon.backends import raritan_legacy
from koidaemon.backends.base import Settings
from koidaemon.backends.raritan_legacy import screen
from koidaemon.backends.raritan_legacy import session as kx
from koidaemon.errors import BackendError, Busy, ConfigError, InvalidRequest, Unsupported

from fake_kx import FakeKX


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    # The real delays exist for the hardware; the fake does not need them.
    monkeypatch.setattr(kx, "ENCRYPTION_SWITCH_DELAY", 0.05)
    monkeypatch.setattr(kx, "INPUT_DRAIN_TIME", 0.0)
    monkeypatch.setattr(kx, "BUSY_RETRY_INTERVAL", 0.05)


def make_backend(tmp_path, tcp_port=5000, password="secret", **settings):
    (tmp_path / "password").write_text(password + "\n")
    values = {"host": "127.0.0.1", "port": 1, "password_file": "password",
              "tcp_port": tcp_port, "key_delay": 0.0, "wait": 0.0, **settings}
    return raritan_legacy.RaritanLegacyBackend("board", Settings("board", values, tmp_path))


@pytest.fixture
def fake():
    with FakeKX() as switch:
        yield switch


@pytest.fixture
def backend(tmp_path, fake):
    return make_backend(tmp_path, fake.port)


@pytest.fixture
def offline_backend(tmp_path):
    """A backend whose switch is unreachable: anything that tries to
    connect fails with BackendError instead of what the test expects."""
    return make_backend(tmp_path, tcp_port=1)


def finished(fake):
    """Wait for the fake switch's session to end, and surface its errors."""
    assert fake.logged_out.wait(10), "the session never logged out"
    if fake.error is not None:
        raise fake.error


def key_bytes(fake):
    return [packet[5:].hex() for packet in fake.keyboard_packets]


def mouse_events(fake):
    events = []
    for packet in fake.mouse_packets:
        _device, function, x, y = struct.unpack(">BBhh", packet[4:10])
        events.append((function, x, y))
    return events


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def test_settings(tmp_path):
    backend = make_backend(tmp_path, port=4, user="operator")
    assert (backend.host, backend.port, backend.user) == ("127.0.0.1", 4, "operator")
    assert backend.password == "secret"
    assert backend.describe() == {
        "backend": "raritan-legacy",
        "capabilities": ["keyboard", "mouse", "snapshot"],
        "host": "127.0.0.1",
        "port": 4,
    }


def test_defaults(tmp_path):
    (tmp_path / "password").write_text("secret")
    values = {"host": "192.0.2.10", "port": 2, "password_file": "password"}
    backend = raritan_legacy.RaritanLegacyBackend("board", Settings("board", values, tmp_path))
    assert backend.user == "admin"
    assert backend.tcp_port == kx.DEFAULT_PORT
    assert backend.wait == raritan_legacy.DEFAULT_WAIT


def test_ports_of_one_switch_share_a_resource(tmp_path):
    port_1 = make_backend(tmp_path, port=1)
    port_2 = make_backend(tmp_path, port=2)
    assert port_1.resource == port_2.resource == "raritan-legacy:127.0.0.1:5000"


def test_unknown_setting(tmp_path):
    with pytest.raises(ConfigError, match="unknown settings speed"):
        make_backend(tmp_path, speed=9600)


def test_no_usb_storage(offline_backend):
    assert commands.STORAGE_STICK not in offline_backend.capabilities
    with pytest.raises(Unsupported, match="cannot emulate USB storage"):
        offline_backend.storage_status()


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def test_status(fake, backend):
    assert backend.status() == {"online": True, "port_name": "Board A", "video": None}
    finished(fake)


def test_status_of_a_missing_port(tmp_path, fake):
    backend = make_backend(tmp_path, fake.port, port=9)
    with pytest.raises(BackendError, match="has no port 9"):
        backend.status()
    finished(fake)


def test_type_text(fake, backend):
    backend.type_text("Hi\n")
    finished(fake)
    # Shift+H, I, Enter as PS/2 make and break codes.
    assert key_bytes(fake) == ["12", "33", "f033", "f012", "43", "f043", "5a", "f05a"]
    assert fake.connect_xml.endswith("<Target>//*[@id=P_TEST_0]</Target></Connect>")


def test_untypeable_text_is_refused_before_connecting(offline_backend):
    with pytest.raises(InvalidRequest, match="cannot type"):
        offline_backend.type_text("café")


def test_press(fake, backend):
    backend.press([[keys.LEFT_CTRL, 0x06], [0x3B]])
    finished(fake)
    assert key_bytes(fake) == ["14", "21", "f021", "f014", "06", "f006"]


def test_ctrl_numlock_is_refused_before_connecting(offline_backend):
    with pytest.raises(InvalidRequest, match="CIM setup menu"):
        offline_backend.press([[keys.LEFT_CTRL, keys.NAMED_KEYS["numlock"]]])


def test_key_down_and_up(tmp_path):
    with FakeKX() as fake:
        make_backend(tmp_path, fake.port).key_down(keys.LEFT_SHIFT)
        finished(fake)
    assert key_bytes(fake) == ["12"]

    with FakeKX() as fake:
        make_backend(tmp_path, fake.port).key_up(keys.LEFT_SHIFT)
        finished(fake)
    assert key_bytes(fake) == ["f012"]


def test_mouse_move(fake, backend):
    backend.mouse_move(300, 200)
    finished(fake)
    assert mouse_events(fake) == [(kx.MOUSE_MOVE, 300, 200), (kx.MOUSE_WHEEL, 0, 0)]


def test_mouse_click_at_a_position(fake, backend):
    backend.mouse_click("right", 2, 5, 6)
    finished(fake)
    down, up = kx.MOUSE_BUTTONS["right"]
    assert mouse_events(fake) == [
        (kx.MOUSE_MOVE, 5, 6), (kx.MOUSE_WHEEL, 0, 0),
        (down, 5, 6), (up, 5, 6), (down, 5, 6), (up, 5, 6),
        (kx.MOUSE_WHEEL, 0, 0),
    ]


def test_mouse_down_and_up(tmp_path):
    down, up = kx.MOUSE_BUTTONS["left"]
    with FakeKX() as fake:
        make_backend(tmp_path, fake.port).mouse_down("left")
        finished(fake)
    assert mouse_events(fake)[0][0] == down

    with FakeKX() as fake:
        make_backend(tmp_path, fake.port).mouse_up("left")
        finished(fake)
    assert mouse_events(fake)[0][0] == up


def test_scroll(fake, backend):
    backend.mouse_scroll(2)
    finished(fake)
    # The switch inverts the wheel, so up is sent negative.
    assert mouse_events(fake) == [(kx.MOUSE_WHEEL, 0, 0), (kx.MOUSE_WHEEL, -2, 0),
                                  (kx.MOUSE_WHEEL, 0, 0)]


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------

class FakeGrabber:
    """Stands in for the screen grabber: the fake switch sends no video."""

    def __init__(self, session):
        self.session = session
        self.frame = None

    def handle_video_packet(self, packet):
        pass

    def grab(self, timeout):
        assert self.session.video_listener == self.handle_video_packet
        mode = screen.VideoMode(flags=0, colour_table=12, cell_width=16, cell_height=16,
                                cell_count=0, width=800, height=600, buffer_width=800,
                                buffer_height=608, device_id=7)
        self.frame = screen.FrameBuffer(mode)
        return b"\x89PNG grabbed"


def test_snapshot(fake, backend, monkeypatch):
    monkeypatch.setattr(raritan_legacy, "ScreenGrabber", FakeGrabber)
    snapshot = backend.snapshot()
    finished(fake)
    assert (snapshot.format, snapshot.data) == ("png", b"\x89PNG grabbed")
    assert (snapshot.width, snapshot.height) == (800, 600)
    assert fake.connect_params == screen.SCREENSHOT_COMPRESSION_PARAMS


def test_snapshot_without_video(fake, backend, monkeypatch):
    monkeypatch.setattr(raritan_legacy, "SNAPSHOT_TIMEOUT", 0.5)
    with pytest.raises(BackendError, match="no complete frame|no video signal"):
        backend.snapshot()
    finished(fake)


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------

def test_busy_switch(fake, backend):
    fake.busy_connects = 1
    with pytest.raises(Busy, match="video channel is in use"):
        backend.type_text("x")
    finished(fake)


def test_waiting_for_a_busy_switch(tmp_path, fake):
    fake.busy_connects = 2
    make_backend(tmp_path, fake.port, wait=5.0).type_text("x")
    finished(fake)
    assert fake.connect_attempts == 3


def test_wrong_password(tmp_path, fake):
    backend = make_backend(tmp_path, fake.port, password="wrong")
    with pytest.raises(BackendError, match="wrong user name or password"):
        backend.type_text("x")


def test_unreachable_switch(tmp_path):
    backend = make_backend(tmp_path, tcp_port=1)
    with pytest.raises(BackendError, match="127.0.0.1"):
        backend.type_text("x")
