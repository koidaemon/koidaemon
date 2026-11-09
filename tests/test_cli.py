# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""koictl against a real daemon with a recording backend, and koid itself."""

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from koidaemon import cli, commands, config, keys
from koidaemon.client import Client
from koidaemon.daemon import Daemon
from koidaemon.errors import BackendError
from koidaemon.httpapi import HTTPApi

from fakes import FAKE_PNG, RecordingBackend, make_certificate, short_socket_path


@pytest.fixture
def backend():
    return RecordingBackend()


@pytest.fixture
def socket_path(backend, monkeypatch):
    monkeypatch.delenv("KOI_TARGET", raising=False)
    monkeypatch.delenv("KOID_URL", raising=False)
    daemon = Daemon({"board": backend, "keys-only": RecordingBackend(
        "keys-only", capabilities={commands.KEYBOARD})})
    path = short_socket_path()
    daemon.listen(path)
    thread = threading.Thread(target=daemon.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    yield path
    daemon.shutdown()
    daemon.close()


def koictl(socket_path, *argv, target="board"):
    args = ["--socket", str(socket_path)]
    if target:
        args += ["-t", target]
    return cli.koictl_main(args + list(argv))


@pytest.mark.parametrize("argv, call", [
    (["keyboard", "type", "root"], ("type_text", "root")),
    (["keyboard", "type", "--enter", "root"], ("type_text", "root\n")),
    (["keyboard", "press", "f2", "ctrl+alt+delete"],
     ("press", [[0x3B], [keys.LEFT_CTRL, keys.LEFT_ALT, 0x4C]])),
    (["keyboard", "down", "shift"], ("key_down", keys.LEFT_SHIFT)),
    (["keyboard", "up", "shift"], ("key_up", keys.LEFT_SHIFT)),
    (["mouse", "move", "640", "400"], ("mouse_move", 640, 400)),
    (["mouse", "click"], ("mouse_click", "left", 1, None, None)),
    (["mouse", "click", "--button", "right", "--count", "2", "--at", "5", "6"],
     ("mouse_click", "right", 2, 5, 6)),
    (["mouse", "down", "--button", "middle"], ("mouse_down", "middle")),
    (["mouse", "up"], ("mouse_up", "left")),
    (["mouse", "scroll", "-3"], ("mouse_scroll", -3)),
    (["storage", "eject"], ("storage_eject",)),
])
def test_commands(socket_path, backend, argv, call):
    assert koictl(socket_path, *argv) == 0
    assert backend.calls == [call]


def test_storage_insert_sends_an_absolute_path(socket_path, backend, monkeypatch, tmp_path,
                                               capsys):
    monkeypatch.chdir(tmp_path)
    assert koictl(socket_path, "storage", "insert", "--cdrom", "installer.iso") == 0
    assert backend.calls == [("storage_insert", tmp_path / "installer.iso", "cdrom", False)]
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "cdrom"


def test_writable_stick(socket_path, backend):
    assert koictl(socket_path, "storage", "insert", "--writable", "/images/disk.img") == 0
    assert backend.calls == [("storage_insert", pathlib.Path("/images/disk.img"), "stick", True)]


def test_results_are_printed_as_json(socket_path, capsys):
    assert koictl(socket_path, "status") == 0
    assert json.loads(capsys.readouterr().out)["online"] is True

    assert koictl(socket_path, "targets", target=None) == 0
    targets = json.loads(capsys.readouterr().out)["targets"]
    assert sorted(targets) == ["board", "keys-only"]

    assert koictl(socket_path, "storage", "status") == 0
    assert json.loads(capsys.readouterr().out)["inserted"] is False


def test_commands_without_a_result_print_nothing(socket_path, capsys):
    assert koictl(socket_path, "keyboard", "type", "x") == 0
    assert capsys.readouterr().out == ""


def test_snapshot_to_a_file(socket_path, tmp_path, capsys):
    output = tmp_path / "screen.png"
    assert koictl(socket_path, "snapshot", str(output)) == 0
    assert output.read_bytes() == FAKE_PNG
    assert capsys.readouterr().err == "saved png 640x480 to %s\n" % output


def test_snapshot_to_standard_output(socket_path, capsysbinary):
    assert koictl(socket_path, "snapshot", "-") == 0
    assert capsysbinary.readouterr().out == FAKE_PNG


def test_target_from_the_environment(socket_path, backend, monkeypatch):
    monkeypatch.setenv("KOI_TARGET", "board")
    assert koictl(socket_path, "keyboard", "type", "x", target=None) == 0
    assert backend.calls == [("type_text", "x")]


def test_a_target_is_needed(socket_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        koictl(socket_path, "status", target=None)
    assert exit_info.value.code == 2
    assert "give a target" in capsys.readouterr().err


def test_unsupported_exits_with_3(socket_path, capsys):
    assert koictl(socket_path, "mouse", "move", "1", "1", target="keys-only") == 3
    assert "keys-only (fake) cannot do mouse.move" in capsys.readouterr().err


def test_failures_exit_with_1(socket_path, backend, capsys):
    backend.error = BackendError("the KVM is on fire")
    assert koictl(socket_path, "keyboard", "type", "x") == 1
    assert "the KVM is on fire" in capsys.readouterr().err

    assert koictl(socket_path, "status", target="nobody") == 1


def test_bad_arguments_from_the_daemon_exit_with_1(socket_path, capsys):
    assert koictl(socket_path, "keyboard", "press", "ctrl+banana") == 1
    assert "unknown key name" in capsys.readouterr().err


def test_no_daemon(tmp_path, capsys):
    assert cli.koictl_main(["--socket", str(tmp_path / "none.sock"), "targets"]) == 1
    assert "cannot connect to koid" in capsys.readouterr().err


def test_over_http(backend, tmp_path, monkeypatch):
    monkeypatch.delenv("KOID_URL", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("s3cret\n")
    http_api = HTTPApi(Daemon({"board": backend}), "127.0.0.1:0", "s3cret")
    http_api.start(0.02)
    try:
        url = "http://127.0.0.1:%d" % http_api.address[1]
        argv = ["--url", url, "--token-file", str(token_file), "-t", "board",
                "keyboard", "type", "over http"]
        assert cli.koictl_main(argv) == 0
    finally:
        http_api.stop()
    assert backend.calls == [("type_text", "over http")]


def test_http_needs_a_token_file(monkeypatch, capsys):
    monkeypatch.delenv("KOID_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit):
        cli.koictl_main(["--url", "http://127.0.0.1:1", "targets"])
    assert "needs --token-file" in capsys.readouterr().err


def test_http_token_file_must_exist(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.koictl_main(["--url", "http://127.0.0.1:1", "--token-file",
                         str(tmp_path / "missing"), "targets"])
    assert "cannot read" in capsys.readouterr().err


# ----------------------------------------------------------------------
# koid
# ----------------------------------------------------------------------

def write_config(directory: pathlib.Path, socket_path: pathlib.Path,
                 http: str = "") -> pathlib.Path:
    (directory / "secret").write_text("raritan")
    path = directory / "koid.toml"
    path.write_text(textwrap.dedent("""
        [daemon]
        socket = "%s"

        [targets.board-a]
        backend = "raritan-legacy"
        host = "192.0.2.10"
        port = 1
        password_file = "secret"
        """ % socket_path) + textwrap.dedent(http))
    return path


def start_koid(config_file: pathlib.Path, socket_path: pathlib.Path) -> subprocess.Popen:
    """Run koid in its own process, and wait until it is listening."""
    command = [sys.executable, "-c", "from koidaemon.cli import koid; raise SystemExit(koid())",
               "-c", str(config_file), "-q"]
    process = subprocess.Popen(command)
    deadline = time.monotonic() + 10
    while not socket_path.exists():
        assert time.monotonic() < deadline, "koid never created its socket"
        assert process.poll() is None, "koid exited early"
        time.sleep(0.05)
    return process


def stop_koid(process: subprocess.Popen) -> int:
    os.kill(process.pid, signal.SIGTERM)
    return process.wait(timeout=10)


def test_koid_check(tmp_path, capsys):
    config_file = write_config(tmp_path, tmp_path / "koid.sock")
    assert cli.koid_main(["-c", str(config_file), "--check"]) == 0
    assert capsys.readouterr().out == "board-a: raritan-legacy\n"


def test_koid_with_a_bad_configuration(tmp_path, capsys):
    config_file = tmp_path / "koid.toml"
    config_file.write_text("[daemon]\n")
    assert cli.koid_main(["-c", str(config_file)]) == 1
    assert "defines no targets" in capsys.readouterr().err


def test_koid_runs_and_stops_cleanly(tmp_path):
    socket_path = short_socket_path()
    process = start_koid(write_config(tmp_path, socket_path), socket_path)
    try:
        assert cli.koictl_main(["--socket", str(socket_path), "targets"]) == 0
    finally:
        assert stop_koid(process) == 0
    assert not socket_path.exists()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_koid_with_the_http_api(tmp_path, capsys):
    (tmp_path / "token").write_text("s3cret")
    port = free_port()
    http = """
        [http]
        enabled = true
        listen = "127.0.0.1:%d"
        token_file = "token"
        """ % port
    socket_path = short_socket_path()
    process = start_koid(write_config(tmp_path, socket_path, http), socket_path)
    try:
        argv = ["--url", "http://127.0.0.1:%d" % port, "--token-file", str(tmp_path / "token"),
                "targets"]
        assert cli.koictl_main(argv) == 0
    finally:
        assert stop_koid(process) == 0
    assert "board-a" in json.loads(capsys.readouterr().out)["targets"]


def test_koid_refuses_an_http_port_in_use(tmp_path, capsys):
    (tmp_path / "token").write_text("s3cret")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        http = """
            [http]
            enabled = true
            listen = "127.0.0.1:%d"
            token_file = "token"
            """ % taken.getsockname()[1]
        config_file = write_config(tmp_path, short_socket_path(), http)
        assert cli.koid_main(["-c", str(config_file), "-q"]) == 1
    assert "cannot start the HTTP API" in capsys.readouterr().err


def test_http_api_with_tls(tmp_path):
    cert_file, key_file = make_certificate(tmp_path)
    settings = config.HTTPConfig(enabled=True, listen="127.0.0.1:0", token="s3cret",
                                 cert_file=cert_file, key_file=key_file)
    backend = RecordingBackend()
    http_api = cli._start_http_api(Daemon({"board": backend}), settings)
    try:
        url = "https://127.0.0.1:%d" % http_api.address[1]
        client = Client(url=url, token="s3cret", ca_file=cert_file)
        client.call("keyboard.type", "board", text="tls")
    finally:
        http_api.stop()
    assert backend.calls == [("type_text", "tls")]
