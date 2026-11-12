# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import hashlib
import pathlib
import ssl
import time

import pytest

from koidaemon import keys
from koidaemon.backends import pikvm
from koidaemon.backends.base import Settings
from koidaemon.errors import BackendError, ConfigError, InvalidRequest, Unsupported

from fake_kvmd import FakeKVMD, make_jpeg
from fakes import make_certificate


def make_backend(tmp_path, url, password="secret", **settings) -> pikvm.PiKVMBackend:
    (tmp_path / "password").write_text(password + "\n")
    values = {"url": url, "password_file": "password", **settings}
    return pikvm.PiKVMBackend("board", Settings("board", values, tmp_path))


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr(pikvm, "VIDEO_WAIT", 0.5)
    monkeypatch.setattr(pikvm, "VIDEO_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(pikvm, "IMAGE_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pikvm, "RELEASE_SETTLE", 0.05)


@pytest.fixture
def kvmd():
    with FakeKVMD() as fake:
        yield fake


@pytest.fixture
def backend(tmp_path, kvmd):
    return make_backend(tmp_path, kvmd.url)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "disk image.img"
    path.write_bytes(b"\x00" * pikvm.MIN_CD_SIZE)
    return path


# ----------------------------------------------------------------------
# Settings and connection
# ----------------------------------------------------------------------

def test_settings(tmp_path):
    backend = make_backend(tmp_path, "https://pikvm.lab/", user="operator", keymap="de",
                           timeout=5)
    assert backend.url == "https://pikvm.lab"
    assert backend.user == "operator"
    assert backend.password == "secret"
    assert backend.keymap == "de"
    assert backend.resource == "pikvm:https://pikvm.lab"
    assert backend.describe()["url"] == "https://pikvm.lab"


def test_unknown_setting(tmp_path):
    with pytest.raises(ConfigError, match="unknown settings colour"):
        make_backend(tmp_path, "https://pikvm.lab", colour="red")


def test_tls_is_verified_by_default(tmp_path):
    backend = make_backend(tmp_path, "https://pikvm.lab")
    assert backend._tls.verify_mode == ssl.CERT_REQUIRED
    assert backend._tls.check_hostname


def test_tls_check_can_be_turned_off(tmp_path):
    backend = make_backend(tmp_path, "https://pikvm.lab", verify_tls=False)
    assert backend._tls.verify_mode == ssl.CERT_NONE


def test_trusting_a_certificate_file(tmp_path):
    cert_file, _key_file = make_certificate(tmp_path)
    backend = make_backend(tmp_path, "https://pikvm.lab", ca_file=cert_file.name)
    assert backend._tls.verify_mode == ssl.CERT_REQUIRED
    # A PiKVM's certificate names the device, not the address it is
    # reached by, so trusting the certificate itself is the check.
    assert not backend._tls.check_hostname


def test_login_headers(kvmd, backend):
    backend.status()
    headers = kvmd.requests[0].headers
    assert headers["X-KVMD-User"] == "admin"
    assert headers["X-KVMD-Passwd"] == "secret"


def test_wrong_password(tmp_path, kvmd):
    backend = make_backend(tmp_path, kvmd.url, password="wrong")
    with pytest.raises(BackendError, match="refused the login for admin"):
        backend.status()


def test_unreachable(tmp_path):
    backend = make_backend(tmp_path, "http://127.0.0.1:1", timeout=2)
    with pytest.raises(BackendError, match="cannot reach"):
        backend.status()


def test_http_errors_without_details(kvmd, backend):
    kvmd.answer_everything_with = (500, b"oops")
    with pytest.raises(BackendError, match="answered HTTP 500"):
        backend.status()


def test_failure_reported_with_http_200(kvmd, backend):
    kvmd.answer_everything_with = (200, b'{"ok": false, "result": {"error_msg": "nope"}}')
    with pytest.raises(BackendError, match="nope"):
        backend.status()


def test_answers_that_are_not_json(kvmd, backend):
    kvmd.answer_everything_with = (200, b"<html>")
    with pytest.raises(BackendError, match="not JSON"):
        backend.status()


def test_kvmd_error_messages_are_passed_on(kvmd, backend):
    kvmd.msd_enabled = True
    kvmd.drive["connected"] = True
    kvmd.images["x"] = {"size": 1, "complete": True}
    with pytest.raises(BackendError, match="the drive is connected"):
        backend._json("POST", "msd/set_params", {"image": "x"})


