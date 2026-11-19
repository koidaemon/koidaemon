# Copyright (c) 2026 Koen Kooi
#
# SPDX-License-Identifier: MIT

"""The jetkvm backend against a fake JetKVM, over real WebRTC."""

import hashlib
import io
import time

import pytest

pytest.importorskip("aiortc")
pytest.importorskip("av")

from aiortc import RTCDataChannel
from PIL import Image

from koidaemon import keys
from koidaemon.backends import jetkvm
from koidaemon.backends.base import Settings
from koidaemon.errors import BackendError, InvalidRequest, Unsupported
from koidaemon.uploads import MIN_CD_SIZE, LocalImage, PushedImage, UploadError, image_name

from fake_jetkvm import FakeJetKVM

CTRL, SHIFT = keys.LEFT_CTRL, keys.LEFT_SHIFT
A, B, ENTER = 0x04, 0x05, 0x28


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr(jetkvm, "KEY_DELAY_MS", 1)
    monkeypatch.setattr(jetkvm, "VIDEO_WAIT", 5.0)


@pytest.fixture
def fake():
    with FakeJetKVM() as device:
        yield device


def make_backend(tmp_path, url, password=None, **settings):
    values = {"url": url, "timeout": 10, **settings}
    if password is not None:
        (tmp_path / "password").write_text(password)
        values["password_file"] = "password"
    return jetkvm.JetKVMBackend("board", Settings("board", values, tmp_path))


@pytest.fixture
def backend(tmp_path, fake):
    backend = make_backend(tmp_path, fake.url)
    yield backend
    backend.close()


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def typed(fake) -> list[tuple[int, list[int]]]:
    """The keyboard reports the fake was sent, as (modifier, keys)."""
    return [(modifier, [key for key in slots if key]) for modifier, slots in fake.keyboard]


# ----------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------

def test_status(fake, backend):
    assert backend.status() == {"online": True, "video": {"width": 320, "height": 240}}
    assert fake.logins == 0


def test_status_without_a_signal(fake, backend):
    fake.signal = None
    assert backend.status() == {"online": True, "video": None}


def test_one_session_for_many_commands(fake, backend):
    backend.status()
    backend.status()
    assert fake.sessions == 1


def test_login(tmp_path):
    with FakeJetKVM(password="secret") as fake:
        backend = make_backend(tmp_path, fake.url, password="secret")
        try:
            assert backend.status()["online"]
        finally:
            backend.close()
        assert fake.logins == 1


def test_wrong_password(tmp_path):
    with FakeJetKVM(password="secret") as fake:
        backend = make_backend(tmp_path, fake.url, password="wrong")
        with pytest.raises(BackendError, match="refused the password"):
            backend.status()
        backend.close()


def test_password_needed(tmp_path):
    with FakeJetKVM(password="secret") as fake:
        backend = make_backend(tmp_path, fake.url)
        with pytest.raises(BackendError, match="refused the login: it needs a password_file"):
            backend.status()
        backend.close()


def test_unreachable(tmp_path):
    backend = make_backend(tmp_path, "http://127.0.0.1:1", timeout=2)
    with pytest.raises(BackendError, match="cannot reach http://127.0.0.1:1"):
        backend.status()
    backend.close()


def test_idle_session_is_closed(tmp_path, fake):
    backend = make_backend(tmp_path, fake.url, idle_timeout=0.3)
    try:
        backend.status()
        wait_for(lambda: fake.connected == 0)
        backend.status()
        assert fake.sessions == 2
    finally:
        backend.close()


def test_session_stays_while_a_key_is_held(tmp_path, fake):
    backend = make_backend(tmp_path, fake.url, idle_timeout=0.3)
    try:
        backend.key_down(CTRL)
        time.sleep(1)
        assert fake.connected == 1
        backend.key_up(CTRL)
        wait_for(lambda: fake.connected == 0)
    finally:
        backend.close()


def test_only_a_snapshot_has_video(fake, backend):
    backend.status()
    backend.press([[A]])
    assert (fake.sessions, fake.video_sessions) == (1, 0)
    backend.snapshot()
    assert (fake.sessions, fake.video_sessions) == (2, 1)
    backend.press([[A]])
    backend.snapshot()
    assert (fake.sessions, fake.video_sessions) == (2, 1)


