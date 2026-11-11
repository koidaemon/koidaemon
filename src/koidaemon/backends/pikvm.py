# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Backend for PiKVM, and other devices running its kvmd daemon.

Everything goes through kvmd's HTTP API (https://docs.pikvm.org/api/),
authenticated with the X-KVMD-User and X-KVMD-Passwd headers.  Tested with
a PiKVM V4 Mini running kvmd 4.61.

Configuration:

    [targets.board-b]
    backend = "pikvm"
    url = "https://pikvm-07.lab"
    user = "admin"                      # optional; default admin
    password_file = "/run/secrets/pikvm-07"
    ca_file = "pikvm-07.pem"            # optional; trust this certificate
    verify_tls = true                   # optional; false skips the check
    keymap = "en-us"                    # optional; kvmd's default otherwise
    timeout = 30                        # optional; seconds per request

A PiKVM's certificate is self-signed out of the box: either give its
certificate as ca_file (better) or set verify_tls = false.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import pathlib
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from koidaemon import commands, keys
from koidaemon.backends.base import Backend, Settings, Snapshot
from koidaemon.errors import BackendError, ConfigError, InvalidRequest, Unsupported
from koidaemon.websocket import WebSocket, WebSocketError

LOG = logging.getLogger(__name__)

# kvmd takes absolute mouse positions scaled to this range, whatever the
# screen size, and wheel steps of at most this size.
MOUSE_MIN = -32768
MOUSE_MAX = 32767
MAX_WHEEL_STEP = 127

# Pause between the clicks of a double click.
CLICK_INTERVAL = 0.05

# How long to wait for video once kvmd's streamer is running, and how
# often to look.
VIDEO_WAIT = 10.0
VIDEO_POLL_INTERVAL = 0.25

# Uploaded images are named after their contents, so an image that is
# already on the PiKVM is not uploaded again.
IMAGE_PREFIX = "koid-"
HASH_CHUNK = 1024 * 1024

# Linux's USB mass storage gadget will not present a CD smaller than 300
# sectors of 2048 bytes; kvmd then fails with a bare HTTP 500.
MIN_CD_SIZE = 300 * 2048

# How long kvmd may take to notice an uploaded image, and how often to look.
IMAGE_WAIT = 10.0
IMAGE_POLL_INTERVAL = 0.25


class _Unavailable(BackendError):
    """kvmd answered 503: what was asked for is not ready, such as video."""


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's frame header, or None if not found."""
    if data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        # The start-of-frame markers are C0-CF, except C4 (Huffman tables),
        # C8 (reserved) and CC (arithmetic coding).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[offset + 5:offset + 7], "big")
            width = int.from_bytes(data[offset + 7:offset + 9], "big")
            return width, height
        offset += 2 + length
    return None


def to_mouse_range(position: int, size: int) -> int:
    """Scale a pixel position on a screen of this size to kvmd's range.

    Positions off the screen are pulled back onto its edge.
    """
    if size <= 1:
        return MOUSE_MIN
    position = min(max(position, 0), size - 1)
    return MOUSE_MIN + round(position * (MOUSE_MAX - MOUSE_MIN) / (size - 1))


