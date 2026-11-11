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
