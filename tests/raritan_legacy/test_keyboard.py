# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pytest

from koidaemon import keys
from koidaemon.backends.raritan_legacy import keyboard
from koidaemon.backends.raritan_legacy.packets import ServerId

SHIFT = keys.LEFT_SHIFT


class RecordingSession:
    """Stands in for KXSession and records the key bytes sent."""

    def __init__(self, hid_mode: bool = False) -> None:
        self.server = ServerId("test", "0.0.0.0", 5000, 30, 20, 0x1401,
                               0x4 if hid_mode else 0x2)
        self.sent: list[bytes] = []

    def send_key_bytes(self, key_bytes: bytes) -> None:
        self.sent.append(key_bytes)


# Scan codes captured from the official client typing a, b, c, Enter, F2.
@pytest.mark.parametrize("name, make", [
    ("a", "1c"), ("b", "32"), ("c", "21"), ("Enter", "5a"), ("F2", "06"),
])
def test_scan_codes_match_the_official_client(name, make):
    usage = keys.usage_for_name(name)
    assert keyboard.encode_press(usage, hid_mode=False).hex() == make
    assert keyboard.encode_release(usage, hid_mode=False).hex() == "f0" + make


def test_extended_keys_use_the_e0_prefix():
    up = keys.usage_for_name("up")
    assert keyboard.encode_press(up, hid_mode=False) == bytes.fromhex("e075")
    assert keyboard.encode_release(up, hid_mode=False) == bytes.fromhex("e0f075")


def test_hid_mode_encoding():
    usage = keys.usage_for_name("a")
    assert keyboard.encode_press(usage, hid_mode=True) == b"\x01\x04"
    assert keyboard.encode_release(usage, hid_mode=True) == b"\x02\x04"


def test_key_without_a_break_code_is_refused_in_ps2_mode():
    with pytest.raises(keyboard.KeyboardError):
        keyboard.encode_release(keys.usage_for_name("pause"), hid_mode=False)


def test_text_events_use_shift_for_capitals_and_symbols():
    a = keys.usage_for_name("a")
    one = keys.usage_for_name("1")
    events = keyboard.text_events("aA!")
    assert events == [
        ("press", a), ("release", a),
        ("press", SHIFT), ("press", a), ("release", a), ("release", SHIFT),
        ("press", SHIFT), ("press", one), ("release", one), ("release", SHIFT),
    ]


def test_newline_and_tab_are_typed_as_keys():
    events = keyboard.text_events("\t\n")
    assert events[0] == ("press", keys.NAMED_KEYS["tab"])
    assert events[2] == ("press", keys.NAMED_KEYS["enter"])


def test_untypeable_characters_are_refused():
    with pytest.raises(keyboard.KeyboardError):
        keyboard.text_events("café")


def test_combination_presses_in_order_and_releases_in_reverse():
    combo = keys.parse_combination("ctrl+alt+delete")
    delete = keys.NAMED_KEYS["delete"]
    assert combo == [keys.LEFT_CTRL, keys.LEFT_ALT, delete]
    assert keyboard.combination_events(combo) == [
        ("press", keys.LEFT_CTRL), ("press", keys.LEFT_ALT), ("press", delete),
        ("release", delete), ("release", keys.LEFT_ALT), ("release", keys.LEFT_CTRL),
    ]


def test_ctrl_numlock_is_refused():
    # It would open the CIM's setup menu, which then swallows key presses.
    with pytest.raises(keyboard.KeyboardError, match="CIM"):
        keyboard.combination_events(keys.parse_combination("ctrl+numlock"))
    with pytest.raises(keyboard.KeyboardError):
        keyboard.combination_events(keys.parse_combination("numlock+rctrl"))


def test_numlock_alone_is_allowed():
    assert keyboard.combination_events([keys.usage_for_name("numlock")])


@pytest.mark.parametrize("name, usage", [
    ("Enter", 0x28), ("RETURN", 0x28), ("esc", 0x29), ("F12", 0x45),
    ("a", 0x04), ("A", 0x04), ("1", 0x1E), ("0", 0x27), ("/", 0x38),
])
def test_usage_for_name(name, usage):
    assert keys.usage_for_name(name) == usage


def test_unknown_key_name():
    with pytest.raises(ValueError):
        keys.usage_for_name("hyper")
    with pytest.raises(ValueError):
        keys.parse_combination("+")


def test_keyboard_sends_one_packet_per_event():
    session = RecordingSession()
    keyboard.Keyboard(session, delay=0).type_text("ab")
    assert session.sent == [bytes.fromhex(code) for code in ("1c", "f01c", "32", "f032")]


def test_keyboard_follows_the_switch_mode():
    session = RecordingSession(hid_mode=True)
    keyboard.Keyboard(session, delay=0).press("F2")
    assert session.sent == [b"\x01\x3b", b"\x02\x3b"]


def test_every_typeable_character_has_ps2_codes():
    for character, (usage, _shift) in keys.CHARACTERS.items():
        assert keyboard.encode_press(usage, hid_mode=False), character
        assert keyboard.encode_release(usage, hid_mode=False), character
