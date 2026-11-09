# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pytest

from koidaemon import keys


@pytest.mark.parametrize("name, usage", [
    ("enter", 0x28),
    ("Enter", 0x28),
    ("ESC", 0x29),
    ("f12", 0x45),
    ("ctrl", keys.LEFT_CTRL),
    ("altgr", keys.RIGHT_ALT),
    ("a", 0x04),
    ("A", 0x04),
    ("!", 0x1E),
    ("KeyA", 0x04),
    ("keya", 0x04),
    ("Digit0", 0x27),
    ("ControlLeft", keys.LEFT_CTRL),
    ("NumpadEnter", 0x58),
    ("ArrowUp", 0x52),
])
def test_usage_for_name(name, usage):
    assert keys.usage_for_name(name) == usage


def test_unknown_key_name():
    with pytest.raises(ValueError, match="unknown key name"):
        keys.usage_for_name("hyper")


def test_combination_keeps_the_order_given():
    assert keys.parse_combination("ctrl+alt+delete") == [keys.LEFT_CTRL, keys.LEFT_ALT, 0x4C]
    assert keys.parse_combination("ControlLeft+KeyC") == [keys.LEFT_CTRL, 0x06]


def test_empty_combination():
    with pytest.raises(ValueError, match="empty"):
        keys.parse_combination("+")


def test_web_codes_round_trip():
    for usage, code in keys.WEB_CODES.items():
        assert keys.usage_for_name(code) == usage


def test_every_named_key_has_a_web_code():
    for name, usage in keys.NAMED_KEYS.items():
        assert keys.web_code(usage), name


def test_every_typeable_character_has_a_web_code():
    for character, (usage, _shift) in keys.CHARACTERS.items():
        assert keys.web_code(usage), repr(character)


def test_key_without_a_web_code():
    # 0x32 is the non-US hash key, which a US keyboard does not have.
    with pytest.raises(ValueError, match="no browser key name"):
        keys.web_code(0x32)


def test_capitals_and_symbols_need_shift():
    assert keys.CHARACTERS["a"] == (0x04, False)
    assert keys.CHARACTERS["A"] == (0x04, True)
    assert keys.CHARACTERS["_"] == (0x2D, True)
    assert keys.CHARACTERS["\n"] == (0x28, False)
