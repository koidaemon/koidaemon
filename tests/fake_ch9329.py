# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A fake CH9329 keyboard and mouse chip, as in a NanoKVM-USB, on a pty."""

from __future__ import annotations

import os
import select
import threading
import tty

from koidaemon.backends import nanokvm_usb

# GET_INFO's answer from a real NanoKVM-USB: chip V1.8, target connected,
# num lock on.
INFO = bytes([0x38, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])


class FakeCH9329:
    """Answers frames written to the pty's other end, and records them."""

    def __init__(self) -> None:
        self.master, slave = os.openpty()
        tty.setraw(self.master)
        self.path = os.ttyname(slave)
        # Kept open so the pty stays up between the backend's opens.
        self._slave = slave
        self.frames: list[tuple[int, bytes]] = []
        # Set to a status byte to refuse commands, or None to not answer.
        self.status: int | None = nanokvm_usb.STATUS_OK
        self.info = INFO
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "FakeCH9329":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        os.close(self.master)
        os.close(self._slave)

    def keyboard_reports(self) -> list[bytes]:
        return [data for command, data in self.frames if command == nanokvm_usb.CMD_KEYBOARD]

    def mouse_reports(self) -> list[bytes]:
        return [data for command, data in self.frames
                if command == nanokvm_usb.CMD_MOUSE_ABSOLUTE]

    def _run(self) -> None:
        buffer = b""
        while not self._stop.is_set():
            readable, _, _ = select.select([self.master], [], [], 0.05)
            if not readable:
                continue
            try:
                buffer += os.read(self.master, 4096)
            except OSError:
                return
            while True:
                decoded = nanokvm_usb.decode_frame(buffer)
                if decoded is None:
                    break
                command, data, used = decoded
                buffer = buffer[used:]
                self.frames.append((command, data))
                self._answer(command)

    def _answer(self, command: int) -> None:
        if self.status is None:
            return
        if self.status != nanokvm_usb.STATUS_OK:
            frame = nanokvm_usb.encode_frame(command | 0xC0, bytes([self.status]))
        elif command == nanokvm_usb.CMD_GET_INFO:
            frame = nanokvm_usb.encode_frame(command | 0x80, self.info)
        else:
            frame = nanokvm_usb.encode_frame(command | 0x80, bytes([self.status]))
        os.write(self.master, frame)
