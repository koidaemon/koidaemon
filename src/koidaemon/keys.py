# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Key names, as USB HID usage ids (keyboard page 0x07).

Every backend works from HID usage ids: the Raritan backend turns them into
PS/2 scan codes, and the PiKVM backend into the browser-style key names
("KeyA", "ControlLeft") its API takes.  So one set of key names behaves the
same on every KVM.

A key can be named in two ways, both case-insensitive:

  * a short name: "enter", "esc", "f2", "ctrl", "a", "1", "-", ...
  * its browser KeyboardEvent.code: "Enter", "Escape", "KeyA", "Digit1", ...

Text is typed with a US keyboard layout.
"""

from __future__ import annotations

# Modifier keys.
LEFT_CTRL = 0xE0
LEFT_SHIFT = 0xE1
LEFT_ALT = 0xE2
LEFT_GUI = 0xE3
RIGHT_CTRL = 0xE4
RIGHT_SHIFT = 0xE5
RIGHT_ALT = 0xE6
RIGHT_GUI = 0xE7

MODIFIERS = frozenset(range(LEFT_CTRL, RIGHT_GUI + 1))

# Short names, matched case-insensitively.  Several spellings are accepted
# for the common ones.
NAMED_KEYS = {
    "enter": 0x28, "return": 0x28,
    "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A,
    "tab": 0x2B,
    "space": 0x2C,
    "capslock": 0x39,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
    "printscreen": 0x46, "sysrq": 0x46,
    "scrolllock": 0x47,
    "pause": 0x48,
    "insert": 0x49,
    "home": 0x4A,
    "pageup": 0x4B, "pgup": 0x4B,
    "delete": 0x4C, "del": 0x4C,
    "end": 0x4D,
    "pagedown": 0x4E, "pgdn": 0x4E,
    "right": 0x4F,
    "left": 0x50,
    "down": 0x51,
    "up": 0x52,
    "numlock": 0x53,
    "menu": 0x65, "application": 0x65,
    "ctrl": LEFT_CTRL, "lctrl": LEFT_CTRL, "control": LEFT_CTRL,
    "shift": LEFT_SHIFT, "lshift": LEFT_SHIFT,
    "alt": LEFT_ALT, "lalt": LEFT_ALT,
    "super": LEFT_GUI, "win": LEFT_GUI, "meta": LEFT_GUI, "lsuper": LEFT_GUI,
    "rctrl": RIGHT_CTRL,
    "rshift": RIGHT_SHIFT,
    "ralt": RIGHT_ALT, "altgr": RIGHT_ALT,
    "rsuper": RIGHT_GUI,
}


def _build_web_codes() -> dict[int, str]:
    """Map each usage to its browser KeyboardEvent.code name."""
    codes = {}
    for offset, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        codes[0x04 + offset] = "Key" + letter
    for offset, digit in enumerate("1234567890"):
        codes[0x1E + offset] = "Digit" + digit
    for offset in range(12):
        codes[0x3A + offset] = "F%d" % (offset + 1)
    for offset, digit in enumerate("1234567890"):
        codes[0x59 + offset] = "Numpad" + digit

    codes.update({
        0x28: "Enter", 0x29: "Escape", 0x2A: "Backspace", 0x2B: "Tab",
        0x2C: "Space", 0x2D: "Minus", 0x2E: "Equal", 0x2F: "BracketLeft",
        0x30: "BracketRight", 0x31: "Backslash", 0x33: "Semicolon",
        0x34: "Quote", 0x35: "Backquote", 0x36: "Comma", 0x37: "Period",
        0x38: "Slash", 0x39: "CapsLock", 0x46: "PrintScreen",
        0x47: "ScrollLock", 0x48: "Pause", 0x49: "Insert", 0x4A: "Home",
        0x4B: "PageUp", 0x4C: "Delete", 0x4D: "End", 0x4E: "PageDown",
        0x4F: "ArrowRight", 0x50: "ArrowLeft", 0x51: "ArrowDown",
        0x52: "ArrowUp", 0x53: "NumLock", 0x54: "NumpadDivide",
        0x55: "NumpadMultiply", 0x56: "NumpadSubtract", 0x57: "NumpadAdd",
        0x58: "NumpadEnter", 0x63: "NumpadDecimal", 0x64: "IntlBackslash",
        0x65: "ContextMenu",
        LEFT_CTRL: "ControlLeft", LEFT_SHIFT: "ShiftLeft",
        LEFT_ALT: "AltLeft", LEFT_GUI: "MetaLeft",
        RIGHT_CTRL: "ControlRight", RIGHT_SHIFT: "ShiftRight",
        RIGHT_ALT: "AltRight", RIGHT_GUI: "MetaRight",
    })
    return codes


WEB_CODES = _build_web_codes()
_USAGE_FOR_WEB_CODE = {code.lower(): usage for usage, code in WEB_CODES.items()}

# Characters typed without shift, and the same keys with shift held.
_DIGIT_SHIFTED = "!@#$%^&*()"
_PUNCTUATION = {
    # usage: (unshifted, shifted)
    0x2D: ("-", "_"),
    0x2E: ("=", "+"),
    0x2F: ("[", "{"),
    0x30: ("]", "}"),
    0x31: ("\\", "|"),
    0x33: (";", ":"),
    0x34: ("'", '"'),
    0x35: ("`", "~"),
    0x36: (",", "<"),
    0x37: (".", ">"),
    0x38: ("/", "?"),
}


def _build_character_map() -> dict[str, tuple[int, bool]]:
    """Map each typeable character to (usage, needs shift)."""
    characters = {}

    for offset, letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
        usage = 0x04 + offset
        characters[letter] = (usage, False)
        characters[letter.upper()] = (usage, True)

    # Digits 1-9 are usages 0x1E-0x26 and 0 is 0x27.
    for offset, digit in enumerate("1234567890"):
        usage = 0x1E + offset
        characters[digit] = (usage, False)
        characters[_DIGIT_SHIFTED[offset]] = (usage, True)

    for usage, (plain, shifted) in _PUNCTUATION.items():
        characters[plain] = (usage, False)
        characters[shifted] = (usage, True)

    characters[" "] = (NAMED_KEYS["space"], False)
    characters["\n"] = (NAMED_KEYS["enter"], False)
    characters["\t"] = (NAMED_KEYS["tab"], False)
    return characters


CHARACTERS = _build_character_map()


def usage_for_name(name: str) -> int:
    """HID usage for a key name such as "Enter", "F2", "ctrl", "KeyA" or "a"."""
    lowered = name.lower()
    if lowered in NAMED_KEYS:
        return NAMED_KEYS[lowered]
    if lowered in _USAGE_FOR_WEB_CODE:
        return _USAGE_FOR_WEB_CODE[lowered]

    # A single character names the key it is on, so "A" and "a" are the
    # same key, as are "!" and "1".
    if len(name) == 1 and name in CHARACTERS:
        usage, _needs_shift = CHARACTERS[name]
        return usage

    raise ValueError("unknown key name: %r" % name)


def parse_combination(text: str) -> list[int]:
    """Parse "ctrl+alt+delete" into a list of usages, in the order given."""
    parts = [part for part in text.split("+") if part]
    if not parts:
        raise ValueError("empty key combination")
    return [usage_for_name(part) for part in parts]


def web_code(usage: int) -> str:
    """The browser KeyboardEvent.code for a usage, e.g. 0x04 -> "KeyA"."""
    if usage not in WEB_CODES:
        raise ValueError("key 0x%02x has no browser key name" % usage)
    return WEB_CODES[usage]
