# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pytest

from koidaemon import commands, keys
from koidaemon.errors import InvalidRequest


def test_defaults_are_filled_in():
    assert commands.validate("mouse.click", {}) == {
        "button": "left", "count": 1, "x": None, "y": None,
    }
    assert commands.validate("storage.insert", {"image": "/images/a.img"}) == {
        "image": "/images/a.img", "mode": "stick", "writable": False,
    }
    assert commands.validate("snapshot", None) == {"format": "png"}


def test_commands_without_arguments():
    for name in ("status", "storage.eject", "storage.status"):
        assert commands.validate(name, None) == {}


def test_unknown_command():
    with pytest.raises(InvalidRequest, match="unknown command"):
        commands.validate("keyboard.smash", {})


def test_unknown_argument():
    with pytest.raises(InvalidRequest, match="does not take colour"):
        commands.validate("mouse.move", {"x": 1, "y": 2, "colour": "red"})


def test_missing_argument():
    with pytest.raises(InvalidRequest, match="keyboard.type needs text"):
        commands.validate("keyboard.type", {})


@pytest.mark.parametrize("name, args, message", [
    ("mouse.move", {"x": "1", "y": 2}, "x must be a whole number"),
    ("mouse.move", {"x": True, "y": 2}, "x must be a whole number"),
    ("mouse.move", {"x": 1.5, "y": 2}, "x must be a whole number"),
    ("keyboard.type", {"text": 7}, "text must be text"),
    ("storage.insert", {"image": "/a", "writable": "yes"}, "writable must be true or false"),
    ("mouse.click", {"button": "fourth"}, "button must be one of left, right, middle"),
    ("storage.insert", {"image": "/a", "mode": "floppy"}, "mode must be one of stick, cdrom"),
])
def test_wrong_types_and_choices(name, args, message):
    with pytest.raises(InvalidRequest, match=message):
        commands.validate(name, args)


def test_press_parses_combinations():
    args = commands.validate("keyboard.press", {"keys": ["f2", "ctrl+alt+delete"]})
    assert args == {"combinations": [[0x3B], [keys.LEFT_CTRL, keys.LEFT_ALT, 0x4C]]}


@pytest.mark.parametrize("value", [[], ["f2", 3]])
def test_press_needs_a_list_of_names(value):
    with pytest.raises(InvalidRequest, match="list of key combinations"):
        commands.validate("keyboard.press", {"keys": value})


def test_press_with_an_unknown_key():
    with pytest.raises(InvalidRequest, match="unknown key name"):
        commands.validate("keyboard.press", {"keys": ["ctrl+banana"]})


def test_key_down_and_up_parse_the_key():
    assert commands.validate("keyboard.down", {"key": "shift"}) == {"usage": keys.LEFT_SHIFT}
    assert commands.validate("keyboard.up", {"key": "KeyA"}) == {"usage": 0x04}


def test_negative_positions():
    with pytest.raises(InvalidRequest, match="y cannot be negative"):
        commands.validate("mouse.move", {"x": 0, "y": -1})


def test_click_needs_both_coordinates():
    with pytest.raises(InvalidRequest, match="both x and y"):
        commands.validate("mouse.click", {"x": 10})


@pytest.mark.parametrize("count", [0, commands.MAX_CLICKS + 1])
def test_click_count_limits(count):
    with pytest.raises(InvalidRequest, match="count must be"):
        commands.validate("mouse.click", {"count": count})


def test_scroll_limit():
    commands.validate("mouse.scroll", {"notches": -commands.MAX_SCROLL})
    with pytest.raises(InvalidRequest, match="notches must be within"):
        commands.validate("mouse.scroll", {"notches": commands.MAX_SCROLL + 1})


def test_storage_image_must_be_absolute():
    with pytest.raises(InvalidRequest, match="absolute path"):
        commands.validate("storage.insert", {"image": "installer.iso"})


def test_cd_drives_are_read_only():
    with pytest.raises(InvalidRequest, match="cannot be writable"):
        commands.validate("storage.insert", {"image": "/a.iso", "mode": "cdrom", "writable": True})


def test_required_capability():
    stick = commands.validate("storage.insert", {"image": "/a.img"})
    cdrom = commands.validate("storage.insert", {"image": "/a.iso", "mode": "cdrom"})
    assert commands.required_capability("storage.insert", stick) == commands.STORAGE_STICK
    assert commands.required_capability("storage.insert", cdrom) == commands.STORAGE_CDROM
    assert commands.required_capability("keyboard.type", {"text": "x"}) == commands.KEYBOARD
    assert commands.required_capability("status", {}) is None


def test_has_capability():
    cd_only = frozenset({commands.STORAGE_CDROM})
    assert commands.has_capability(cd_only, commands.STORAGE_CDROM)
    assert not commands.has_capability(cd_only, commands.STORAGE_STICK)
    # Ejecting and checking work with either kind of drive.
    assert commands.has_capability(cd_only, commands.ANY_STORAGE)
    assert not commands.has_capability(frozenset({commands.KEYBOARD}), commands.ANY_STORAGE)
    assert commands.has_capability(frozenset(), None)


def test_every_command_needs_a_known_capability():
    known = set(commands.ALL_CAPABILITIES) | {commands.ANY_STORAGE, None}
    for command in commands.COMMANDS.values():
        assert command.capability in known, command.name