# ----------------------------------------------------------------------
# Status and screen
# ----------------------------------------------------------------------

def test_status_while_the_streamer_runs(kvmd, backend):
    kvmd.stream_forever = True
    assert backend.status() == {"online": True, "video": {"width": 1920, "height": 1080}}


def test_status_does_not_start_the_streamer(kvmd, backend):
    assert backend.status() == {"online": True, "video": None}
    assert kvmd.watch_count == 0


def test_status_without_a_signal(kvmd, backend):
    kvmd.stream_forever = True
    kvmd.signal = None
    assert backend.status() == {"online": True, "video": None}


def test_snapshot_watches_to_start_the_streamer(kvmd, backend):
    kvmd.signal = (1024, 768)
    snapshot = backend.snapshot()
    assert snapshot.format == "jpeg"
    assert snapshot.data == make_jpeg(1024, 768)
    assert (snapshot.width, snapshot.height) == (1024, 768)
    (watch,) = kvmd.calls("ws")
    assert watch.query == {"stream": "1"}
    assert watch.headers["X-KVMD-Passwd"] == "secret"


def test_snapshot_stops_watching_afterwards(kvmd, backend):
    backend.snapshot()
    deadline = time.monotonic() + 5
    while kvmd.watchers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert kvmd.watchers == 0


def test_snapshot_while_the_streamer_runs(kvmd, backend):
    kvmd.stream_forever = True
    backend.snapshot()
    assert kvmd.watch_count == 0


def test_snapshot_waits_for_the_first_frame(kvmd, backend):
    kvmd.unready_snapshots = 3
    assert backend.snapshot().width == 1920
    assert len(kvmd.calls("streamer/snapshot")) == 4


def test_snapshot_without_a_signal(kvmd, backend):
    kvmd.signal = None
    with pytest.raises(BackendError, match="no video signal from the target"):
        backend.snapshot()
    assert kvmd.calls("streamer/snapshot") == []


def test_snapshot_never_ready(kvmd, backend):
    kvmd.unready_snapshots = 1000
    with pytest.raises(BackendError, match="Service Unavailable"):
        backend.snapshot()


def test_watching_with_the_wrong_password(tmp_path, kvmd, backend, monkeypatch):
    # The streamer state is readable, so only the WebSocket is refused.
    monkeypatch.setattr(backend, "_source", lambda: None)
    backend.password = "wrong"
    with pytest.raises(BackendError, match="refused the login for admin"):
        backend.snapshot()


def test_jpeg_size():
    assert pikvm.jpeg_size(make_jpeg(800, 600)) == (800, 600)
    progressive = make_jpeg(640, 480).replace(b"\xff\xc0", b"\xff\xc2")
    assert pikvm.jpeg_size(progressive) == (640, 480)


@pytest.mark.parametrize("data", [
    b"\x89PNG",                        # not a JPEG at all
    b"\xff\xd8\x00\x00\x00\x00",        # garbage where a marker should be
    b"\xff\xd8\xff\xd9",                # ends before any frame header
])
def test_jpeg_size_without_a_frame_header(data):
    assert pikvm.jpeg_size(data) is None


def test_snapshot_that_is_not_a_jpeg(kvmd, backend):
    kvmd.snapshot_data = b"not a picture"
    snapshot = backend.snapshot()
    assert snapshot.data == b"not a picture"
    assert snapshot.width is None


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------

def test_type_text(kvmd, backend):
    backend.type_text("root\nécho")
    (request,) = kvmd.calls("hid/print")
    assert request.body == "root\nécho".encode()
    # limit=0 lifts kvmd's limit of 1024 characters.
    assert request.query == {"limit": "0"}


def test_type_text_with_a_keymap(tmp_path, kvmd):
    backend = make_backend(tmp_path, kvmd.url, keymap="de")
    backend.type_text("x")
    assert kvmd.calls("hid/print")[0].query == {"limit": "0", "keymap": "de"}


def test_press(kvmd, backend):
    backend.press([[keys.LEFT_CTRL, keys.LEFT_ALT, 0x4C], [0x3B]])
    events = [(request.query["key"], request.query["state"])
              for request in kvmd.calls("hid/events/send_key")]
    # Pressed in order, released in reverse.
    assert events == [
        ("ControlLeft", "1"), ("AltLeft", "1"), ("Delete", "1"),
        ("Delete", "0"), ("AltLeft", "0"), ("ControlLeft", "0"),
        ("F2", "1"), ("F2", "0"),
    ]


