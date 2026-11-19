# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Backend for the Sipeed NanoKVM-USB, a KVM plugged into koid's machine.

The NanoKVM-USB shows up as two USB devices:

- a serial port, through which a CH9329 keyboard and mouse chip takes
  framed commands at 57600 baud, as in Sipeed's own client
  (https://github.com/sipeed/NanoKVM-USB, browser/src/libs/device);
- a video capture device (a MacroSilicon MS2130), read with GStreamer.

Configuration:

    [targets.board-c]
    backend = "nanokvm-usb"
    serial = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_XXXX-if00"
    video = "/dev/v4l/by-id/usb-MACROSILICON_USB3_Video_XXXX-video-index0"
    width = 1920                        # optional; capture size
    height = 1080                       # optional
    timeout = 10                        # optional; seconds
    key_interval = 0.05                 # optional; seconds between key reports
    gst_launch = "gst-launch-1.0"       # optional; GStreamer's command
"""

from __future__ import annotations

import logging
import os
import pathlib
import select
import subprocess
import tempfile
import termios
import time

from koidaemon import commands, keys
from koidaemon.backends.base import Backend, Settings, Snapshot
from koidaemon.errors import BackendError, ConfigError, InvalidRequest
from koidaemon.images import jpeg_size

LOG = logging.getLogger(__name__)

BAUD_RATE = termios.B57600

# Every frame starts with these, then an address, a command, the length
# of the data, the data and a checksum.
HEAD = b"\x57\xab"
ADDRESS = 0x00

CMD_GET_INFO = 0x01
CMD_KEYBOARD = 0x02
CMD_MOUSE_ABSOLUTE = 0x04

# The chip answers each command with the same command plus this.
ANSWER = 0x80
# And, for commands without data of their own to answer, one status byte.
STATUS_OK = 0x00
STATUS_ERRORS = {
    0xE1: "a byte was not received in time",
    0xE2: "the frame header was wrong",
    0xE3: "the command was unknown",
    0xE4: "the checksum was wrong",
    0xE5: "a parameter was wrong",
    0xE6: "the chip could not do it",
}

# A keyboard report has room for this many keys besides the modifiers.
MAX_KEYS = 6

# Absolute mouse positions go from 0 to this, whatever the screen size;
# wheel steps are signed bytes.
MOUSE_MAX = 4095
MAX_WHEEL_STEP = 127
MOUSE_BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04}

# Pause between the clicks of a double click.
CLICK_INTERVAL = 0.05

# How long each keyboard report stands before the next, by default.  The
# chip passes reports on as fast as they come, and UEFI on a Radxa Dragon
# Q8B missed some key presses with less than 50 ms between reports.
KEY_INTERVAL = 0.05

# The MS2130's first frames after it is opened can be dark or torn, so
# this many are captured and the last kept.
SNAPSHOT_FRAMES = 5


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def encode_frame(command: int, data: bytes = b"") -> bytes:
    """A command frame for the CH9329."""
    frame = HEAD + bytes([ADDRESS, command, len(data)]) + data
    return frame + bytes([checksum(frame)])


def decode_frame(buffer: bytes) -> tuple[int, bytes, int] | None:
    """The first whole frame in buffer: (command, data, bytes used).

    None if no whole frame is there yet.  Bytes before a frame header are
    skipped; a frame with a bad checksum is an error.
    """
    start = buffer.find(HEAD)
    if start < 0 or len(buffer) < start + 5:
        return None
    length = buffer[start + 4]
    end = start + 5 + length + 1
    if len(buffer) < end:
        return None
    frame = buffer[start:end]
    if checksum(frame[:-1]) != frame[-1]:
        raise BackendError("the NanoKVM-USB sent a frame with a bad checksum")
    return frame[3], frame[5:-1], end


def to_mouse_range(position: int, size: int) -> int:
    """Scale a pixel position on a screen of this size to the chip's range.

    Positions off the screen are pulled back onto its edge.
    """
    if size <= 1:
        return 0
    position = min(max(position, 0), size - 1)
    return round(position * MOUSE_MAX / (size - 1))


class NanoKVMUSBBackend(Backend):
    name = "nanokvm-usb"
    capabilities = frozenset((commands.SNAPSHOT, commands.KEYBOARD, commands.MOUSE))

    def __init__(self, target: str, settings: Settings) -> None:
        super().__init__(target, settings)
        self.serial = settings.get_path("serial")
        self.video = settings.get_path("video")
        self.width = settings.get_int("width", 1920)
        self.height = settings.get_int("height", 1080)
        self.timeout = settings.get_float("timeout", 10.0)
        self.key_interval = settings.get_float("key_interval", KEY_INTERVAL)
        self.gst_launch = settings.get_str("gst_launch", "gst-launch-1.0")
        settings.finish()
        if self.width < 1 or self.height < 1:
            raise ConfigError("target %r: width and height must be positive" % target)
        if self.key_interval < 0:
            raise ConfigError("target %r: key_interval cannot be negative" % target)

        self._fd: int | None = None
        self._buffer = b""
        # What is held down: modifier bits, other keys in the order they
        # were pressed, and mouse button bits.  Every report the chip
        # takes says the whole state, so koid keeps it.
        self._modifiers = 0
        self._keys: list[int] = []
        self._buttons = 0
        self._position = (0, 0)

    @property
    def resource(self) -> str:
        return "nanokvm-usb:%s" % self.serial

    def describe(self) -> dict:
        description = super().describe()
        description["serial"] = str(self.serial)
        description["video"] = str(self.video)
        return description

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self._buffer = b""

    # ------------------------------------------------------------------
    # Serial port
    # ------------------------------------------------------------------

    def _port(self) -> int:
        if self._fd is not None:
            return self._fd
        try:
            fd = os.open(self.serial, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as error:
            raise BackendError("cannot open %s: %s" % (self.serial, error.strerror)) from error
        try:
            self._make_raw(fd)
        except (OSError, termios.error) as error:
            os.close(fd)
            raise BackendError("cannot set up %s: %s" % (self.serial, error)) from error
        self._fd = fd
        return fd

    @staticmethod
    def _make_raw(fd: int) -> None:
        """Raw bytes at 57600 baud, 8 data bits, no parity, one stop bit."""
        if not os.isatty(fd):
            return
        attributes = termios.tcgetattr(fd)
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = attributes
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                   | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
                   | termios.IXOFF | termios.IXANY)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG
                   | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, BAUD_RATE, BAUD_RATE, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)

    def _command(self, command: int, data: bytes = b"") -> bytes:
        """Send a command and return the data of the chip's answer."""
        fd = self._port()
        # Anything left from before, such as an answer that came late,
        # would be taken for this one's.
        self._buffer = b""
        self._drain(fd)
        frame = encode_frame(command, data)
        try:
            written = 0
            while written < len(frame):
                select.select([], [fd], [], self.timeout)
                written += os.write(fd, frame[written:])
        except OSError as error:
            self.close()
            raise BackendError("cannot write to %s: %s" % (self.serial, error.strerror)) from error
        return self._answer(fd, command)

    def _drain(self, fd: int) -> None:
        while True:
            try:
                if not os.read(fd, 4096):
                    return
            except BlockingIOError:
                return
            except OSError as error:
                self.close()
                raise BackendError("cannot read %s: %s" % (self.serial, error.strerror)) from error

    def _answer(self, fd: int, command: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while True:
            decoded = decode_frame(self._buffer)
            if decoded is not None:
                answered, data, used = decoded
                self._buffer = self._buffer[used:]
                if answered == command | ANSWER:
                    return data
                if answered == command | ANSWER | 0x40:
                    status = data[0] if data else None
                    raise BackendError("the NanoKVM-USB refused the command: %s"
                                       % STATUS_ERRORS.get(status, "error 0x%02x" % (status or 0)))
                LOG.debug("%s: skipping answer to command 0x%02x", self.target, answered)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("the NanoKVM-USB did not answer on %s" % self.serial)
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as error:
                self.close()
                raise BackendError("cannot read %s: %s" % (self.serial, error.strerror)) from error
            self._buffer += chunk

    def _checked(self, command: int, data: bytes) -> None:
        """Send a command whose answer is one status byte, and check it."""
        answer = self._command(command, data)
        status = answer[0] if answer else None
        if status != STATUS_OK:
            raise BackendError("the NanoKVM-USB refused the command: %s"
                               % STATUS_ERRORS.get(status, "error 0x%02x" % (status or 0)))

    # ------------------------------------------------------------------
    # Status and screen
    # ------------------------------------------------------------------

    def status(self) -> dict:
        info = self._command(CMD_GET_INFO)
        if len(info) < 3:
            raise BackendError("the NanoKVM-USB sent a short answer to GET_INFO")
        version = info[0] - 0x30
        leds = info[2]
        return {
            "online": True,
            "chip_version": "V%.1f" % (1.0 + version / 10),
            # Whether the target has found the keyboard and mouse.
            "usb_connected": bool(info[1]),
            "num_lock": bool(leds & 0x01),
            "caps_lock": bool(leds & 0x02),
            "scroll_lock": bool(leds & 0x04),
            "video": {"width": self.width, "height": self.height},
        }

    def snapshot(self) -> Snapshot:
        if not os.access(self.video, os.F_OK):
            raise BackendError("%s does not exist" % self.video)
        if not os.access(self.video, os.R_OK | os.W_OK):
            raise BackendError("cannot open %s: permission denied (is koid in the "
                               "group that owns it, often video?)" % self.video)
        with tempfile.TemporaryDirectory(prefix="koid-") as directory:
            command = [
                self.gst_launch, "-q",
                "v4l2src", "device=%s" % self.video, "num-buffers=%d" % SNAPSHOT_FRAMES,
                "!", "image/jpeg,width=%d,height=%d" % (self.width, self.height),
                "!", "multifilesink", "location=%s" % os.path.join(directory, "frame-%05d.jpg"),
            ]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        timeout=self.timeout)
            except FileNotFoundError as error:
                raise BackendError("cannot run %s: is GStreamer installed?"
                                   % self.gst_launch) from error
            except subprocess.TimeoutExpired as error:
                raise BackendError("GStreamer took more than %g seconds to capture %s"
                                   % (self.timeout, self.video)) from error
            frames = sorted(pathlib.Path(directory).glob("frame-*.jpg"))
            frames = [frame for frame in frames if frame.stat().st_size]
            if result.returncode != 0 or not frames:
                output = result.stdout.decode("utf-8", "replace").strip()
                raise BackendError("cannot capture %s: %s"
                                   % (self.video, output or "GStreamer wrote no frame"))
            data = frames[-1].read_bytes()
        size = jpeg_size(data)
        if size is None:
            return Snapshot("jpeg", data)
        return Snapshot("jpeg", data, size[0], size[1])

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def _send_keys(self, modifiers: int, pressed: list[int]) -> None:
        if len(pressed) > MAX_KEYS:
            raise InvalidRequest("the NanoKVM-USB can hold at most %d keys besides "
                                 "the modifiers" % MAX_KEYS)
        report = bytes([modifiers, 0] + pressed + [0] * (MAX_KEYS - len(pressed)))
        self._checked(CMD_KEYBOARD, report)
        time.sleep(self.key_interval)

    def _send_held_keys(self) -> None:
        self._send_keys(self._modifiers, self._keys)

    def type_text(self, text: str) -> None:
        for character in text:
            if character not in keys.CHARACTERS:
                raise InvalidRequest("cannot type %r on a US keyboard" % character)
        shift = 1 << (keys.LEFT_SHIFT - keys.LEFT_CTRL)
        for character in text:
            usage, needs_shift = keys.CHARACTERS[character]
            modifiers = self._modifiers | (shift if needs_shift else 0)
            pressed = self._keys if usage in self._keys else self._keys + [usage]
            self._send_keys(modifiers, pressed)
            self._send_held_keys()

    def press(self, combinations: list[list[int]]) -> None:
        for usages in combinations:
            for usage in usages:
                self._hold(usage)
                self._send_held_keys()
            for usage in reversed(usages):
                self._release(usage)
                self._send_held_keys()

    def _hold(self, usage: int) -> None:
        if usage in keys.MODIFIERS:
            self._modifiers |= 1 << (usage - keys.LEFT_CTRL)
        elif usage not in self._keys:
            if len(self._keys) >= MAX_KEYS:
                raise InvalidRequest("the NanoKVM-USB can hold at most %d keys besides "
                                     "the modifiers" % MAX_KEYS)
            self._keys.append(usage)

    def _release(self, usage: int) -> None:
        if usage in keys.MODIFIERS:
            self._modifiers &= ~(1 << (usage - keys.LEFT_CTRL))
        elif usage in self._keys:
            self._keys.remove(usage)

    def key_down(self, usage: int) -> None:
        self._hold(usage)
        self._send_held_keys()

    def key_up(self, usage: int) -> None:
        self._release(usage)
        self._send_held_keys()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _send_mouse(self, wheel: int = 0) -> None:
        x = to_mouse_range(self._position[0], self.width)
        y = to_mouse_range(self._position[1], self.height)
        data = bytes([0x02, self._buttons]) + x.to_bytes(2, "little") \
            + y.to_bytes(2, "little") + bytes([wheel & 0xFF])
        self._checked(CMD_MOUSE_ABSOLUTE, data)

    def mouse_move(self, x: int, y: int) -> None:
        self._position = (x, y)
        self._send_mouse()

    def mouse_click(self, button: str, count: int, x: int | None, y: int | None) -> None:
        if x is not None:
            self.mouse_move(x, y)
        bit = MOUSE_BUTTONS[button]
        for click in range(count):
            if click:
                time.sleep(CLICK_INTERVAL)
            self._buttons |= bit
            self._send_mouse()
            self._buttons &= ~bit
            self._send_mouse()

    def mouse_down(self, button: str) -> None:
        self._buttons |= MOUSE_BUTTONS[button]
        self._send_mouse()

    def mouse_up(self, button: str) -> None:
        self._buttons &= ~MOUSE_BUTTONS[button]
        self._send_mouse()

    def mouse_scroll(self, notches: int) -> None:
        # Positive scrolls up, as with a USB wheel.
        remaining = notches
        while remaining:
            step = max(-MAX_WHEEL_STEP, min(MAX_WHEEL_STEP, remaining))
            self._send_mouse(step)
            remaining -= step
