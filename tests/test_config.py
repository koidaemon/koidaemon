# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pathlib
import textwrap

import pytest

from koidaemon import config
from koidaemon.backends.pikvm import PiKVMBackend
from koidaemon.backends.raritan_legacy import RaritanLegacyBackend
from koidaemon.errors import ConfigError

RARITAN = """
[targets.board-a]
backend = "raritan-legacy"
host = "192.0.2.10"
port = 3
password_file = "raritan.secret"
"""


def write_config(directory: pathlib.Path, text: str) -> pathlib.Path:
    (directory / "raritan.secret").write_text("raritan\n")
    (directory / "pikvm.secret").write_text("admin-password\n")
    path = directory / "koid.toml"
    path.write_text(textwrap.dedent(text))
    return path


def test_targets_and_defaults(tmp_path):
    text = RARITAN + """
    [targets.board-b]
    backend = "pikvm"
    url = "https://192.0.2.20/"
    password_file = "pikvm.secret"
    verify_tls = false
    """
    loaded = config.load(write_config(tmp_path, text))

    assert loaded.socket == config.DEFAULT_SOCKET
    assert loaded.socket_mode == 0o660
    assert loaded.socket_group is None
    assert not loaded.http.enabled

    raritan = loaded.targets["board-a"]
    assert isinstance(raritan, RaritanLegacyBackend)
    assert (raritan.host, raritan.port, raritan.user) == ("192.0.2.10", 3, "admin")
    # The password comes from the file, relative to the configuration.
    assert raritan.password == "raritan"

    pikvm = loaded.targets["board-b"]
    assert isinstance(pikvm, PiKVMBackend)
    assert pikvm.url == "https://192.0.2.20"
    assert pikvm.password == "admin-password"


def test_daemon_settings(tmp_path):
    text = """
    [daemon]
    socket = "/tmp/koid-test.sock"
    socket_mode = "0600"
    socket_group = "koid"
    """ + RARITAN
    loaded = config.load(write_config(tmp_path, text))
    assert loaded.socket == pathlib.Path("/tmp/koid-test.sock")
    assert loaded.socket_mode == 0o600
    assert loaded.socket_group == "koid"


def test_relative_socket_path(tmp_path):
    text = """
    [daemon]
    socket = "koid.sock"
    """ + RARITAN
    loaded = config.load(write_config(tmp_path, text))
    assert loaded.socket == tmp_path.resolve() / "koid.sock"


@pytest.mark.parametrize("text, message", [
    ("[daemon]\nsocket_mode = 660\n" + RARITAN, "octal digits in quotes"),
    ("[daemon]\nport = 1\n" + RARITAN, r"\[daemon\]: unknown settings port"),
    ("[extras]\n" + RARITAN, "unknown sections extras"),
    ("[daemon]\n", "defines no targets"),
    ("[targets.a]\nhost = 'x'\n", "needs a backend"),
    ("[targets]\na = 1\n", "targets.a must be a table"),
    ("[targets.a]\nbackend = 'vnc'\n", "unknown backend 'vnc'"),
    ("[targets.'bad name']\nbackend = 'pikvm'\n", "target name 'bad name'"),
    (RARITAN + "colour = 'red'\n", "unknown settings colour"),
    (RARITAN.replace("port = 3", "port = '3'"), "port must be a number"),
    (RARITAN.replace("port = 3", "port = true"), "port must be a number"),
    (RARITAN.replace('host = "192.0.2.10"', ""), "needs host"),
    (RARITAN.replace("raritan.secret", "missing.secret"), "cannot read .*missing.secret"),
    ("[targets.b]\nbackend = 'pikvm'\nurl = 'pikvm.lab'\npassword_file = 'pikvm.secret'\n",
     "must start with https://"),
    ("this is = not toml [", "koid.toml"),
])
def test_configuration_errors(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        config.load(write_config(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        config.load(tmp_path / "nope.toml")


def test_http_api(tmp_path):
    (tmp_path / "token").write_text("s3cret\n")
    text = """
    [http]
    enabled = true
    listen = "0.0.0.0:9000"
    token_file = "token"
    cert_file = "koid.pem"
    key_file = "koid.key"
    """ + RARITAN
    http = config.load(write_config(tmp_path, text)).http
    assert http.enabled
    assert http.listen == "0.0.0.0:9000"
    assert http.token == "s3cret"
    assert http.cert_file == tmp_path / "koid.pem"
    assert http.key_file == tmp_path / "koid.key"


def test_disabled_http_api_needs_nothing(tmp_path):
    http = config.load(write_config(tmp_path, "[http]\nenabled = false\n" + RARITAN)).http
    assert not http.enabled
    assert http.token == ""


@pytest.mark.parametrize("http_table, message", [
    ("enabled = true", "needs a token_file"),
    ("enabled = true\ntoken_file = 'missing'", "cannot read"),
    ("enabled = true\ntoken_file = 'empty'", "is empty"),
    ("enabled = true\ntoken_file = 'token'\ncert_file = 'koid.pem'", "both cert_file and key_file"),
    ("enabled = 'yes'", r"\[http\]: enabled must be true or false"),
    ("enabled = true\nport = 1", r"\[http\]: unknown settings port"),
])
def test_http_api_errors(tmp_path, http_table, message):
    (tmp_path / "token").write_text("s3cret")
    (tmp_path / "empty").write_text("\n")
    text = "[http]\n" + http_table + "\n" + RARITAN
    with pytest.raises(ConfigError, match=message):
        config.load(write_config(tmp_path, text))


def test_example_configuration_is_valid(tmp_path):
    example = pathlib.Path(__file__).parent.parent / "examples" / "koid.toml"
    text = example.read_text()
    # Point the example's secrets at files that exist here.
    text = text.replace("/run/secrets/raritan", str(tmp_path / "raritan.secret"))
    text = text.replace("/run/secrets/pikvm-b", str(tmp_path / "pikvm.secret"))
    text = text.replace('ca_file = "pikvm-b.pem"', "")
    loaded = config.load(write_config(tmp_path, text))
    assert sorted(loaded.targets) == ["board-a", "board-b"]