def test_key_down_and_up(kvmd, backend):
    backend.key_down(keys.LEFT_SHIFT)
    backend.key_up(keys.LEFT_SHIFT)
    states = [request.query for request in kvmd.calls("hid/events/send_key")]
    assert states == [{"key": "ShiftLeft", "state": "1"}, {"key": "ShiftLeft", "state": "0"}]


def test_key_without_a_kvmd_name(backend):
    with pytest.raises(Unsupported, match="no browser key name"):
        backend.key_down(0x32)


# ----------------------------------------------------------------------
# Mouse
# ----------------------------------------------------------------------

@pytest.mark.parametrize("position, size, scaled", [
    (0, 1920, -32768),
    (1919, 1920, 32767),
    (5000, 1920, 32767),
    (-3, 1920, -32768),
    (0, 1, -32768),
])
def test_to_mouse_range(position, size, scaled):
    assert pikvm.to_mouse_range(position, size) == scaled


def test_to_mouse_range_middle():
    assert abs(pikvm.to_mouse_range(960, 1921)) <= 1


def test_mouse_move_uses_the_screen_size(kvmd, backend):
    kvmd.signal = (1000, 500)
    backend.mouse_move(999, 0)
    (request,) = kvmd.calls("hid/events/send_mouse_move")
    assert request.query == {"to_x": "32767", "to_y": "-32768"}
    # The streamer was stopped, so the size needed watching for.
    assert kvmd.watch_count == 1


def test_moves_do_not_watch_once_the_size_is_known(kvmd, backend):
    # Watching makes kvmd release held buttons, so only the first move,
    # which has to learn the screen size, may do it.
    kvmd.signal = (1000, 500)
    for x in (10, 20, 30):
        backend.mouse_move(x, 10)
    assert kvmd.watch_count == 1
    assert len(kvmd.calls("hid/events/send_mouse_move")) == 3


def test_a_snapshot_teaches_the_size(kvmd, backend):
    kvmd.signal = (1000, 500)
    backend.snapshot()
    backend.mouse_move(999, 0)
    assert kvmd.watch_count == 1
    assert kvmd.calls("hid/events/send_mouse_move")[0].query == {"to_x": "32767",
                                                                  "to_y": "-32768"}


def test_the_size_follows_a_running_streamer(kvmd, backend):
    kvmd.signal = (1000, 500)
    backend.mouse_move(1, 1)
    # The target changes mode; the streamer, now running, says so.
    kvmd.stream_forever = True
    kvmd.signal = (100, 100)
    backend.mouse_move(99, 99)
    assert kvmd.calls("hid/events/send_mouse_move")[-1].query == {"to_x": "32767",
                                                                   "to_y": "32767"}


def test_mouse_move_while_the_streamer_runs(kvmd, backend):
    kvmd.stream_forever = True
    kvmd.signal = (100, 100)
    backend.mouse_move(99, 99)
    assert kvmd.calls("hid/events/send_mouse_move")[0].query == {"to_x": "32767",
                                                                  "to_y": "32767"}
    assert kvmd.watch_count == 0


def test_mouse_move_without_a_signal(kvmd, backend):
    kvmd.signal = None
    with pytest.raises(BackendError, match="no video signal"):
        backend.mouse_move(1, 1)
    assert kvmd.calls("hid/events/send_mouse_move") == []


def test_mouse_move_when_the_screen_size_is_unknown(kvmd, backend, monkeypatch):
    monkeypatch.setattr(backend, "_source", lambda: {"online": True, "resolution": {}})
    with pytest.raises(BackendError, match="pixel positions cannot be placed"):
        backend.mouse_move(1, 1)


def test_double_click_at_a_position(kvmd, backend, monkeypatch):
    monkeypatch.setattr(pikvm, "CLICK_INTERVAL", 0)
    backend.mouse_click("right", 2, 10, 20)
    assert len(kvmd.calls("hid/events/send_mouse_move")) == 1
    # Without a state, kvmd presses and releases.
    buttons = [request.query for request in kvmd.calls("hid/events/send_mouse_button")]
    assert buttons == [{"button": "right"}, {"button": "right"}]