def image_name(path: pathlib.Path, digest: str) -> str:
    """The name an image is stored under on the PiKVM."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)
    return "%s%s-%s" % (IMAGE_PREFIX, digest[:12], safe)


def file_digest(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as image:
        while True:
            chunk = image.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class PiKVMBackend(Backend):
    name = "pikvm"
    capabilities = frozenset(commands.ALL_CAPABILITIES)

    def __init__(self, target: str, settings: Settings) -> None:
        super().__init__(target, settings)
        self.url = settings.get_str("url").rstrip("/")
        if not self.url.startswith(("https://", "http://")):
            raise ConfigError("target %r: url must start with https:// or http://" % target)
        self.user = settings.get_str("user", "admin")
        self.password = settings.get_password()
        self.keymap = settings.get_str("keymap", None)
        self.timeout = settings.get_float("timeout", 30.0)
        ca_file = settings.get_path("ca_file", None)
        verify_tls = settings.get_bool("verify_tls", True)
        settings.finish()
        self._tls = self._tls_context(ca_file, verify_tls)

    @staticmethod
    def _tls_context(ca_file: pathlib.Path | None, verify: bool) -> ssl.SSLContext:
        if ca_file is not None:
            context = ssl.create_default_context(cafile=str(ca_file))
            # A PiKVM's own certificate names the device, which rarely
            # matches the name it is reached by; trusting that one
            # certificate is the check that matters.
            context.check_hostname = False
            return context
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    @property
    def resource(self) -> str:
        return "pikvm:%s" % self.url

    def describe(self) -> dict:
        description = super().describe()
        description["url"] = self.url
        return description

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _call(self, method: str, path: str, query: dict | None = None,
              body=None, headers: dict | None = None, timeout: float | None = None):
        """Make one API request and return the raw response body."""
        url = "%s/api/%s" % (self.url, path)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("X-KVMD-User", self.user)
        request.add_header("X-KVMD-Passwd", self.password)
        for name, value in (headers or {}).items():
            request.add_header(name, value)

        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout,
                                        context=self._tls) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise _Unavailable(self._describe_http_error(error)) from error
            raise BackendError(self._describe_http_error(error)) from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise BackendError("cannot reach %s: %s" % (self.url, reason)) from error

    def _describe_http_error(self, error: urllib.error.HTTPError) -> str:
        if error.code in (401, 403):
            return "%s refused the login for %s" % (self.url, self.user)
        # kvmd explains failures in a JSON body: {"ok": false, "result":
        # {"error": "...", "error_msg": "..."}}.
        try:
            result = json.loads(error.read())["result"]
            detail = result.get("error_msg") or result.get("error")
        except (ValueError, KeyError, TypeError, AttributeError):
            detail = None
        if detail:
            return "%s: %s" % (self.url, detail)
        return "%s answered HTTP %d" % (self.url, error.code)

    def _json(self, method: str, path: str, query: dict | None = None, **kwargs) -> dict:
        """Make an API request and return the "result" of its JSON answer."""
        body = self._call(method, path, query, **kwargs)
        try:
            answer = json.loads(body)
        except ValueError as error:
            raise BackendError("%s sent something that is not JSON" % self.url) from error
        if not answer.get("ok"):
            result = answer.get("result") or {}
            raise BackendError("%s: %s" % (self.url, result.get("error_msg", "request failed")))
        return answer.get("result") or {}

    # ------------------------------------------------------------------
    # Status and screen
    # ------------------------------------------------------------------

    def _source(self) -> dict | None:
        """The video source's state, or None while the streamer is stopped."""
        streamer = self._json("GET", "streamer").get("streamer")
        if not streamer:
            return None
        return streamer.get("source")

    def status(self) -> dict:
        # Only reports video while kvmd's streamer happens to be running:
        # starting it just to look would make status slow.
        source = self._source()
        video = None
        if source and source.get("online"):
            resolution = source.get("resolution", {})
            video = {"width": resolution.get("width"), "height": resolution.get("height")}
        return {"online": True, "video": video}

    def _watch(self) -> WebSocket:
        """A connection to kvmd's WebSocket, which keeps its streamer running."""
        url = self.url.replace("http", "ws", 1) + "/api/ws?stream=1"
        headers = {"X-KVMD-User": self.user, "X-KVMD-Passwd": self.password}
        return WebSocket(url, headers, self._tls, self.timeout)

    @contextlib.contextmanager
    def _video(self):
        """Make sure kvmd's streamer runs and has a signal; yield the source.

        kvmd only runs its streamer while someone watches, so if it is
        stopped this watches through kvmd's WebSocket until the block ends.
        """
        source = self._source()
        if source is not None and source.get("online"):
            yield source
            return

        watching = self._watch() if source is None else contextlib.nullcontext()
        try:
            with watching:
                yield self._wait_for_video()
        except WebSocketError as error:
            if error.status in (401, 403):
                raise BackendError("%s refused the login for %s" % (self.url, self.user)) from error
            raise BackendError("%s: cannot watch the video: %s" % (self.url, error)) from error

    def _wait_for_video(self) -> dict:
        deadline = time.monotonic() + VIDEO_WAIT
        while True:
            source = self._source()
            if source is not None and source.get("online"):
                return source
            if time.monotonic() >= deadline:
                raise BackendError("no video signal from the target")
            time.sleep(VIDEO_POLL_INTERVAL)

    def snapshot(self) -> Snapshot:
        with self._video():
            data = self._take_snapshot()
        size = jpeg_size(data)
        if size is None:
            return Snapshot("jpeg", data)
        return Snapshot("jpeg", data, size[0], size[1])

    def _take_snapshot(self) -> bytes:
        # A streamer that has just started may not have a frame yet.
        deadline = time.monotonic() + VIDEO_WAIT
        while True:
            try:
                return self._call("GET", "streamer/snapshot", {"allow_offline": "0"})
            except _Unavailable:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(VIDEO_POLL_INTERVAL)

    def _screen_size(self) -> tuple[int, int]:
        """The target's screen size, needed to place the pointer in pixels."""
        with self._video() as source:
            resolution = source.get("resolution", {})
        width = resolution.get("width")
        height = resolution.get("height")
        if not width or not height:
            raise BackendError("kvmd did not say the screen size, so pixel positions "
                               "cannot be placed")
        return width, height

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def type_text(self, text: str) -> None:
        # limit=0 lifts kvmd's default limit of 1024 characters.
        query = {"limit": "0"}
        if self.keymap:
            query["keymap"] = self.keymap
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        self._json("POST", "hid/print", query, body=text.encode("utf-8"), headers=headers)

    def press(self, combinations: list[list[int]]) -> None:
        # One key at a time: kvmd's send_shortcut, which would do a whole
        # combination, is newer than many PiKVMs' kvmd.
        for usages in combinations:
            codes = [_web_code(usage) for usage in usages]
            for code in codes:
                self._json("POST", "hid/events/send_key", {"key": code, "state": "1"})
            for code in reversed(codes):
                self._json("POST", "hid/events/send_key", {"key": code, "state": "0"})

    def key_down(self, usage: int) -> None:
        self._json("POST", "hid/events/send_key", {"key": _web_code(usage), "state": "1"})

    def key_up(self, usage: int) -> None:
        self._json("POST", "hid/events/send_key", {"key": _web_code(usage), "state": "0"})

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def mouse_move(self, x: int, y: int) -> None:
        width, height = self._screen_size()
        query = {
            "to_x": str(to_mouse_range(x, width)),
            "to_y": str(to_mouse_range(y, height)),
        }
        self._json("POST", "hid/events/send_mouse_move", query)

    def mouse_click(self, button: str, count: int, x: int | None, y: int | None) -> None:
        if x is not None:
            self.mouse_move(x, y)
        for click in range(count):
            if click:
                time.sleep(CLICK_INTERVAL)
            # Without a state kvmd presses and releases the button.
            self._json("POST", "hid/events/send_mouse_button", {"button": button})

    def mouse_down(self, button: str) -> None:
        self._json("POST", "hid/events/send_mouse_button", {"button": button, "state": "1"})

    def mouse_up(self, button: str) -> None:
        self._json("POST", "hid/events/send_mouse_button", {"button": button, "state": "0"})

    def mouse_scroll(self, notches: int) -> None:
        # Positive delta_y scrolls up, as with a USB wheel.  Each request
        # can move at most MAX_WHEEL_STEP.
        remaining = notches
        while remaining:
            step = max(-MAX_WHEEL_STEP, min(MAX_WHEEL_STEP, remaining))
            self._json("POST", "hid/events/send_mouse_wheel",
                       {"delta_x": "0", "delta_y": str(step)})
            remaining -= step

    # ------------------------------------------------------------------
    # USB storage
    # ------------------------------------------------------------------

    def _msd(self) -> dict:
        state = self._json("GET", "msd")
        if not state.get("enabled"):
            raise Unsupported("this PiKVM has no mass storage drive enabled")
        return state

    def _disconnect(self, state: dict) -> None:
        drive = state.get("drive") or {}
        if drive.get("connected"):
            self._json("POST", "msd/set_connected", {"connected": "0"})

    def _upload(self, path: pathlib.Path, name: str) -> None:
        size = path.stat().st_size
        LOG.info("%s: uploading %s (%d bytes) as %s", self.target, path, size, name)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        query = {"image": name, "remove_incomplete": "1"}
        with open(path, "rb") as image:
            self._json("POST", "msd/write", query, body=image, headers=headers)

    def storage_insert(self, image: pathlib.Path, mode: str, writable: bool) -> dict:
        try:
            size = image.stat().st_size
            digest = file_digest(image)
        except OSError as error:
            raise BackendError("cannot read %s: %s" % (image, error.strerror)) from error
        if mode == "cdrom" and size < MIN_CD_SIZE:
            raise InvalidRequest("%s is too small to be a CD: the PiKVM needs at least %d KiB"
                                 % (image.name, MIN_CD_SIZE // 1024))

        state = self._msd()
        self._disconnect(state)

        name = image_name(image, digest)
        stored = (state.get("storage") or {}).get("images", {})
        if name not in stored or not stored[name].get("complete", True):
            self._upload(image, name)
            self._wait_for_image(name)

        cdrom = mode == "cdrom"
        params = {
            "image": name,
            "cdrom": "1" if cdrom else "0",
            "rw": "1" if writable else "0",
        }
        self._json("POST", "msd/set_params", params)
        self._json("POST", "msd/set_connected", {"connected": "1"})

        # kvmd quietly drops flags it cannot honour, such as cdrom for an
        # image too big to be a CD, so check what it actually did.
        status = self.storage_status()
        if status["image"] != name or not status["inserted"]:
            raise BackendError("the PiKVM did not connect %s" % name)
        if status["mode"] != mode:
            raise BackendError("the PiKVM would not present %s as a %s"
                               " (too big for a CD?)" % (image.name, mode))
        if status["writable"] != writable:
            raise BackendError("the PiKVM would not make %s %s"
                               % (image.name, "writable" if writable else "read-only"))
        return status

    def _wait_for_image(self, name: str) -> None:
        """Wait for kvmd to notice an uploaded image.

        kvmd finds images by watching its storage directory, so for a
        moment after an upload it does not know the image and refuses to
        select it.
        """
        deadline = time.monotonic() + IMAGE_WAIT
        while True:
            images = (self._msd().get("storage") or {}).get("images", {})
            if images.get(name, {}).get("complete", False):
                return
            if time.monotonic() >= deadline:
                raise BackendError("the PiKVM did not list %s after it was uploaded" % name)
            time.sleep(IMAGE_POLL_INTERVAL)

    def storage_eject(self) -> None:
        self._disconnect(self._msd())

    def storage_status(self) -> dict:
        drive = self._msd().get("drive") or {}
        image = drive.get("image") or {}
        return {
            "inserted": bool(drive.get("connected")),
            "image": image.get("name"),
            "mode": "cdrom" if drive.get("cdrom") else "stick",
            "writable": bool(drive.get("rw")),
        }


def _web_code(usage: int) -> str:
    """kvmd's name for a key."""
    try:
        return keys.web_code(usage)
    except ValueError as error:
        raise Unsupported(str(error)) from error
