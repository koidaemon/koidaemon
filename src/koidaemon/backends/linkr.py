# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Backend for the Radxa Linkr.

The keyboard, mouse and screen go through the Linkr's public API
(https://docs.radxa.com/en/linkr/linkr/develop-guide): two requests,
authenticated with an access token in an "Authorization: token ..."
header, GET /api/public/snapshot for a JPEG of the screen and POST
/api/public/control with a list of keyboard, mouse and text events.

The public API has nothing for the Linkr's virtual media, so disk images
go through the API its web interface uses (koidaemon.backends.
linkr_storage), which needs the Linkr's own user and password.  Without
password_file there is no USB storage.

Configuration:

    [targets.board-d]
    backend = "linkr"
    url = "https://linkr.lab"
    token_file = "/run/secrets/linkr"   # an access token, radxa_linkr_...
    user = "admin"                      # optional; for USB storage
    password_file = "/run/secrets/linkr-password"   # optional; ditto
    ca_file = "linkr.pem"               # optional; trust this certificate
    verify_tls = true                   # optional; false skips the check
    timeout = 30                        # optional; seconds per request
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from koidaemon import commands, keys, tls
from koidaemon.backends.base import Backend, Settings, Snapshot
from koidaemon.backends.linkr_storage import LinkrStorage, image_name
from koidaemon.errors import BackendError, ConfigError, InvalidRequest, Unsupported
from koidaemon.images import jpeg_size
from koidaemon.uploads import DiskImage

# The Linkr types text given as these characters only.
TEXT_CHARACTERS = frozenset("\t\n" + "".join(chr(code) for code in range(32, 127)))
# Radxa advise a pause of about a second after every 30 or so characters
# typed, which the Linkr needs to keep up.
TEXT_CHUNK = 30
TEXT_PAUSE_MS = 1000

# Wheel steps go from -this to this.
MAX_WHEEL_STEP = 20
MOUSE_BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04}

# Pause between the clicks of a double click.
CLICK_INTERVAL = 0.05


def to_fraction(position: int, size: int) -> float:
    """A pixel position on a screen of this size as the Linkr takes it,
    0 at the top or left edge to 1 at the bottom or right.

    Positions off the screen are pulled back onto its edge.
    """
    if size <= 1:
        return 0.0
    position = min(max(position, 0), size - 1)
    return round(position / (size - 1), 6)


def text_events(text: str) -> list[list]:
    """Events that type text, in chunks with pauses between them."""
    events: list[list] = []
    for start in range(0, len(text), TEXT_CHUNK):
        if events:
            events.append(["delay", TEXT_PAUSE_MS])
        events.append(["text", text[start:start + TEXT_CHUNK]])
    return events


