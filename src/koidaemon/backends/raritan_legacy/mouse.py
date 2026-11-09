# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Mouse control through a KXSession connected to a port.

Positions are absolute screen pixels.  The switch tracks where it last put
the pointer and turns each new position into relative USB/PS/2 mouse
movement for the target.  When a port is connected the switch first drives
the pointer into the top-left corner, so (0, 0) is the top-left of the
screen.

Behaviour of a real Dominion KX worth knowing (seen on firmware 1.4.9.5.16
with a DCIM-USBG2):

  * a wheel event is only passed on to the target when the next wheel
    event arrives, and the first one of a session can be lost, so every
    scroll is wrapped in zero-length ones;
  * wheel values are inverted on the way through, so to scroll up by N
    notches the switch must be sent -N;
  * moving to the position the pointer is already at sends nothing;
  * more generally the switch can hold back the latest mouse event until
    another one arrives (a button release went missing this way, leaving
    the button held down), so every action here ends with an empty wheel
    event, which is harmless on the target.
"""

from __future__ import annotations

import time

from koidaemon.backends.raritan_legacy.session import (MOUSE_BUTTONS, MOUSE_MOVE, MOUSE_WHEEL,
                                SessionError)

DEFAULT_CLICK_DELAY = 0.05


class Mouse:
    def __init__(self, session, delay: float = DEFAULT_CLICK_DELAY) -> None:
        self.session = session
        self.delay = delay
        self.position: tuple[int, int] | None = None

    def flush(self) -> None:
        """Push any held-back mouse event through to the target."""
        time.sleep(self.delay)
        self.session.send_mouse(MOUSE_WHEEL, 0, 0)

    def move_to(self, x: int, y: int) -> None:
        """Move the pointer to screen position (x, y)."""
        if x < 0 or y < 0:
            raise SessionError("screen positions cannot be negative")
        self.session.send_mouse(MOUSE_MOVE, x, y)
        self.position = (x, y)
        self.flush()

    def _button_functions(self, button: str) -> tuple[int, int]:
        if button not in MOUSE_BUTTONS:
            raise SessionError("unknown mouse button %r (use left, right or middle)" % button)
        return MOUSE_BUTTONS[button]

    def _where(self) -> tuple[int, int]:
        # Button events carry a position too.  Use the last one we moved
        # to; the switch ignores it when it matches where the pointer is.
        return self.position if self.position is not None else (0, 0)

    def _send_button(self, function: int) -> None:
        x, y = self._where()
        self.session.send_mouse(function, x, y)

    def button_down(self, button: str = "left") -> None:
        down, _up = self._button_functions(button)
        self._send_button(down)
        self.flush()

    def button_up(self, button: str = "left") -> None:
        _down, up = self._button_functions(button)
        self._send_button(up)
        self.flush()

    def click(self, button: str = "left", count: int = 1) -> None:
        """Click a button count times (2 for a double click)."""
        down, up = self._button_functions(button)
        for _ in range(count):
            self._send_button(down)
            time.sleep(self.delay)
            self._send_button(up)
            time.sleep(self.delay)
        self.flush()

    def scroll(self, notches: int) -> None:
        """Scroll the wheel: positive is up (away from the user), negative down."""
        if notches == 0:
            return
        # The first wheel event of a session can be swallowed, so lead with
        # an empty one.  It does no harm when it is not the first.
        self.session.send_mouse(MOUSE_WHEEL, 0, 0)
        time.sleep(self.delay)
        self.session.send_mouse(MOUSE_WHEEL, -notches, 0)
        self.flush()