def test_a_session_with_video_is_closed_soon(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(jetkvm, "VIDEO_IDLE", 0.3)
    backend = make_backend(tmp_path, fake.url)
    try:
        backend.snapshot()
        wait_for(lambda: fake.connected == 0)
    finally:
        backend.close()


def test_held_keys_survive_the_snapshot(fake, backend):
    backend.key_down(SHIFT)
    backend.snapshot()
    assert fake.sessions == 2
    assert typed(fake) == [(2, []), (2, [])]
    backend.key_up(SHIFT)


def test_taken_over(fake, backend):
    backend.status()
    fake.kick()
    assert backend.status()["online"]
    assert fake.sessions == 2


def test_close(fake, backend):
    backend.status()
    backend.close()
    wait_for(lambda: fake.connected == 0)


def test_device_errors_are_passed_on(fake, backend):
    with pytest.raises(BackendError, match="Method not found"):
        backend._run(lambda session: session.call("launchRocket"))


# ----------------------------------------------------------------------
# Screen
# ----------------------------------------------------------------------

def test_snapshot(fake, backend):
    snapshot = backend.snapshot()
    assert (snapshot.format, snapshot.width, snapshot.height) == ("png", 320, 240)
    with Image.open(io.BytesIO(snapshot.data)) as picture:
        left = picture.getpixel((80, 120))
        right = picture.getpixel((240, 120))
    # H.264 is lossy, so only nearly the test picture's red and blue.
    assert left[0] > 200 and left[2] < 60
    assert right[2] > 200 and right[0] < 60


def test_snapshot_without_a_signal(fake, backend):
    fake.signal = None
    with pytest.raises(BackendError, match=r"no video signal from the target \(no_signal\)"):
        backend.snapshot()


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------

def test_typing(fake, backend):
    backend.type_text("aB\n")
    assert typed(fake) == [(0, [A]), (0, []), (2, [B]), (0, []), (0, [ENTER]), (0, [])]
    assert fake.macros == [6]


def test_long_text_goes_in_several_macros(fake, backend, monkeypatch):
    monkeypatch.setattr(jetkvm, "MACRO_STEPS", 4)
    backend.type_text("aaaaa")
    assert fake.macros == [4, 4, 2]
    assert typed(fake) == [(0, [A]), (0, [])] * 5


def test_typing_what_a_us_keyboard_cannot(fake, backend):
    with pytest.raises(InvalidRequest, match="cannot type"):
        backend.type_text("€")
    assert fake.keyboard == []


def test_press(fake, backend):
    backend.press([[CTRL, A], [ENTER]])
    assert typed(fake) == [(1, []), (1, [A]), (1, []), (0, []), (0, [ENTER]), (0, [])]


def test_keys_held(fake, backend):
    backend.key_down(SHIFT)
    backend.type_text("a")
    backend.key_up(SHIFT)
    assert typed(fake) == [(2, []), (0, [A]), (2, []), (0, [])]


# ----------------------------------------------------------------------
# Mouse
# ----------------------------------------------------------------------

def test_move(fake, backend):
    backend.mouse_move(319, 120)
    wait_for(lambda: fake.pointer)
    assert fake.pointer == [(32767, 16452, 0)]


def test_click(fake, backend):
    backend.mouse_click("right", 2, 0, 239)
    wait_for(lambda: len(fake.pointer) == 5)
    assert fake.pointer == [(0, 32767, 0), (0, 32767, 2), (0, 32767, 0),
                            (0, 32767, 2), (0, 32767, 0)]


def test_drag(fake, backend):
    backend.mouse_down("left")
    backend.mouse_move(0, 0)
    backend.mouse_up("left")
    wait_for(lambda: len(fake.pointer) == 3)
    assert [buttons for _x, _y, buttons in fake.pointer] == [1, 1, 0]


def test_move_without_a_signal(fake, backend):
    fake.signal = None
    with pytest.raises(BackendError, match="no video signal"):
        backend.mouse_move(1, 1)


def test_scroll(fake, backend):
    backend.mouse_scroll(-100)
    backend.mouse_scroll(3)
    assert fake.calls.count("wheelReport") == 2
    assert fake.wheel == [-100, 3]


def test_a_report_the_channel_cannot_send(tmp_path, fake, monkeypatch):
    backend = make_backend(tmp_path, fake.url, timeout=0.5)
    try:
        backend.status()
        monkeypatch.setattr(RTCDataChannel, "bufferedAmount", property(lambda channel: 1))
        with pytest.raises(BackendError, match="did not take the mouse report within 0.5 s"):
            backend.mouse_move(1, 1)
    finally:
        monkeypatch.undo()
        backend.close()


def test_a_wedged_loop(tmp_path, fake, monkeypatch):
    backend = make_backend(tmp_path, fake.url, timeout=0.2)
    try:
        backend.status()

        async def stuck(session):
            time.sleep(2)

        with pytest.raises(BackendError, match="did not finish within 1 s"):
            backend._run(stuck)
    finally:
        backend.close()


def test_a_failed_close_is_not_held_against_later_commands(tmp_path, fake, monkeypatch):
    backend = make_backend(tmp_path, fake.url, idle_timeout=0.2)
    try:
        backend.status()
        close = jetkvm._Session.close

        async def failing(session):
            await close(session)
            raise RuntimeError("cannot close")

        monkeypatch.setattr(jetkvm._Session, "close", failing)
        wait_for(lambda: backend._closing is not None)
        monkeypatch.setattr(jetkvm._Session, "close", close)
        assert backend.status()["online"]
        assert backend._closing is None
    finally:
        backend.close()


# ----------------------------------------------------------------------
# USB storage
# ----------------------------------------------------------------------

@pytest.fixture
def image(tmp_path):
    path = tmp_path / "installer.iso"
    # Bigger than a socket buffer.
    path.write_bytes(bytes(range(256)) * (3 * 1024 * 1024 // 256))
    return path


def stored_name(path) -> str:
    return image_name(path.name, hashlib.sha256(path.read_bytes()).hexdigest())


def test_insert(fake, backend, image):
    status = backend.storage_insert(LocalImage(image), "cdrom", False)
    name = stored_name(image)
    assert status == {"inserted": True, "image": name, "mode": "cdrom", "writable": False}
    assert bytes(fake.files[name]) == image.read_bytes()
    assert fake.mounted["mode"] == "CDROM"
    assert backend.storage_status() == status


def test_insert_what_is_already_there(fake, backend, image):
    fake.files[stored_name(image)] = bytearray(image.read_bytes())
    backend.storage_insert(LocalImage(image), "stick", False)
    assert "startStorageFileUpload" not in fake.calls
    assert fake.mounted["mode"] == "Disk"


def test_insert_replaces(fake, backend, image):
    fake.files["other.img"] = bytearray(10)
    fake.mounted = {"source": "Storage", "mode": "Disk", "filename": "other.img", "size": 10}
    backend.storage_insert(LocalImage(image), "stick", False)
    assert fake.mounted["filename"] == stored_name(image)


def test_a_stale_partial_upload_is_not_resumed(fake, backend, image):
    fake.files[stored_name(image) + ".incomplete"] = bytearray(b"other bytes")
    backend.storage_insert(LocalImage(image), "stick", False)
    assert bytes(fake.files[stored_name(image)]) == image.read_bytes()


def test_insert_a_pushed_image(fake, backend):
    contents = b"\x55" * 100000
    digest = hashlib.sha256(contents).hexdigest()
    image = PushedImage("disk.img", len(contents), digest, lambda size: io.BytesIO(contents))
    backend.storage_insert(image, "stick", False)
    assert bytes(fake.files[image_name("disk.img", digest)]) == contents


def test_a_pushed_image_that_does_not_match(fake, backend):
    contents = b"\x55" * 100000
    digest = hashlib.sha256(b"something else").hexdigest()
    image = PushedImage("disk.img", len(contents), digest, lambda size: io.BytesIO(contents))
    with pytest.raises(UploadError, match="SHA-256"):
        backend.storage_insert(image, "stick", False)
    assert fake.files == {}
    assert fake.mounted is None


def test_an_upload_the_device_stops(fake, backend, image):
    fake.stop_upload_after = 100000
    with pytest.raises(BackendError, match="refused the upload: HTTP 500"):
        backend.storage_insert(LocalImage(image), "stick", False)
    assert fake.files == {}


def test_too_little_space(fake, backend, image):
    fake.bytes_free = 1000
    with pytest.raises(BackendError, match="1000 bytes free"):
        backend.storage_insert(LocalImage(image), "stick", False)
    assert "startStorageFileUpload" not in fake.calls


def test_writable_is_refused(fake, backend, image):
    with pytest.raises(Unsupported, match="read-only"):
        backend.storage_insert(LocalImage(image), "stick", True)
    assert fake.sessions == 0


def test_a_cd_too_small(tmp_path, fake, backend):
    path = tmp_path / "tiny.iso"
    path.write_bytes(bytes(MIN_CD_SIZE - 1))
    with pytest.raises(InvalidRequest, match="too small to be a CD"):
        backend.storage_insert(LocalImage(path), "cdrom", False)


def test_device_errors_carry_the_firmware_message(fake, backend):
    with pytest.raises(BackendError, match="file does not exist: nothing"):
        backend._run(lambda session: session.call("deleteStorageFile", filename="nothing"))


def test_eject(fake, backend, image):
    backend.storage_insert(LocalImage(image), "stick", False)
    backend.storage_eject()
    assert fake.mounted is None
    assert backend.storage_status() == {"inserted": False, "image": None, "mode": None,
                                        "writable": False}
    backend.storage_eject()
    assert fake.calls.count("unmountImage") == 1