class LinkrBackend(Backend):
    name = "linkr"
    capabilities = frozenset((commands.SNAPSHOT, commands.KEYBOARD, commands.MOUSE))

    def __init__(self, target: str, settings: Settings) -> None:
        super().__init__(target, settings)
        self.url = settings.get_str("url").rstrip("/")
        if not self.url.startswith(("https://", "http://")):
            raise ConfigError("target %r: url must start with https:// or http://" % target)
        self.token = settings.get_password("token_file")
        self.timeout = settings.get_float("timeout", 30.0)
        user = settings.get_str("user", "admin")
        password_file = settings.get_path("password_file", None)
        ca_file = settings.get_path("ca_file", None)
        verify_tls = settings.get_bool("verify_tls", True)
        settings.finish()
        self._tls = tls.client_context(ca_file, verify_tls)
        # Disk images need the Linkr's own login, so only with a password.
        self.storage: LinkrStorage | None = None
        if password_file is not None:
            try:
                password = password_file.read_text().strip()
            except OSError as error:
                raise ConfigError("%s: cannot read %s: %s"
                                  % (settings.where, password_file, error.strerror)) from error
            self.storage = LinkrStorage(self.url, user, password, self._tls, self.timeout)
            self.capabilities = self.capabilities | {commands.STORAGE_STICK}
        # The last screen size seen in a snapshot, to place the pointer.
        self._screen: tuple[int, int] | None = None
        # Every mouse event says the buttons held and where the pointer is.
        self._buttons = 0
        self._position = (0.0, 0.0)

    @property
    def resource(self) -> str:
        return "linkr:%s" % self.url

    def describe(self) -> dict:
        description = super().describe()
        description["url"] = self.url
        return description

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _call(self, method: str, path: str, body: bytes | None = None,
              headers: dict | None = None) -> bytes:
        request = urllib.request.Request("%s/api/public/%s" % (self.url, path),
                                         data=body, method=method)
        request.add_header("Authorization", "token %s" % self.token)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self._tls) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise BackendError("%s refused the access token" % self.url) from error
            detail = None
            try:
                detail = json.loads(error.read()).get("message")
            except (ValueError, AttributeError):
                pass
            raise BackendError("%s: %s" % (self.url, detail or "HTTP %d" % error.code)) from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise BackendError("cannot reach %s: %s" % (self.url, reason)) from error

    def _control(self, events: list[list]) -> None:
        body = json.dumps({"events": events}).encode("utf-8")
        answer = self._call("POST", "control", body, {"Content-Type": "application/json"})
        try:
            result = json.loads(answer)
        except ValueError as error:
            raise BackendError("%s sent something that is not JSON" % self.url) from error
        if not isinstance(result, dict) or result.get("code") != 0:
            message = result.get("message") if isinstance(result, dict) else None
            raise BackendError("%s: %s" % (self.url, message or "the command failed"))

    # ------------------------------------------------------------------
    # Status and screen
    # ------------------------------------------------------------------

    def status(self) -> dict:
        # An empty list of events checks the Linkr answers and takes the
        # token, without the cost of a snapshot.
        self._control([])
        video = None
        if self._screen is not None:
            video = {"width": self._screen[0], "height": self._screen[1]}
        return {"online": True, "video": video}

    def snapshot(self) -> Snapshot:
        data = self._call("GET", "snapshot")
        size = jpeg_size(data)
        if size is None:
            raise BackendError("%s sent a snapshot that is not a JPEG" % self.url)
        self._screen = size
        return Snapshot("jpeg", data, size[0], size[1])

    def _screen_size(self) -> tuple[int, int]:
        if self._screen is None:
            self.snapshot()
        return self._screen

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def type_text(self, text: str) -> None:
        for character in text:
            if character not in TEXT_CHARACTERS:
                raise InvalidRequest("the Linkr cannot type %r" % character)
        for start in range(0, len(text), TEXT_CHUNK * 8):
            if start:
                self._control([["delay", TEXT_PAUSE_MS]])
            self._control(text_events(text[start:start + TEXT_CHUNK * 8]))

    def press(self, combinations: list[list[int]]) -> None:
        events = []
        for usages in combinations:
            codes = [_web_code(usage) for usage in usages]
            events += [["keyboard", code, True] for code in codes]
            events += [["keyboard", code, False] for code in reversed(codes)]
        self._control(events)

    def key_down(self, usage: int) -> None:
        self._control([["keyboard", _web_code(usage), True]])

    def key_up(self, usage: int) -> None:
        self._control([["keyboard", _web_code(usage), False]])

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _mouse_event(self, wheel: int = 0) -> list:
        x, y = self._position
        return ["mouse_abs", self._buttons, x, y, wheel, 0]

    def mouse_move(self, x: int, y: int) -> None:
        width, height = self._screen_size()
        self._position = (to_fraction(x, width), to_fraction(y, height))
        self._control([self._mouse_event()])

    def mouse_click(self, button: str, count: int, x: int | None, y: int | None) -> None:
        if x is not None:
            self.mouse_move(x, y)
        bit = MOUSE_BUTTONS[button]
        for click in range(count):
            if click:
                time.sleep(CLICK_INTERVAL)
            self._buttons |= bit
            down = self._mouse_event()
            self._buttons &= ~bit
            self._control([down, self._mouse_event()])

    def mouse_down(self, button: str) -> None:
        self._buttons |= MOUSE_BUTTONS[button]
        self._control([self._mouse_event()])

    def mouse_up(self, button: str) -> None:
        self._buttons &= ~MOUSE_BUTTONS[button]
        self._control([self._mouse_event()])

    def mouse_scroll(self, notches: int) -> None:
        # koid's positive notches scroll up, as with a USB wheel; the
        # Linkr's wheelV scrolls down when positive, as Radxa's own client
        # (radxa-linkr/linkr-skills) documents it.
        events = []
        remaining = notches
        while remaining:
            step = max(-MAX_WHEEL_STEP, min(MAX_WHEEL_STEP, remaining))
            events.append(self._mouse_event(-step))
            remaining -= step
        self._control(events)


    # ------------------------------------------------------------------
    # USB storage
    # ------------------------------------------------------------------

    def _storage(self) -> LinkrStorage:
        if self.storage is None:
            raise Unsupported("USB storage on a Linkr needs its password_file")
        return self.storage

    def storage_insert(self, image: DiskImage, mode: str, writable: bool) -> dict:
        storage = self._storage()
        if mode != "stick":
            raise Unsupported("a Linkr presents images only as a USB stick")
        if not writable:
            raise InvalidRequest("a Linkr cannot present an image read-only: the target "
                                 "can always write to it; insert it as writable "
                                 "(koictl storage insert --writable)")

        name = image_name(image.name, image.digest())
        stored, mounted = storage.images()
        sizes = {entry.get("name"): entry.get("size") for entry in stored}
        if sizes.get(name) != image.size:
            # The name carries the image's SHA-256, so a different size
            # is an upload that stopped part way; the Linkr says where to
            # carry on from.
            free = storage.free_space() + (sizes.get(name) or 0)
            if image.size > free:
                raise BackendError("the Linkr has %d bytes free, not enough for %s (%d bytes)"
                                   % (free, image.name, image.size))
            storage.upload(image, name)
            stored, mounted = storage.images()
            if not any(entry.get("name") == name and entry.get("size") == image.size
                       for entry in stored):
                raise BackendError("the Linkr did not list %s after it was uploaded" % name)

        if mounted != name:
            if mounted:
                storage.unmount()
            storage.mount(name)
        status = self.storage_status()
        if status["image"] != name:
            raise BackendError("the Linkr did not mount %s" % name)
        return status

    def storage_eject(self) -> None:
        storage = self._storage()
        _stored, mounted = storage.images()
        if mounted:
            storage.unmount()

    def storage_status(self) -> dict:
        _stored, mounted = self._storage().images()
        return {
            "inserted": bool(mounted),
            "image": mounted or None,
            # The Linkr has no other way to present an image.
            "mode": "stick",
            "writable": True,
        }


def _web_code(usage: int) -> str:
    """The Linkr's name for a key: the browser's KeyboardEvent.code."""
    try:
        return keys.web_code(usage)
    except ValueError as error:
        raise Unsupported(str(error)) from error
