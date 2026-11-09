# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Backend for first-generation Raritan Dominion KX switches.

These are the original Dominion KX models (KX101, KX116, KX132, KX216,
KX232, KX464 and similar), which only ever shipped a Java client.  The
modules in this package reimplement their network protocol; the Dominion
KX II and later speak a different protocol and are not supported.

A target is one port of a switch.  The switch has a single video channel,
so only one of its ports can be connected at a time: every target on the
same switch shares one resource, and koid serialises them.  Each command
logs in, connects to the port, does its job and logs out.

The switch cannot emulate USB storage, and its screenshots are untested
(see screen.py).  Configuration:

    [targets.board-a]
    backend = "raritan-legacy"
    host = "192.0.2.10"         # the switch
    port = 1                    # numbered from 1, as on the switch
    user = "admin"              # optional; default admin
    password_file = "/run/secrets/raritan"
    wait = 30                   # optional; seconds to wait while the
                                # switch is busy with another user
    key_delay = 0.03            # optional; seconds between key events
"""

from __future__ import annotations

import contextlib

from koidaemon import commands
from koidaemon.backends.base import Backend, Settings, Snapshot
from koidaemon.backends.raritan_legacy import auth
from koidaemon.backends.raritan_legacy import session as kx
from koidaemon.backends.raritan_legacy.keyboard import (DEFAULT_KEY_DELAY, Keyboard,
                                                        KeyboardError, combination_events,
                                                        text_events)
from koidaemon.backends.raritan_legacy.mouse import Mouse
from koidaemon.backends.raritan_legacy.screen import (SCREENSHOT_COMPRESSION_PARAMS,
                                                      ScreenError, ScreenGrabber)
from koidaemon.errors import BackendError, Busy, InvalidRequest

DEFAULT_WAIT = 30.0
SNAPSHOT_TIMEOUT = 30.0


class RaritanLegacyBackend(Backend):
    name = "raritan-legacy"
    capabilities = frozenset({commands.SNAPSHOT, commands.KEYBOARD, commands.MOUSE})

    def __init__(self, target: str, settings: Settings) -> None:
        super().__init__(target, settings)
        self.host = settings.get_str("host")
        self.port = settings.get_int("port")
        self.user = settings.get_str("user", "admin")
        self.password = settings.get_password()
        self.tcp_port = settings.get_int("tcp_port", kx.DEFAULT_PORT)
        self.wait = settings.get_float("wait", DEFAULT_WAIT)
        self.key_delay = settings.get_float("key_delay", DEFAULT_KEY_DELAY)
        settings.finish()

    @property
    def resource(self) -> str:
        # All ports of a switch share its single video channel.
        return "raritan-legacy:%s:%d" % (self.host, self.tcp_port)

    def describe(self) -> dict:
        description = super().describe()
        description["host"] = self.host
        description["port"] = self.port
        return description

    def _session(self) -> kx.KXSession:
        return kx.KXSession(self.host, self.user, self.password,
                            tcp_port=self.tcp_port, wait=self.wait)

    @contextlib.contextmanager
    def _switch_errors(self):
        """Turn errors from the switch into koid errors."""
        try:
            yield
        except kx.SwitchBusyError as error:
            raise Busy(str(error)) from error
        except KeyboardError as error:
            # Something the switch cannot type, or Ctrl+NumLock.
            raise InvalidRequest(str(error)) from error
        except (kx.SessionError, auth.LoginError, ScreenError, OSError) as error:
            raise BackendError("%s: %s" % (self.host, error)) from error

    def _run(self, action) -> None:
        """Log in, connect to the port, run action(session) and log out."""
        with self._switch_errors(), self._session() as session:
            session.connect_port(self.port)
            action(session)

    def _send_key_events(self, events: list[tuple[str, int]]) -> None:
        def send(session):
            Keyboard(session, self.key_delay).send_events(events)

        self._run(send)

    def status(self) -> dict:
        with self._switch_errors(), self._session() as session:
            ports = {port.number: port for port in session.list_ports()}

        if self.port not in ports:
            raise BackendError("%s has no port %d" % (self.host, self.port))
        port = ports[self.port]
        # The switch only reports the video mode once a port is connected,
        # so status does not include one.
        return {
            "online": port.active,
            "port_name": port.name,
            "video": None,
        }

    def snapshot(self) -> Snapshot:
        with self._switch_errors(), self._session() as session:
            # The grabber has to listen before connecting: the video mode
            # arrives straight after.
            grabber = ScreenGrabber(session)
            session.video_listener = grabber.handle_video_packet
            session.connect_port(self.port, compression_params=SCREENSHOT_COMPRESSION_PARAMS)
            png = grabber.grab(timeout=SNAPSHOT_TIMEOUT)

        mode = grabber.frame.mode
        return Snapshot("png", png, mode.width, mode.height)

    def type_text(self, text: str) -> None:
        with self._switch_errors():
            events = text_events(text)
        self._send_key_events(events)

    def press(self, combinations: list[list[int]]) -> None:
        events = []
        with self._switch_errors():
            for usages in combinations:
                # This refuses Ctrl+NumLock, which opens the CIM's own menu.
                events += combination_events(usages)
        self._send_key_events(events)

    def key_down(self, usage: int) -> None:
        self._send_key_events([("press", usage)])

    def key_up(self, usage: int) -> None:
        self._send_key_events([("release", usage)])

    def mouse_move(self, x: int, y: int) -> None:
        self._run(lambda session: Mouse(session).move_to(x, y))

    def mouse_click(self, button: str, count: int, x: int | None, y: int | None) -> None:
        def click(session):
            mouse = Mouse(session)
            if x is not None:
                mouse.move_to(x, y)
            mouse.click(button, count)

        self._run(click)

    def mouse_down(self, button: str) -> None:
        self._run(lambda session: Mouse(session).button_down(button))

    def mouse_up(self, button: str) -> None:
        self._run(lambda session: Mouse(session).button_up(button))

    def mouse_scroll(self, notches: int) -> None:
        self._run(lambda session: Mouse(session).scroll(notches))