def test_click_where_the_pointer_is(kvmd, backend):
    backend.mouse_click("left", 1, None, None)
    assert kvmd.calls("hid/events/send_mouse_move") == []
    assert len(kvmd.calls("hid/events/send_mouse_button")) == 1


def test_mouse_down_and_up(kvmd, backend):
    backend.mouse_down("middle")
    backend.mouse_up("middle")
    states = [request.query for request in kvmd.calls("hid/events/send_mouse_button")]
    assert states == [{"button": "middle", "state": "1"}, {"button": "middle", "state": "0"}]


# kvmd releases every held key and button when a WebSocket client leaves,
# and koid watches through a WebSocket while it needs video.

def watching_finished(kvmd):
    """Wait until the fake has seen every WebSocket client leave, so that
    its releasing held input cannot come after the test looks."""
    deadline = time.monotonic() + 5
    while kvmd.watches_ended < kvmd.watch_count:
        assert time.monotonic() < deadline, "a WebSocket never closed"
        time.sleep(0.01)
    return kvmd

def test_a_held_button_survives_a_snapshot(kvmd, backend):
    backend.mouse_down("left")
    backend.snapshot()
    watching_finished(kvmd)
    assert kvmd.held_buttons == {"left"}


def test_a_held_key_survives_a_snapshot(kvmd, backend):
    backend.key_down(keys.LEFT_SHIFT)
    backend.key_down(keys.LEFT_CTRL)
    backend.snapshot()
    watching_finished(kvmd)
    assert kvmd.held_keys == {"ShiftLeft", "ControlLeft"}


def test_a_held_button_survives_the_first_move(kvmd, backend):
    # The move that learns the screen size has to watch.
    backend.mouse_down("left")
    backend.mouse_move(10, 10)
    assert kvmd.watch_count == 1
    watching_finished(kvmd)
    assert kvmd.held_buttons == {"left"}


def test_released_input_is_not_pressed_again(kvmd, backend):
    backend.mouse_down("left")
    backend.mouse_up("left")
    backend.key_down(keys.LEFT_SHIFT)
    backend.press([[keys.LEFT_SHIFT, 0x04]])
    backend.mouse_down("right")
    backend.mouse_click("right", 1, None, None)
    before = len(kvmd.requests)
    backend.snapshot()
    pressed = [request for request in kvmd.requests[before:]
               if request.query.get("state") == "1"]
    assert pressed == []
    watching_finished(kvmd)
    assert (kvmd.held_keys, kvmd.held_buttons) == (set(), set())


def test_nothing_held_means_no_waiting(kvmd, backend, monkeypatch):
    monkeypatch.setattr(pikvm, "RELEASE_SETTLE", 5.0)
    start = time.monotonic()
    backend.snapshot()
    assert time.monotonic() - start < 2


def test_pressing_again_can_fail_quietly(kvmd, backend, monkeypatch, caplog):
    backend.mouse_down("left")
    real_json = backend._json

    def failing(method, path, query=None, **kwargs):
        if path == "hid/events/send_mouse_button":
            raise pikvm.BackendError("gone")
        return real_json(method, path, query, **kwargs)

    monkeypatch.setattr(backend, "_json", failing)
    backend.snapshot()
    assert "could not press held keys and buttons again" in caplog.text


@pytest.mark.parametrize("notches, steps", [
    (3, ["3"]),
    (-2, ["-2"]),
    (300, ["127", "127", "46"]),
    (0, []),
])
def test_scroll_in_steps(kvmd, backend, notches, steps):
    backend.mouse_scroll(notches)
    sent = [request.query["delta_y"] for request in kvmd.calls("hid/events/send_mouse_wheel")]
    assert sent == steps


# ----------------------------------------------------------------------
# USB storage
# ----------------------------------------------------------------------

def stored_name(image: pathlib.Path) -> str:
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    return "koid-%s-disk_image.img" % digest[:12]


def test_insert_a_stick(kvmd, backend, image):
    status = backend.storage_insert(image, "stick", False)
    name = stored_name(image)
    assert status == {"inserted": True, "image": name, "mode": "stick", "writable": False}
    assert kvmd.uploads[name] == image.read_bytes()
    assert kvmd.calls("msd/set_params")[0].query == {"image": name, "cdrom": "0", "rw": "0"}


