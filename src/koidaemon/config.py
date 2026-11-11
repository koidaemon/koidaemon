# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""koid's configuration file.

A TOML file with an optional [daemon] table and one [targets.NAME] table
per machine:

    [daemon]
    socket = "/run/koidaemon/koid.sock"     # default
    socket_mode = "0660"                    # default
    socket_group = "koid"                   # optional

    [http]                                  # optional; see httpapi.py
    enabled = false                         # default

    [targets.board-a]
    backend = "raritan-legacy"
    ...

Each target's other settings belong to its backend; see the backend modules.
Relative paths are relative to the configuration file.
"""

from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass, field

from koidaemon.backends import backend_class
from koidaemon.backends.base import Backend, Settings
from koidaemon.errors import ConfigError

DEFAULT_CONFIG = pathlib.Path("/etc/koidaemon/koid.toml")
DEFAULT_SOCKET = pathlib.Path("/run/koidaemon/koid.sock")
DEFAULT_SOCKET_MODE = 0o660
DEFAULT_HTTP_LISTEN = "127.0.0.1:8470"

TARGET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class HTTPConfig:
    enabled: bool = False
    listen: str = DEFAULT_HTTP_LISTEN
    token: str = ""
    cert_file: pathlib.Path | None = None
    key_file: pathlib.Path | None = None


@dataclass
class Config:
    socket: pathlib.Path = DEFAULT_SOCKET
    socket_mode: int = DEFAULT_SOCKET_MODE
    socket_group: str | None = None
    http: HTTPConfig = field(default_factory=HTTPConfig)
    targets: dict[str, Backend] = field(default_factory=dict)


def _socket_mode(value) -> int:
    """socket_mode as a string of octal digits, e.g. "0660"."""
    if not isinstance(value, str) or not re.fullmatch(r"0?[0-7]{3}", value):
        raise ConfigError('socket_mode must be octal digits in quotes, such as "0660"')
    return int(value, 8)


def _daemon_settings(config: Config, table: dict, config_dir: pathlib.Path) -> None:
    table = dict(table)
    if "socket" in table:
        config.socket = config_dir / table.pop("socket")
    if "socket_mode" in table:
        config.socket_mode = _socket_mode(table.pop("socket_mode"))
    if "socket_group" in table:
        config.socket_group = str(table.pop("socket_group"))
    if table:
        raise ConfigError("[daemon]: unknown settings %s" % ", ".join(sorted(table)))


def _http_settings(config: Config, table: dict, config_dir: pathlib.Path) -> None:
    # The [http] table is read like a target's settings, for the same type
    # checks and relative paths.
    settings = Settings("http", table, config_dir, where="[http]")
    http = config.http
    http.enabled = settings.get_bool("enabled", False)
    http.listen = settings.get_str("listen", DEFAULT_HTTP_LISTEN)
    token_file = settings.get_path("token_file", None)
    http.cert_file = settings.get_path("cert_file", None)
    http.key_file = settings.get_path("key_file", None)
    settings.finish()

    if not http.enabled:
        return
    if token_file is None:
        raise ConfigError("[http]: an enabled HTTP API needs a token_file")
    try:
        http.token = token_file.read_text().strip()
    except OSError as error:
        raise ConfigError("[http]: cannot read %s: %s" % (token_file, error.strerror)) from error
    if not http.token:
        raise ConfigError("[http]: %s is empty" % token_file)
    if (http.cert_file is None) != (http.key_file is None):
        raise ConfigError("[http]: give both cert_file and key_file, or neither")


def _target(name: str, table: dict, config_dir: pathlib.Path) -> Backend:
    if not TARGET_NAME.match(name):
        raise ConfigError("target name %r: use letters, digits, '.', '_' and '-'" % name)
    if not isinstance(table, dict):
        raise ConfigError("targets.%s must be a table" % name)

    values = dict(table)
    if "backend" not in values:
        raise ConfigError("target %r needs a backend" % name)
    cls = backend_class(values.pop("backend"))
    return cls(name, Settings(name, values, config_dir))


def load(path: pathlib.Path) -> Config:
    """Read the configuration and create a backend for every target."""
    try:
        with open(path, "rb") as config_file:
            document = tomllib.load(config_file)
    except OSError as error:
        raise ConfigError("cannot read %s: %s" % (path, error.strerror)) from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError("%s: %s" % (path, error)) from error

    config = Config()
    config_dir = path.resolve().parent
    document = dict(document)
    _daemon_settings(config, document.pop("daemon", {}), config_dir)
    _http_settings(config, document.pop("http", {}), config_dir)

    targets = document.pop("targets", {})
    if document:
        raise ConfigError("unknown sections %s" % ", ".join(sorted(document)))
    if not targets:
        raise ConfigError("%s defines no targets" % path)

    for name, table in targets.items():
        config.targets[name] = _target(name, table, config_dir)
    return config
