# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""What every backend provides.

A backend drives one target: one machine behind a KVM.  The daemon creates
one per configured target and calls its methods with arguments already
checked by koidaemon.commands, so a backend never sees malformed input.

The daemon serialises calls per resource: one KVM device, which may serve
several targets (a Raritan switch has many ports, but only one can be used
at a time).  Two calls with the same resource never run at once.

Methods for capabilities a backend lacks are never called; the defaults
here raise Unsupported in case they are.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from koidaemon.errors import ConfigError, Unsupported

# How setting types are named in error messages.
KIND_NAMES = {str: "text", int: "a number", float: "a number", bool: "true or false"}


@dataclass
class Snapshot:
    """A picture of the target's screen."""

    format: str     # "png" or "jpeg"
    data: bytes
    width: int | None = None
    height: int | None = None


class Settings:
    """A target's settings from the configuration file, read once each.

    Backends take what they need with the get_* methods; anything left over
    is a mistake in the file and reported by finish().
    """

    def __init__(self, target: str, values: dict, config_dir: pathlib.Path,
                 where: str | None = None) -> None:
        self.target = target
        # How errors name where the settings came from.
        self.where = where or "target %r" % target
        self._values = dict(values)
        self._config_dir = config_dir

    def _take(self, key: str, kind: type, default):
        if key not in self._values:
            if default is ...:
                raise ConfigError("%s needs %s" % (self.where, key))
            return default
        value = self._values.pop(key)
        if kind is int and isinstance(value, bool):
            raise ConfigError("%s: %s must be %s" % (self.where, key, KIND_NAMES[int]))
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, kind):
            raise ConfigError("%s: %s must be %s" % (self.where, key, KIND_NAMES[kind]))
        return value

    def get_str(self, key: str, default=...) -> str:
        return self._take(key, str, default)

    def get_int(self, key: str, default=...) -> int:
        return self._take(key, int, default)

    def get_float(self, key: str, default=...) -> float:
        return self._take(key, float, default)

    def get_bool(self, key: str, default=...) -> bool:
        return self._take(key, bool, default)

    def get_path(self, key: str, default=...) -> pathlib.Path | None:
        """A file path; relative paths are relative to the configuration file."""
        value = self.get_str(key, default)
        if value is None:
            return None
        return self._config_dir / pathlib.Path(value).expanduser()

    def get_password(self, key: str = "password_file") -> str:
        """Read a password from the file named by key.

        Passwords never go in the configuration file itself, which is
        typically readable by more people than the secret should be.
        """
        path = self.get_path(key)
        try:
            return path.read_text().strip()
        except OSError as error:
            raise ConfigError("%s: cannot read %s: %s"
                              % (self.where, path, error.strerror)) from error

    def finish(self) -> None:
        """Complain about settings nobody asked for."""
        if self._values:
            raise ConfigError("%s: unknown settings %s"
                              % (self.where, ", ".join(sorted(self._values))))


class Backend:
    """Base class for backends.  Subclasses set name and capabilities."""

    name = ""
    capabilities: frozenset = frozenset()

    def __init__(self, target: str, settings: Settings) -> None:
        self.target = target

    @property
    def resource(self) -> str:
        """The KVM device this target uses; calls are serialised per resource."""
        return "%s:%s" % (self.name, self.target)

    def describe(self) -> dict:
        """Static facts about the target, for the targets command."""
        return {
            "backend": self.name,
            "capabilities": sorted(self.capabilities),
        }

    def close(self) -> None:
        """Release anything held between calls."""

    # Every backend answers status.
    def status(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> Snapshot:
        raise Unsupported("%s cannot take snapshots" % self.name)

    def type_text(self, text: str) -> None:
        raise Unsupported("%s has no keyboard" % self.name)

    def press(self, combinations: list[list[int]]) -> None:
        raise Unsupported("%s has no keyboard" % self.name)

    def key_down(self, usage: int) -> None:
        raise Unsupported("%s has no keyboard" % self.name)

    def key_up(self, usage: int) -> None:
        raise Unsupported("%s has no keyboard" % self.name)

    def mouse_move(self, x: int, y: int) -> None:
        raise Unsupported("%s has no mouse" % self.name)

    def mouse_click(self, button: str, count: int, x: int | None, y: int | None) -> None:
        raise Unsupported("%s has no mouse" % self.name)

    def mouse_down(self, button: str) -> None:
        raise Unsupported("%s has no mouse" % self.name)

    def mouse_up(self, button: str) -> None:
        raise Unsupported("%s has no mouse" % self.name)

    def mouse_scroll(self, notches: int) -> None:
        raise Unsupported("%s has no mouse" % self.name)

    def storage_insert(self, image: pathlib.Path, mode: str, writable: bool) -> dict:
        raise Unsupported("%s cannot emulate USB storage" % self.name)

    def storage_eject(self) -> None:
        raise Unsupported("%s cannot emulate USB storage" % self.name)

    def storage_status(self) -> dict:
        raise Unsupported("%s cannot emulate USB storage" % self.name)
