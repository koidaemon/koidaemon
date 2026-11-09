# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""The commands koid understands, the same for every kind of KVM.

This is the single definition of the command list: the daemon validates
requests against it, and koictl builds requests from it.  Each command needs
one capability; a backend lists the capabilities it has, and a command it
lacks is refused as unsupported before the backend is touched.

Positions are screen pixels from the top-left corner.  Key names are those
of koidaemon.keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from koidaemon import keys
from koidaemon.errors import InvalidRequest

# Capabilities a backend can have.
SNAPSHOT = "snapshot"
KEYBOARD = "keyboard"
MOUSE = "mouse"
STORAGE_STICK = "storage.stick"
STORAGE_CDROM = "storage.cdrom"
ALL_CAPABILITIES = (SNAPSHOT, KEYBOARD, MOUSE, STORAGE_STICK, STORAGE_CDROM)

# Not a capability of its own: commands that work with either kind of drive.
ANY_STORAGE = "storage"

MOUSE_BUTTONS = ("left", "right", "middle")
STORAGE_MODES = ("stick", "cdrom")
MAX_CLICKS = 10
MAX_SCROLL = 100


@dataclass(frozen=True)
class Argument:
    name: str
    kind: type
    help: str
    required: bool = False
    default: Any = None
    choices: tuple = ()


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    # The capability needed, or None if every target can do it.
    capability: str | None
    arguments: tuple[Argument, ...] = ()


_X = Argument("x", int, "pixels from the left edge")
_Y = Argument("y", int, "pixels from the top edge")
_BUTTON = Argument("button", str, "mouse button", default="left", choices=MOUSE_BUTTONS)

COMMANDS = {command.name: command for command in (
    Command("status", "whether the target is reachable, and what it can do", None),
    Command("snapshot", "a picture of the target's screen", SNAPSHOT),

    Command("keyboard.type", "type text (US layout)", KEYBOARD, (
        Argument("text", str, "the text, taken literally; \\n is Enter", required=True),
    )),
    Command("keyboard.press", "press and release keys or combinations, in order", KEYBOARD, (
        Argument("keys", list, 'combinations such as "f2" or "ctrl+alt+delete"', required=True),
    )),
    Command("keyboard.down", "hold a key down until keyboard.up", KEYBOARD, (
        Argument("key", str, "key name", required=True),
    )),
    Command("keyboard.up", "release a key held with keyboard.down", KEYBOARD, (
        Argument("key", str, "key name", required=True),
    )),

    Command("mouse.move", "move the pointer", MOUSE, (
        Argument("x", int, "pixels from the left edge", required=True),
        Argument("y", int, "pixels from the top edge", required=True),
    )),
    Command("mouse.click", "click a button, optionally after moving", MOUSE, (
        _BUTTON,
        Argument("count", int, "2 for a double click", default=1),
        _X,
        _Y,
    )),
    Command("mouse.down", "hold a mouse button down", MOUSE, (_BUTTON,)),
    Command("mouse.up", "release a mouse button", MOUSE, (_BUTTON,)),
    Command("mouse.scroll", "turn the wheel; positive scrolls up", MOUSE, (
        Argument("notches", int, "how far; positive is up, negative down", required=True),
    )),

    # storage.insert needs storage.stick or storage.cdrom depending on its
    # mode; see required_capability().
    Command("storage.insert", "present a disk image to the target over USB", STORAGE_STICK, (
        Argument("image", str, "absolute path of the image, readable by koid", required=True),
        Argument("mode", str, "as a USB stick or a CD drive", default="stick",
                 choices=STORAGE_MODES),
        Argument("writable", bool, "let the target write to it (sticks only)", default=False),
    )),
    Command("storage.eject", "disconnect the emulated USB drive", ANY_STORAGE),
    Command("storage.status", "what the emulated USB drive is presenting", ANY_STORAGE),
)}


# How argument types are named in error messages.
KIND_NAMES = {str: "text", int: "a whole number", bool: "true or false", list: "a list"}


def _check_type(argument: Argument, value: Any) -> None:
    # bool is a subclass of int in Python; do not let True pass as 1.
    if argument.kind is int and isinstance(value, bool):
        wrong = True
    else:
        wrong = not isinstance(value, argument.kind)
    if wrong:
        raise InvalidRequest("%s must be %s" % (argument.name, KIND_NAMES[argument.kind]))
    if argument.choices and value not in argument.choices:
        raise InvalidRequest("%s must be one of %s" % (argument.name, ", ".join(argument.choices)))


def validate(name: str, args: dict | None) -> dict:
    """Check a command's arguments, fill in defaults and parse key names.

    Returns the arguments a backend is called with.  Key names come back
    parsed: keyboard.press gets "combinations" (lists of HID usages), and
    keyboard.down/up get "usage".
    """
    if name not in COMMANDS:
        raise InvalidRequest("unknown command %r" % name)
    command = COMMANDS[name]
    args = dict(args or {})

    known = {argument.name for argument in command.arguments}
    unknown = sorted(set(args) - known)
    if unknown:
        raise InvalidRequest("%s does not take %s" % (name, ", ".join(unknown)))

    checked = {}
    for argument in command.arguments:
        if argument.name in args and args[argument.name] is not None:
            _check_type(argument, args[argument.name])
            checked[argument.name] = args[argument.name]
        elif argument.required:
            raise InvalidRequest("%s needs %s" % (name, argument.name))
        else:
            checked[argument.name] = argument.default

    _check_meaning(name, checked)
    return checked


def _check_meaning(name: str, args: dict) -> None:
    """Checks that need more than the type of one argument."""
    try:
        if name == "keyboard.press":
            if not args["keys"] or not all(isinstance(key, str) for key in args["keys"]):
                raise InvalidRequest("keys must be a list of key combinations")
            args["combinations"] = [keys.parse_combination(key) for key in args.pop("keys")]
        elif name in ("keyboard.down", "keyboard.up"):
            args["usage"] = keys.usage_for_name(args.pop("key"))
    except ValueError as error:
        raise InvalidRequest(str(error)) from error

    for axis in ("x", "y"):
        if args.get(axis) is not None and args[axis] < 0:
            raise InvalidRequest("%s cannot be negative" % axis)

    if name == "mouse.click":
        if (args["x"] is None) != (args["y"] is None):
            raise InvalidRequest("give both x and y, or neither")
        if not 1 <= args["count"] <= MAX_CLICKS:
            raise InvalidRequest("count must be 1 to %d" % MAX_CLICKS)

    if name == "mouse.scroll" and abs(args["notches"]) > MAX_SCROLL:
        raise InvalidRequest("notches must be within ±%d" % MAX_SCROLL)

    if name == "storage.insert":
        if not os.path.isabs(args["image"]):
            raise InvalidRequest("image must be an absolute path (koid resolves it, not koictl)")
        if args["mode"] == "cdrom" and args["writable"]:
            raise InvalidRequest("a CD drive cannot be writable")


def required_capability(name: str, args: dict) -> str | None:
    """The capability a validated command needs."""
    if name == "storage.insert" and args["mode"] == "cdrom":
        return STORAGE_CDROM
    return COMMANDS[name].capability


def has_capability(capabilities: frozenset, needed: str | None) -> bool:
    """Whether a target with these capabilities can run a command."""
    if needed is None:
        return True
    if needed == ANY_STORAGE:
        return bool(capabilities & {STORAGE_STICK, STORAGE_CDROM})
    return needed in capabilities
