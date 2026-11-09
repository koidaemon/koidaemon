# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pathlib

import pytest

from koidaemon.backends.base import Backend, Settings
from koidaemon.errors import ConfigError, Unsupported


class MinimalBackend(Backend):
    """A backend that can do nothing but report its status."""

    name = "minimal"

    def status(self):
        return {"online": True}


@pytest.mark.parametrize("method, args", [
    ("snapshot", ()),
    ("type_text", ("x",)),
    ("press", ([[4]],)),
    ("key_down", (4,)),
    ("key_up", (4,)),
    ("mouse_move", (1, 2)),
    ("mouse_click", ("left", 1, None, None)),
    ("mouse_down", ("left",)),
    ("mouse_up", ("left",)),
    ("mouse_scroll", (1,)),
    ("storage_insert", (pathlib.Path("/a.img"), "stick", False)),
    ("storage_eject", ()),
    ("storage_status", ()),
])
def test_missing_methods_are_unsupported(method, args):
    backend = MinimalBackend("board", None)
    with pytest.raises(Unsupported, match="minimal"):
        getattr(backend, method)(*args)


def test_defaults():
    backend = MinimalBackend("board", None)
    assert backend.resource == "minimal:board"
    assert backend.describe() == {"backend": "minimal", "capabilities": []}
    backend.close()


def settings(values, tmp_path=pathlib.Path("/etc/koidaemon")):
    return Settings("board", values, tmp_path)


def test_typed_settings():
    values = {"name": "x", "count": 3, "ratio": 2, "flag": True}
    board = settings(values)
    assert board.get_str("name") == "x"
    assert board.get_int("count") == 3
    # A whole number is fine where a float is wanted.
    assert board.get_float("ratio") == 2.0
    assert board.get_bool("flag") is True
    assert board.get_str("missing", "default") == "default"
    board.finish()


def test_paths_are_relative_to_the_configuration():
    board = settings({"relative": "keys/a.pem", "absolute": "/srv/b.pem"})
    assert board.get_path("relative") == pathlib.Path("/etc/koidaemon/keys/a.pem")
    assert board.get_path("absolute") == pathlib.Path("/srv/b.pem")
    assert board.get_path("missing", None) is None


@pytest.mark.parametrize("values, getter, message", [
    ({}, "get_str", "target 'board' needs key"),
    ({"key": 1}, "get_str", "key must be text"),
    ({"key": "1"}, "get_int", "key must be a number"),
    ({"key": False}, "get_int", "key must be a number"),
    ({"key": "yes"}, "get_bool", "key must be true or false"),
])
def test_setting_errors(values, getter, message):
    with pytest.raises(ConfigError, match=message):
        getattr(settings(values), getter)("key")


def test_password_from_a_file(tmp_path):
    (tmp_path / "secret").write_text("hunter2\n")
    assert settings({"password_file": "secret"}, tmp_path).get_password() == "hunter2"


def test_leftover_settings_are_reported():
    with pytest.raises(ConfigError, match="unknown settings extra, more"):
        settings({"extra": 1, "more": 2}).finish()
