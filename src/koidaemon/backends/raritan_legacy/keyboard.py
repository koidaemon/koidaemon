# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Turn key presses and text into keyboard packets.

The switch accepts key events in one of two encodings, chosen by the
switch (see ServerId.wants_hid_keycodes):

PS/2
    Scan code set 2 make and break bytes, exactly as a PS/2 keyboard would
    send them.  A first-generation Dominion KX uses this, even for USB CIMs.

HID
    Two-byte commands: 01 <usage> to press a key, 02 <usage> to release it.

Raritan's client sends one packet per key event, and so do we.
"""

from __future__ import annotations

import time

from koidaemon import keys
from koidaemon.backends.raritan_legacy.scancodes import SET2_BREAK, SET2_MAKE

HID_PRESS = 0x01
HID_RELEASE = 0x02

# Pause between key events.  The target keyboard is a real USB (or PS/2)
# device behind the switch, so do not flood it.
DEFAULT_KEY_DELAY = 0.03

# The CIM on each port opens its setup menu on Ctrl+NumLock and then eats
# all key presses until Escape.  Refuse to send that by accident.
CIM_MENU_KEYS = {keys.LEFT_CTRL, keys.RIGHT_CTRL}
NUMLOCK = keys.NAMED_KEYS["numlock"]


class KeyboardError(Exception):
    """A key cannot be sent."""


def encode_press(usage: int, hid_mode: bool) -> bytes:
    if hid_mode:
        return bytes([HID_PRESS, usage])
    if usage not in SET2_MAKE:
        raise KeyboardError("key 0x%02x has no PS/2 scan code" % usage)
    return SET2_MAKE[usage]


def encode_release(usage: int, hid_mode: bool) -> bytes:
    if hid_mode:
        return bytes([HID_RELEASE, usage])
    if usage not in SET2_BREAK:
        raise KeyboardError("key 0x%02x has no PS/2 break code" % usage)
    return SET2_BREAK[usage]


def combination_events(usages: list[int]) -> list[tuple[str, int]]:
    """Press every key in order, then release them in reverse order."""
    if NUMLOCK in usages and CIM_MENU_KEYS.intersection(usages):
        raise KeyboardError("Ctrl+NumLock opens the CIM setup menu; refusing to send it")
    events = [("press", usage) for usage in usages]
    events += [("release", usage) for usage in reversed(usages)]
    return events


def text_events(text: str) -> list[tuple[str, int]]:
    """Key events that type text on a US layout."""
    events = []
    for character in text:
        if character not in keys.CHARACTERS:
            raise KeyboardError("cannot type %r on a US keyboard" % character)
        usage, needs_shift = keys.CHARACTERS[character]
        if needs_shift:
            events += combination_events([keys.LEFT_SHIFT, usage])
        else:
            events += combination_events([usage])
    return events


class Keyboard:
    """Sends key events through a KXSession connected to a port."""

    def __init__(self, session, delay: float = DEFAULT_KEY_DELAY) -> None:
        self.session = session
        self.delay = delay

    @property
    def hid_mode(self) -> bool:
        return self.session.server.wants_hid_keycodes

    def send_events(self, events: list[tuple[str, int]]) -> None:
        for action, usage in events:
            if action == "press":
                key_bytes = encode_press(usage, self.hid_mode)
            else:
                key_bytes = encode_release(usage, self.hid_mode)
            self.session.send_key_bytes(key_bytes)
            if self.delay:
                time.sleep(self.delay)

    def type_text(self, text: str) -> None:
        """Type text, e.g. "root\\n"."""
        self.send_events(text_events(text))

    def press(self, combination: str) -> None:
        """Press and release a key or combination, e.g. "F2" or "ctrl+alt+delete"."""
        self.send_events(combination_events(keys.parse_combination(combination)))

    def key_down(self, name: str) -> None:
        self.send_events([("press", keys.usage_for_name(name))])

    def key_up(self, name: str) -> None:
        self.send_events([("release", keys.usage_for_name(name))])
