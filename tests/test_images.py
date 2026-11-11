# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import io

import pytest
from PIL import Image

from koidaemon import images
from koidaemon.backends.base import Snapshot
from koidaemon.errors import BackendError

from fakes import make_picture


def opened(snapshot):
    picture = Image.open(io.BytesIO(snapshot.data))
    picture.load()
    return picture


def test_jpeg_to_png():
    jpeg = Snapshot("jpeg", make_picture(32, 24, "JPEG"), 32, 24)
    png = images.convert(jpeg, "png")
    assert png.format == "png"
    assert png.data.startswith(b"\x89PNG")
    assert (png.width, png.height) == (32, 24)
    # PNG is lossless, so it holds exactly what the JPEG decoded to.
    assert opened(png).tobytes() == opened(jpeg).tobytes()


def test_png_to_jpeg():
    png = Snapshot("png", make_picture(32, 24), 32, 24)
    jpeg = images.convert(png, "jpeg")
    assert jpeg.format == "jpeg"
    assert jpeg.data.startswith(b"\xff\xd8")
    assert opened(jpeg).size == (32, 24)


def test_size_comes_from_the_picture():
    # Some KVMs cannot say the size; the converted picture can.
    png = images.convert(Snapshot("jpeg", make_picture(20, 10, "JPEG")), "png")
    assert (png.width, png.height) == (20, 10)


@pytest.mark.parametrize("mode", ["RGBA", "P", "LA"])
def test_pictures_jpeg_cannot_hold_as_they_are(mode):
    picture = Image.new(mode, (8, 8))
    output = io.BytesIO()
    picture.save(output, "PNG")
    jpeg = images.convert(Snapshot("png", output.getvalue()), "jpeg")
    assert opened(jpeg).mode == "RGB"


def test_already_in_the_wanted_format():
    snapshot = Snapshot("png", b"not even decoded", 1, 1)
    assert images.convert(snapshot, "png") is snapshot


def test_native_is_never_converted():
    snapshot = Snapshot("jpeg", b"as the KVM sent it", 1, 1)
    assert images.convert(snapshot, "native") is snapshot


def test_a_picture_that_cannot_be_read():
    with pytest.raises(BackendError, match="cannot turn the jpeg snapshot into png"):
        images.convert(Snapshot("jpeg", b"\xff\xd8 broken"), "png")
