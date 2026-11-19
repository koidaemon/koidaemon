# Copyright (c) 2026 Koen Kooi
#
# SPDX-License-Identifier: MIT

"""The jetkvm backend's parts that need no WebRTC; test_jetkvm_session.py
runs it against a fake JetKVM."""

import http.server
import json
import ssl
import threading

import pytest

from koidaemon import keys
from koidaemon.backends import backend_class, jetkvm
from koidaemon.backends.base import Settings
from koidaemon.errors import BackendError, Busy, ConfigError, InvalidRequest

CTRL, SHIFT, ALT = keys.LEFT_CTRL, keys.LEFT_SHIFT, keys.LEFT_ALT
A, ONE, ENTER, DELETE = 0x04, 0x1E, 0x28, 0x4C


def make_backend(tmp_path, url="http://jetkvm.lab", password=None, **settings):
    values = {"url": url, **settings}
    if password is not None:
        (tmp_path / "password").write_text(password + "\n")
        values["password_file"] = "password"
    return jetkvm.JetKVMBackend("board", Settings("board", values, tmp_path))


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def test_registered():
    assert backend_class("jetkvm") is jetkvm.JetKVMBackend


def test_settings(tmp_path):
    backend = make_backend(tmp_path, "https://jetkvm.lab/", password="secret",
                           timeout=5, idle_timeout=10)
    assert backend.url == "https://jetkvm.lab"
    assert backend.password == "secret"
    assert backend.timeout == 5.0
    assert backend.idle_timeout == 10.0
    assert backend.resource == "jetkvm:https://jetkvm.lab"
    assert backend.describe() == {
        "backend": "jetkvm",
        "capabilities": ["keyboard", "mouse", "snapshot", "storage.cdrom", "storage.stick"],
        "url": "https://jetkvm.lab",
    }


def test_defaults(tmp_path):
    backend = make_backend(tmp_path)
    assert backend.password is None
    assert backend.timeout == 30.0
    assert backend.idle_timeout == 60.0
    assert backend.tls.verify_mode == ssl.CERT_REQUIRED


def test_tls_check_can_be_turned_off(tmp_path):
    backend = make_backend(tmp_path, verify_tls=False)
    assert backend.tls.verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("settings, message", [
    ({"url": "jetkvm.lab"}, "url must start with"),
    ({"url": "http://jetkvm.lab", "colour": "red"}, "unknown settings colour"),
    ({"url": "http://jetkvm.lab", "timeout": 0}, "must be more than 0"),
    ({"url": "http://jetkvm.lab", "idle_timeout": -1}, "must be more than 0"),
    ({"url": "http://jetkvm.lab", "password_file": "missing"}, "cannot read"),
    ({}, "needs url"),
])
def test_bad_settings(tmp_path, settings, message):
    with pytest.raises(ConfigError, match=message):
        jetkvm.JetKVMBackend("board", Settings("board", settings, tmp_path))


