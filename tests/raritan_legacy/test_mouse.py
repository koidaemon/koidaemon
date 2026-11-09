# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pytest

from koidaemon.backends.raritan_legacy import session as kx
from koidaemon.backends.raritan_legacy.mouse import Mouse

FLUSH = (kx.MOUSE_WHEEL, 0, 0)


class RecordingSession:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, int]] = []

    def send_mouse(self, function: int, x: int = 0, y: int = 0) -> None:
        self.sent.append((function, x, y))


@pytest.fixture
def recorded():
    session = RecordingSession()
    return session, Mouse(session, delay=0)


def test_move_is_followed_by_a_flush(recorded):
    session, mouse = recorded
    mouse.move_to(640, 400)
    assert session.sent == [(kx.MOUSE_MOVE, 640, 400), FLUSH]
    assert mouse.position == (640, 400)


def test_negative_positions_are_refused(recorded):
    _session, mouse = recorded
    with pytest.raises(kx.SessionError):
        mouse.move_to(-1, 5)


def test_click_uses_the_last_position(recorded):
    session, mouse = recorded
    mouse.move_to(10, 20)
    session.sent.clear()
    mouse.click("right")
    assert session.sent == [(kx.MOUSE_RIGHT_DOWN, 10, 20), (kx.MOUSE_RIGHT_UP, 10, 20), FLUSH]


def test_double_click_flushes_once_at_the_end(recorded):
    session, mouse = recorded
    mouse.click("left", count=2)
    assert session.sent == [
        (kx.MOUSE_LEFT_DOWN, 0, 0), (kx.MOUSE_LEFT_UP, 0, 0),
        (kx.MOUSE_LEFT_DOWN, 0, 0), (kx.MOUSE_LEFT_UP, 0, 0),
        FLUSH,
    ]


def test_scroll_up_sends_a_negative_value_wrapped_in_flushes(recorded):
    # The switch inverts wheel values and holds each one back until the
    # next wheel event, as seen on real hardware.
    session, mouse = recorded
    mouse.scroll(3)
    assert session.sent == [FLUSH, (kx.MOUSE_WHEEL, -3, 0), FLUSH]


def test_scroll_zero_sends_nothing(recorded):
    session, mouse = recorded
    mouse.scroll(0)
    assert session.sent == []


def test_button_down_and_up(recorded):
    session, mouse = recorded
    mouse.button_down("middle")
    mouse.button_up("middle")
    assert session.sent == [(kx.MOUSE_MIDDLE_DOWN, 0, 0), FLUSH, (kx.MOUSE_MIDDLE_UP, 0, 0), FLUSH]


def test_unknown_button(recorded):
    _session, mouse = recorded
    with pytest.raises(kx.SessionError):
        mouse.click("fourth")