def test_insert_waits_for_kvmd_to_list_the_upload(kvmd, backend, image):
    kvmd.unlisted_reads = 3
    backend.storage_insert(image, "stick", False)
    assert len(kvmd.calls("msd/set_params")) == 1


def test_an_upload_kvmd_never_lists(kvmd, backend, image, monkeypatch):
    monkeypatch.setattr(pikvm, "IMAGE_WAIT", 0.1)
    monkeypatch.setattr(pikvm, "IMAGE_POLL_INTERVAL", 0.02)
    kvmd.unlisted_reads = 1000
    with pytest.raises(BackendError, match="did not list koid-.* after it was uploaded"):
        backend.storage_insert(image, "stick", False)
    assert kvmd.calls("msd/set_params") == []


def test_a_cd_too_small_for_the_pikvm(kvmd, backend, tmp_path):
    small = tmp_path / "small.iso"
    small.write_bytes(b"\x00" * (pikvm.MIN_CD_SIZE - 1))
    with pytest.raises(InvalidRequest, match="too small to be a CD.*600 KiB"):
        backend.storage_insert(small, "cdrom", False)
    assert kvmd.uploads == {}


def test_insert_a_cd(kvmd, backend, image):
    status = backend.storage_insert(image, "cdrom", False)
    assert status["mode"] == "cdrom"
    assert kvmd.drive["cdrom"] is True


def test_insert_a_writable_stick(kvmd, backend, image):
    assert backend.storage_insert(image, "stick", True)["writable"] is True
    assert kvmd.drive["rw"] is True


def test_an_image_already_on_the_pikvm_is_not_uploaded_again(kvmd, backend, image):
    backend.storage_insert(image, "stick", False)
    backend.storage_insert(image, "cdrom", False)
    assert len(kvmd.calls("msd/write")) == 1


def test_a_changed_image_is_uploaded_again(kvmd, backend, image):
    backend.storage_insert(image, "stick", False)
    image.write_bytes(b"\x01" * 600)
    backend.storage_insert(image, "stick", False)
    assert len(kvmd.calls("msd/write")) == 2


def test_insert_disconnects_the_drive_first(kvmd, backend, image):
    backend.storage_insert(image, "stick", False)
    # The fake refuses set_params while connected, as kvmd does.
    backend.storage_insert(image, "cdrom", False)
    connects = [request.query["connected"] for request in kvmd.calls("msd/set_connected")]
    assert connects == ["1", "0", "1"]


def test_an_image_too_big_for_a_cd(kvmd, backend, image):
    kvmd.cdrom_limit = 100
    with pytest.raises(BackendError, match="would not present disk image.img as a cdrom"):
        backend.storage_insert(image, "cdrom", False)


def test_a_drive_that_does_not_connect(kvmd, backend, image):
    kvmd.ignore_connect = True
    with pytest.raises(BackendError, match="did not connect"):
        backend.storage_insert(image, "stick", False)


def test_a_drive_that_stays_read_only(kvmd, backend, image):
    kvmd.refuse_writable = True
    with pytest.raises(BackendError, match="would not make disk image.img writable"):
        backend.storage_insert(image, "stick", True)


def test_missing_image(backend, tmp_path):
    with pytest.raises(BackendError, match="cannot read"):
        backend.storage_insert(tmp_path / "missing.img", "stick", False)


def test_storage_disabled(kvmd, backend, image):
    kvmd.msd_enabled = False
    with pytest.raises(Unsupported, match="no mass storage drive"):
        backend.storage_insert(image, "stick", False)
    with pytest.raises(Unsupported):
        backend.storage_status()


def test_eject(kvmd, backend, image):
    backend.storage_insert(image, "stick", False)
    backend.storage_eject()
    assert kvmd.drive["connected"] is False
    assert backend.storage_status()["inserted"] is False


def test_eject_when_nothing_is_inserted(kvmd, backend):
    backend.storage_eject()
    assert kvmd.calls("msd/set_connected") == []


def test_status_with_nothing_inserted(backend):
    assert backend.storage_status() == {"inserted": False, "image": None, "mode": "stick",
                                        "writable": False}


def test_image_names_are_safe():
    name = pikvm.image_name(pathlib.Path("/x/My Disk (1).iso"), "abcdef0123456789")
    assert name == "koid-abcdef012345-My_Disk__1_.iso"