def test_without_aiortc(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(jetkvm, "HAVE_WEBRTC", False)
    backend = make_backend(tmp_path)
    assert "needs aiortc and av" in caplog.text
    with pytest.raises(BackendError, match=r"pip install 'koidaemon\[jetkvm\]'"):
        backend.status()
    backend.close()


def test_closing_an_unused_backend(tmp_path):
    make_backend(tmp_path).close()


# ----------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------

@pytest.mark.parametrize("position, size, expected", [
    (0, 1920, 0),
    (1919, 1920, 32767),
    (960, 1921, 16384),
    (-5, 1920, 0),
    (5000, 1920, 32767),
    (3, 1, 0),
])
def test_mouse_range(position, size, expected):
    assert jetkvm.to_mouse_range(position, size) == expected


def test_keyboard_state():
    assert jetkvm.keyboard_state([]) == (0, bytes(6))
    assert jetkvm.keyboard_state([CTRL, A, keys.RIGHT_ALT]) == (0x41, bytes([A, 0, 0, 0, 0, 0]))
    assert jetkvm.keyboard_state([A, A, ONE]) == (0, bytes([A, ONE, 0, 0, 0, 0]))
    # A report has room for six keys.
    assert jetkvm.keyboard_state(list(range(4, 12)))[1] == bytes(range(4, 10))


def test_macro_message():
    message = jetkvm.macro_message([([SHIFT, A], 20), ([], 0)], paste=True)
    assert message == (b"\x07\x01\x00\x00\x00\x02"
                       b"\x02\x04\x00\x00\x00\x00\x00\x00\x14"
                       b"\x00\x00\x00\x00\x00\x00\x00\x00\x00")


def test_typing_text():
    assert jetkvm.text_steps("aA\n", []) == [
        ([A], 20), ([], 20),
        ([SHIFT, A], 20), ([], 20),
        ([ENTER], 20), ([], 0),
    ]


def test_typing_leaves_held_keys_held():
    assert jetkvm.text_steps("a", [CTRL])[-1] == ([CTRL], 0)


def test_typing_what_a_us_keyboard_cannot():
    with pytest.raises(InvalidRequest, match="cannot type 'é'"):
        jetkvm.text_steps("café", [])


def test_pressing_combinations():
    assert jetkvm.press_steps([[CTRL, ALT, DELETE], [ENTER]], []) == [
        ([CTRL], 20), ([CTRL, ALT], 20), ([CTRL, ALT, DELETE], 20),
        ([CTRL, ALT], 20), ([CTRL], 20), ([], 20),
        ([ENTER], 20), ([], 0),
    ]


def test_pressing_on_top_of_held_keys():
    # A held key that is pressed again is released after, as on a PiKVM.
    assert jetkvm.press_steps([[A]], [SHIFT, CTRL]) == [([SHIFT, CTRL, A], 20), ([SHIFT, CTRL], 0)]
    assert jetkvm.press_steps([[CTRL, A]], [CTRL]) == [([CTRL], 20), ([CTRL, A], 20),
                                                      ([CTRL], 20), ([], 0)]


def test_pointer_message():
    assert jetkvm.pointer_message(32767, 1, 5) == b"\x03\x00\x00\x7f\xff\x00\x00\x00\x01\x05"


# ----------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------

class LoginServer(http.server.ThreadingHTTPServer):
    """Answers every login with status and headers."""

    def __init__(self, status, headers=()):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                server.bodies.append(json.loads(body))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

        super().__init__(("127.0.0.1", 0), Handler)
        self.bodies = []
        self.url = "http://127.0.0.1:%d" % self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()


@pytest.fixture
def login_server():
    servers = []

    def start(status, headers=()):
        servers.append(LoginServer(status, headers))
        return servers[-1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_login(tmp_path, login_server):
    server = login_server(200, [("Set-Cookie", "authToken=abc123; Path=/; HttpOnly")])
    assert make_backend(tmp_path, server.url, password="secret")._login() == "abc123"
    assert server.bodies == [{"password": "secret"}]


def test_no_login_without_a_password(tmp_path):
    assert make_backend(tmp_path, "http://127.0.0.1:1")._login() is None


def test_login_in_no_password_mode(tmp_path, login_server):
    # The JetKVM refuses logins with HTTP 400 when it has no password.
    server = login_server(400)
    assert make_backend(tmp_path, server.url, password="secret")._login() is None


@pytest.mark.parametrize("status, error, message", [
    (401, BackendError, "refused the password"),
    (429, Busy, "too many login attempts"),
    (500, BackendError, "answered HTTP 500 to the login"),
    (200, BackendError, "did not give koid a session cookie"),
])
def test_login_errors(tmp_path, login_server, status, error, message):
    server = login_server(status)
    with pytest.raises(error, match=message):
        make_backend(tmp_path, server.url, password="secret")._login()


def test_login_to_nothing(tmp_path):
    backend = make_backend(tmp_path, "http://127.0.0.1:1", password="secret", timeout=2)
    with pytest.raises(BackendError, match="cannot reach http://127.0.0.1:1"):
        backend._login()
