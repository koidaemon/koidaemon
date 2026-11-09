# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""PS/2 scan code set 2, indexed by USB HID usage id.

A Dominion KX whose server id does not set the HID option expects PS/2 set 2
make and break codes in keyboard packets, even when the CIM on the target
port is a USB one: the switch does the translation.  Keys with no break
code (Pause) are left out.
"""

from __future__ import annotations

# usage: (make bytes, break bytes), as hex strings.
_SET2 = {
    0x04: ("1c", "f0 1c"),  # 'a'
    0x05: ("32", "f0 32"),  # 'b'
    0x06: ("21", "f0 21"),  # 'c'
    0x07: ("23", "f0 23"),  # 'd'
    0x08: ("24", "f0 24"),  # 'e'
    0x09: ("2b", "f0 2b"),  # 'f'
    0x0A: ("34", "f0 34"),  # 'g'
    0x0B: ("33", "f0 33"),  # 'h'
    0x0C: ("43", "f0 43"),  # 'i'
    0x0D: ("3b", "f0 3b"),  # 'j'
    0x0E: ("42", "f0 42"),  # 'k'
    0x0F: ("4b", "f0 4b"),  # 'l'
    0x10: ("3a", "f0 3a"),  # 'm'
    0x11: ("31", "f0 31"),  # 'n'
    0x12: ("44", "f0 44"),  # 'o'
    0x13: ("4d", "f0 4d"),  # 'p'
    0x14: ("15", "f0 15"),  # 'q'
    0x15: ("2d", "f0 2d"),  # 'r'
    0x16: ("1b", "f0 1b"),  # 's'
    0x17: ("2c", "f0 2c"),  # 't'
    0x18: ("3c", "f0 3c"),  # 'u'
    0x19: ("2a", "f0 2a"),  # 'v'
    0x1A: ("1d", "f0 1d"),  # 'w'
    0x1B: ("22", "f0 22"),  # 'x'
    0x1C: ("35", "f0 35"),  # 'y'
    0x1D: ("1a", "f0 1a"),  # 'z'
    0x1E: ("16", "f0 16"),  # '1'
    0x1F: ("1e", "f0 1e"),  # '2'
    0x20: ("26", "f0 26"),  # '3'
    0x21: ("25", "f0 25"),  # '4'
    0x22: ("2e", "f0 2e"),  # '5'
    0x23: ("36", "f0 36"),  # '6'
    0x24: ("3d", "f0 3d"),  # '7'
    0x25: ("3e", "f0 3e"),  # '8'
    0x26: ("46", "f0 46"),  # '9'
    0x27: ("45", "f0 45"),  # '0'
    0x28: ("5a", "f0 5a"),  # enter
    0x29: ("76", "f0 76"),  # esc
    0x2A: ("66", "f0 66"),  # backspace
    0x2B: ("0d", "f0 0d"),  # tab
    0x2C: ("29", "f0 29"),  # space
    0x2D: ("4e", "f0 4e"),  # '-'
    0x2E: ("55", "f0 55"),  # '='
    0x2F: ("54", "f0 54"),  # '['
    0x30: ("5b", "f0 5b"),  # ']'
    0x31: ("5d", "f0 5d"),  # '\\'
    0x32: ("5d", "f0 5d"),  # non-US #
    0x33: ("4c", "f0 4c"),  # ';'
    0x34: ("52", "f0 52"),  # "'"
    0x35: ("0e", "f0 0e"),  # '`'
    0x36: ("41", "f0 41"),  # ','
    0x37: ("49", "f0 49"),  # '.'
    0x38: ("4a", "f0 4a"),  # '/'
    0x39: ("58", "f0 58"),  # capslock
    0x3A: ("05", "f0 05"),  # f1
    0x3B: ("06", "f0 06"),  # f2
    0x3C: ("04", "f0 04"),  # f3
    0x3D: ("0c", "f0 0c"),  # f4
    0x3E: ("03", "f0 03"),  # f5
    0x3F: ("0b", "f0 0b"),  # f6
    0x40: ("83", "f0 83"),  # f7
    0x41: ("0a", "f0 0a"),  # f8
    0x42: ("01", "f0 01"),  # f9
    0x43: ("09", "f0 09"),  # f10
    0x44: ("78", "f0 78"),  # f11
    0x45: ("07", "f0 07"),  # f12
    0x46: ("e0 7c", "e0 f0 7c"),  # printscreen
    0x47: ("7e", "f0 7e"),  # scrolllock
    0x49: ("e0 70", "e0 f0 70"),  # insert
    0x4A: ("e0 6c", "e0 f0 6c"),  # home
    0x4B: ("e0 7d", "e0 f0 7d"),  # pageup
    0x4C: ("e0 71", "e0 f0 71"),  # delete
    0x4D: ("e0 69", "e0 f0 69"),  # end
    0x4E: ("e0 7a", "e0 f0 7a"),  # pagedown
    0x4F: ("e0 74", "e0 f0 74"),  # right
    0x50: ("e0 6b", "e0 f0 6b"),  # left
    0x51: ("e0 72", "e0 f0 72"),  # down
    0x52: ("e0 75", "e0 f0 75"),  # up
    0x53: ("77", "f0 77"),  # numlock
    0x54: ("e0 4a", "e0 f0 4a"),  # keypad /
    0x55: ("7c", "f0 7c"),  # keypad *
    0x56: ("7b", "f0 7b"),  # keypad -
    0x57: ("79", "f0 79"),  # keypad +
    0x58: ("5a", "f0 5a"),  # keypad enter
    0x59: ("69", "f0 69"),  # keypad 1
    0x5A: ("72", "f0 72"),  # keypad 2
    0x5B: ("7a", "f0 7a"),  # keypad 3
    0x5C: ("6b", "f0 6b"),  # keypad 4
    0x5D: ("73", "f0 73"),  # keypad 5
    0x5E: ("74", "f0 74"),  # keypad 6
    0x5F: ("6c", "f0 6c"),  # keypad 7
    0x60: ("75", "f0 75"),  # keypad 8
    0x61: ("7d", "f0 7d"),  # keypad 9
    0x62: ("70", "f0 70"),  # keypad 0
    0x63: ("71", "f0 71"),  # keypad .
    0x64: ("61", "f0 61"),  # non-US \
    0x65: ("e0 2f", "e0 f0 2f"),  # menu
    0x66: ("e0 37", "e0 f0 37"),  # usage 0x66
    0x67: ("0f", "f0 0f"),  # usage 0x67
    0x68: ("08", "f0 08"),  # usage 0x68
    0x69: ("10", "f0 10"),  # usage 0x69
    0x6A: ("18", "f0 18"),  # usage 0x6a
    0x6B: ("20", "f0 20"),  # usage 0x6b
    0x6C: ("28", "f0 28"),  # usage 0x6c
    0x6D: ("30", "f0 30"),  # usage 0x6d
    0x6E: ("38", "f0 38"),  # usage 0x6e
    0x6F: ("40", "f0 40"),  # usage 0x6f
    0x70: ("48", "f0 48"),  # usage 0x70
    0x71: ("50", "f0 50"),  # usage 0x71
    0x72: ("57", "f0 57"),  # usage 0x72
    0x73: ("5f", "f0 5f"),  # usage 0x73
    0x85: ("6d", "f0 6d"),  # usage 0x85
    0x87: ("51", "f0 51"),  # usage 0x87
    0x88: ("13", "f0 13"),  # usage 0x88
    0x89: ("6a", "f0 6a"),  # usage 0x89
    0x8A: ("64", "f0 64"),  # usage 0x8a
    0x8B: ("67", "f0 67"),  # usage 0x8b
    0x8C: ("27", "f0 27"),  # usage 0x8c
    0x92: ("63", "f0 63"),  # usage 0x92
    0x93: ("62", "f0 62"),  # usage 0x93
    0x94: ("5f", "f0 5f"),  # usage 0x94
    0xE0: ("14", "f0 14"),  # ctrl
    0xE1: ("12", "f0 12"),  # shift
    0xE2: ("11", "f0 11"),  # alt
    0xE3: ("e0 1f", "e0 f0 1f"),  # super
    0xE4: ("e0 14", "e0 f0 14"),  # rctrl
    0xE5: ("59", "f0 59"),  # rshift
    0xE6: ("e0 11", "e0 f0 11"),  # ralt
    0xE7: ("e0 27", "e0 f0 27"),  # rsuper
}

SET2_MAKE = {usage: bytes.fromhex(make) for usage, (make, _) in _SET2.items()}
SET2_BREAK = {usage: bytes.fromhex(brk) for usage, (_, brk) in _SET2.items()}
