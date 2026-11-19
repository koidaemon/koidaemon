# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Converting snapshots between picture formats.

KVMs send pictures in their own format: a PiKVM sends JPEG, a Raritan
switch's screen is turned into PNG.  koid hands out PNG unless asked for
something else, so clients get the same format whatever the KVM.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from koidaemon.backends.base import Snapshot
from koidaemon.errors import BackendError

PNG = "png"
JPEG = "jpeg"
# As the KVM sent it, not converted.
NATIVE = "native"
FORMATS = (PNG, JPEG, NATIVE)

JPEG_QUALITY = 90

# Pillow's names for the formats.
_PILLOW_FORMATS = {PNG: "PNG", JPEG: "JPEG"}


def convert(snapshot: Snapshot, wanted: str) -> Snapshot:
    """The snapshot in the wanted format, converted only if it has to be."""
    if wanted == NATIVE or wanted == snapshot.format:
        return snapshot

    try:
        with Image.open(io.BytesIO(snapshot.data)) as picture:
            picture.load()
            if wanted == JPEG and picture.mode not in ("RGB", "L"):
                # JPEG has no transparency or palettes.
                picture = picture.convert("RGB")
            output = io.BytesIO()
            options = {"quality": JPEG_QUALITY} if wanted == JPEG else {}
            picture.save(output, _PILLOW_FORMATS[wanted], **options)
            width, height = picture.size
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise BackendError("cannot turn the %s snapshot into %s: %s"
                           % (snapshot.format, wanted, error)) from error
    return Snapshot(wanted, output.getvalue(), width, height)


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's frame header, or None if not found."""
    if data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        # The start-of-frame markers are C0-CF, except C4 (Huffman tables),
        # C8 (reserved) and CC (arithmetic coding).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[offset + 5:offset + 7], "big")
            width = int.from_bytes(data[offset + 7:offset + 9], "big")
            return width, height
        offset += 2 + length
    return None
