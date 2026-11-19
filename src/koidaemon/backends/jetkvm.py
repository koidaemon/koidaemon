# Copyright (c) 2026 Koen Kooi
#
# SPDX-License-Identifier: MIT

"""Backend for JetKVM.

Talks to the JetKVM the way its web interface does: a WebRTC session, set
up over the WebSocket at /webrtc/signaling/client, carries the H.264 video
and two data channels, "rpc" (JSON-RPC 2.0) and "hidrpc" (binary keyboard
and mouse reports).  Tested with firmware 0.5.10.

WebRTC needs aiortc and av, which koidaemon does not install by default:
pip install 'koidaemon[jetkvm]'.  Without them every command fails.

Configuration:

    [targets.board-c]
    backend = "jetkvm"
    url = "http://192.0.2.30"
    password_file = "/run/secrets/jetkvm-c"   # optional; not in noPassword mode
    ca_file = "jetkvm-c.pem"                   # optional; trust this certificate
    verify_tls = true                          # optional; false skips the check
    timeout = 30                               # optional; seconds per request
    idle_timeout = 60                          # optional; seconds a session stays

A JetKVM serves one session at a time: koid's takes over from the web
interface, and the other way round.  So koid connects when a command needs
the JetKVM and closes the session once it has been idle for idle_timeout.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import http.cookies
import importlib.util
import io
import json
import logging
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request

from koidaemon import commands, keys
from koidaemon.backends.base import Backend, Settings, Snapshot, tls_context
from koidaemon.errors import BackendError, Busy, ConfigError, InvalidRequest
from koidaemon.websocket import WebSocket, WebSocketError

LOG = logging.getLogger(__name__)
# aioice logs every ICE check, and aiortc every frame it cannot decode
# before the first key frame.
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcrtpreceiver").setLevel(logging.ERROR)

HAVE_WEBRTC = all(importlib.util.find_spec(name) for name in ("aiortc", "av"))
NO_WEBRTC = "the jetkvm backend needs aiortc and av: pip install 'koidaemon[jetkvm]'"

SIGNALING_PATH = "/webrtc/signaling/client"
LOGIN_PATH = "/auth/login-local"

# HID RPC message types, from the firmware's internal/hidrpc.
HID_HANDSHAKE = 0x01
HID_POINTER = 0x03
HID_MACRO = 0x07
HID_MACRO_STATE = 0x34
HID_VERSION = 0x01

# Absolute pointer positions are scaled to 0..MOUSE_MAX whatever the
# screen size.
MOUSE_MAX = 32767
BUTTONS = {"left": 1, "right": 2, "middle": 4}
HID_KEYS = 6

# How long each step of a keyboard macro lasts, as the web interface's
# paste does.  A macro is one message on the data channel, which takes at
# most 64 KiB, at 9 bytes a step.
KEY_DELAY_MS = 20
MACRO_STEPS = 1024

CLICK_INTERVAL = 0.05
# How long to wait for a decoded frame.
VIDEO_WAIT = 10.0

# Decoding video costs a core's quarter at 1080p, so only a session made for
# a snapshot has any, and it is closed soon after.
VIDEO_IDLE = 5.0


def to_mouse_range(position: int, size: int) -> int:
    """Scale a pixel position on a screen of this size to 0..MOUSE_MAX.

    Positions off the screen are pulled back onto its edge.
    """
    if size <= 1:
        return 0
    position = min(max(position, 0), size - 1)
    return round(position * MOUSE_MAX / (size - 1))


def keyboard_state(usages: list[int]) -> tuple[int, bytes]:
    """The modifier byte and six key slots of a report holding usages."""
    modifier = 0
    pressed = []
    for usage in usages:
        if usage in keys.MODIFIERS:
            modifier |= 1 << (usage - keys.LEFT_CTRL)
        elif usage not in pressed:
            pressed.append(usage)
    return modifier, bytes(pressed[:HID_KEYS]).ljust(HID_KEYS, b"\0")


def macro_message(steps: list[tuple[list[int], int]], paste: bool) -> bytes:
    """A keyboard macro: each step holds its keys for its delay in ms."""
    message = bytearray(struct.pack(">BBI", HID_MACRO, paste, len(steps)))
    for usages, delay in steps:
        modifier, slots = keyboard_state(usages)
        message += struct.pack(">B6sH", modifier, slots, delay)
    return bytes(message)


def text_steps(text: str, held: list[int]) -> list[tuple[list[int], int]]:
    """Macro steps that type text on a US layout, then leave held held."""
    steps = []
    for character in text:
        if character not in keys.CHARACTERS:
            raise InvalidRequest("cannot type %r on a US keyboard" % character)
        usage, needs_shift = keys.CHARACTERS[character]
        steps.append(([keys.LEFT_SHIFT, usage] if needs_shift else [usage], KEY_DELAY_MS))
        # Released between characters, so that a doubled letter is typed twice.
        steps.append(([], KEY_DELAY_MS))
    steps[-1] = (held, 0)
    return steps


def press_steps(combinations: list[list[int]], held: list[int]) -> list[tuple[list[int], int]]:
    """Macro steps that press each combination in order and release it in
    reverse, on top of held."""
    steps = []
    for usages in combinations:
        base = [usage for usage in held if usage not in usages]
        for count in range(1, len(usages) + 1):
            steps.append((base + usages[:count], KEY_DELAY_MS))
        for count in range(len(usages) - 1, -1, -1):
            steps.append((base + usages[:count], KEY_DELAY_MS))
        held = base
    steps[-1] = (steps[-1][0], 0)
    return steps


def pointer_message(x: int, y: int, buttons: int) -> bytes:
    return struct.pack(">BiiB", HID_POINTER, x, y, buttons)


class _Session:
    """One WebRTC session with the JetKVM, used on the backend's event loop."""

    def __init__(self, backend: "JetKVMBackend", video: bool) -> None:
        self.backend = backend
        self.has_video = video
        self.cookie: str | None = None
        self.url = backend.url
        self.timeout = backend.timeout
        self.lost: str | None = None
        self.video: dict = {}
        self._loop = asyncio.get_running_loop()
        self._pc = None
        self._ws: WebSocket | None = None
        self._rpc = None
        self._hid = None
        self._ids = 0
        self._calls: dict[int, asyncio.Future] = {}
        self._waiters: list[asyncio.Future] = []
        self._frames: list[asyncio.Future] = []
        self._macro: asyncio.Future | None = None
        self._handshake: asyncio.Future | None = None
        self._signals: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    async def open(self, cookie: str | None) -> None:
        from aiortc import RTCConfiguration, RTCPeerConnection

        self.cookie = cookie
        self._pc = pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        if self.has_video:
            pc.addTransceiver("video", direction="recvonly")
        self._rpc = pc.createDataChannel("rpc")
        self._hid = pc.createDataChannel("hidrpc")
        self._rpc.on("message", self._on_rpc)
        self._hid.on("message", self._on_hid)
        pc.on("track", self._on_track)
        pc.on("connectionstatechange", self._on_state)
        ready = [self._waiter(), self._waiter(), self._waiter()]
        self._rpc.on("open", lambda: _settle(ready[0]))
        self._hid.on("open", lambda: _settle(ready[1]))
        self._handshake = ready[2]

        headers = {"Cookie": "authToken=" + cookie} if cookie else {}
        url = self.url.replace("http", "ws", 1) + SIGNALING_PATH
        self._ws = WebSocket(url, headers, self.backend.tls, self.timeout, self._on_signal)
        try:
            await asyncio.to_thread(self._ws.open)
        except WebSocketError as error:
            self._ws = None
            if error.status in (401, 403):
                raise BackendError("%s refused the login%s" % (
                    self.url, "" if cookie else ": it needs a password_file")) from error
            raise BackendError("cannot reach %s: %s" % (self.url, error)) from error

        await pc.setLocalDescription(await pc.createOffer())
        offer = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        sd = base64.b64encode(json.dumps(offer).encode()).decode()
        self._ws.send_text(json.dumps({"type": "offer", "data": {"sd": sd}}))
        self._tasks.append(asyncio.create_task(self._signaling()))
        try:
            async with asyncio.timeout(self.timeout):
                await asyncio.gather(ready[0], ready[1])
                self._hid.send(bytes([HID_HANDSHAKE, HID_VERSION]))
                await self._handshake
        except TimeoutError:
            raise BackendError("%s: no WebRTC session within %g s"
                               % (self.url, self.timeout)) from None

    def _waiter(self) -> asyncio.Future:
        """A future that fails when the session is lost."""
        future = self._loop.create_future()
        self._waiters = [waiter for waiter in self._waiters if not waiter.done()] + [future]
        return future

    def _on_signal(self, message) -> None:
        # From the WebSocket's reader thread.
        self._loop.call_soon_threadsafe(self._signals.put_nowait, message)

    async def _signaling(self) -> None:
        # The JetKVM can send ICE candidates before its answer, and aiortc
        # drops candidates that come before the answer.
        from aiortc import RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp

        early = []
        while True:
            message = await self._signals.get()
            try:
                message = json.loads(message)
                kind, data = message.get("type"), message.get("data")
                if "error" in message:
                    self._lose("%s refused the WebRTC session: %s" % (self.url, message["error"]))
                elif kind == "answer":
                    answer = json.loads(base64.b64decode(data))
                    await self._pc.setRemoteDescription(
                        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
                    for candidate in early:
                        await self._pc.addIceCandidate(candidate)
                    early = None
                elif kind == "new-ice-candidate" and data.get("candidate"):
                    candidate = candidate_from_sdp(data["candidate"].split(":", 1)[1])
                    candidate.sdpMid = data.get("sdpMid")
                    candidate.sdpMLineIndex = data.get("sdpMLineIndex")
                    if early is None:
                        await self._pc.addIceCandidate(candidate)
                    else:
                        early.append(candidate)
            except Exception as error:
                LOG.warning("%s: cannot follow signaling message %.200r: %s",
                            self.backend.target, message, error)

    def _on_state(self) -> None:
        if self._pc.connectionState in ("failed", "closed"):
            self._lose("%s: the WebRTC session ended" % self.url)

    def _on_track(self, track) -> None:
        if track.kind == "video":
            self._tasks.append(asyncio.create_task(self._receive_video(track)))

    async def _receive_video(self, track) -> None:
        while True:
            try:
                frame = await track.recv()
            except Exception:
                # MediaStreamError, as the session closes.
                return
            for waiter in self._frames:
                _settle(waiter, frame)
            self._frames = []

    async def frame(self, timeout: float):
        """The next decoded frame of video."""
        waiter = self._waiter()
        self._frames.append(waiter)
        try:
            return await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            raise BackendError("%s sent no video within %g s" % (self.url, timeout)) from None

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _on_rpc(self, message) -> None:
        try:
            message = json.loads(message)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        method = message.get("method")
        if method == "videoInputState":
            self.video = message.get("params") or {}
        elif method == "otherSessionConnected":
            self._lose("%s: another session took over the JetKVM" % self.url)
        call = self._calls.pop(message.get("id"), None)
        if call is None:
            return
        error = message.get("error")
        if error is not None:
            # The firmware puts its own error text in data.
            if isinstance(error, dict):
                error = error.get("data") or error.get("message")
            _settle(call, BackendError("%s: %s" % (self.url, error)))
        else:
            _settle(call, message.get("result"))

    async def call(self, method: str, **params):
        self._check()
        self._ids += 1
        call_id = self._ids
        future = self._calls[call_id] = self._waiter()
        self._rpc.send(json.dumps({"jsonrpc": "2.0", "id": call_id,
                                   "method": method, "params": params}))
        try:
            return await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            raise BackendError("%s did not answer %s within %g s"
                               % (self.url, method, self.timeout)) from None
        finally:
            self._calls.pop(call_id, None)

    # ------------------------------------------------------------------
    # HID RPC
    # ------------------------------------------------------------------

    def _on_hid(self, message) -> None:
        if not isinstance(message, bytes) or not message:
            return
        if message[0] == HID_HANDSHAKE:
            _settle(self._handshake)
        elif message[0] == HID_MACRO_STATE and message[1:2] == b"\0":
            if self._macro is not None:
                _settle(self._macro)

    def send(self, message: bytes) -> None:
        self._check()
        self._hid.send(message)

    async def macro(self, steps: list[tuple[list[int], int]], paste: bool = False) -> None:
        """Run a keyboard macro on the JetKVM, and wait for it to finish.

        The JetKVM runs the steps itself, so their timing does not depend
        on the network, and says when it is done.
        """
        for start in range(0, len(steps), MACRO_STEPS):
            chunk = steps[start:start + MACRO_STEPS]
            self._macro = self._waiter()
            self.send(macro_message(chunk, paste))
            duration = _duration(chunk)
            try:
                await asyncio.wait_for(self._macro, duration + self.timeout)
            except TimeoutError:
                # A late end could be taken for the next macro's.
                self._lose("%s: a keyboard macro did not finish" % self.url)
                raise BackendError("%s did not finish typing within %g s"
                                   % (self.url, duration + self.timeout)) from None
            finally:
                self._macro = None

    async def flush(self) -> None:
        """Wait for what was sent on the HID channel to leave koid."""
        try:
            async with asyncio.timeout(self.timeout):
                while self._hid.bufferedAmount and not self.lost:
                    await asyncio.sleep(0.01)
        except TimeoutError:
            raise BackendError("%s did not take the mouse report within %g s"
                               % (self.url, self.timeout)) from None
        self._check()

    # ------------------------------------------------------------------
    # The end
    # ------------------------------------------------------------------

    def _check(self) -> None:
        if self.lost:
            raise BackendError(self.lost)

    def _lose(self, reason: str) -> None:
        """Fail everything waiting on the session, which cannot be used."""
        if self.lost:
            return
        LOG.info("%s: %s", self.backend.target, reason)
        self.lost = reason
        for waiter in self._waiters:
            _settle(waiter, BackendError(reason))
        self._waiters = []

    async def close(self) -> None:
        self._lose("%s: the session was closed" % self.url)
        for task in self._tasks:
            task.cancel()
        if self._pc is not None:
            await self._pc.close()
        if self._ws is not None:
            await asyncio.to_thread(self._ws.close)


def _duration(steps: list[tuple[list[int], int]]) -> float:
    return sum(delay for _usages, delay in steps) / 1000


def _settle(future: asyncio.Future, result=None) -> None:
    if future.done():
        return
    if isinstance(result, BaseException):
        future.set_exception(result)
        # Nobody may be waiting any more.
        future.exception()
    else:
        future.set_result(result)


class JetKVMBackend(Backend):
    name = "jetkvm"
    capabilities = frozenset({commands.KEYBOARD, commands.MOUSE, commands.SNAPSHOT})

    def __init__(self, target: str, settings: Settings) -> None:
        super().__init__(target, settings)
        self.url = settings.get_str("url").rstrip("/")
        if not self.url.startswith(("https://", "http://")):
            raise ConfigError("target %r: url must start with https:// or http://" % target)
        self.password = settings.get_password("password_file", None)
        self.timeout = settings.get_float("timeout", 30.0)
        self.idle_timeout = settings.get_float("idle_timeout", 60.0)
        ca_file = settings.get_path("ca_file", None)
        verify_tls = settings.get_bool("verify_tls", True)
        settings.finish()
        if self.timeout <= 0 or self.idle_timeout <= 0:
            raise ConfigError("target %r: timeout and idle_timeout must be more than 0" % target)
        if not HAVE_WEBRTC:
            # Not a ConfigError: the other targets work without WebRTC.
            LOG.warning("%s: %s", target, NO_WEBRTC)
        self.tls = tls_context(ca_file, verify_tls)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: _Session | None = None
        self._idle: asyncio.TimerHandle | None = None
        self._closing: asyncio.Future | None = None
        # The JetKVM releases keys when a session ends, so the session
        # stays while anything is held.
        self._held_keys: list[int] = []
        self._buttons = 0
        # Where koid last put the pointer: every report says where it is.
        self._pointer = (0, 0)
        self._screen: tuple[int, int] | None = None

    @property
    def resource(self) -> str:
        return "jetkvm:%s" % self.url

    def describe(self) -> dict:
        description = super().describe()
        description["url"] = self.url
        return description

    # ------------------------------------------------------------------
    # The session, on a private event loop
    # ------------------------------------------------------------------

    def _run(self, work, *args, video: bool = False, extra: float | None = 0.0):
        """Run work(session, *args) on the event loop, with a session that
        has video if asked.

        Gives up after five timeouts and extra seconds, or never if extra
        is None, for work that takes as long as its data.
        """
        if not HAVE_WEBRTC:
            raise BackendError(NO_WEBRTC)
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                            name="jetkvm %s" % self.target)
            self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._use(video, work, *args), self._loop)
        limit = None if extra is None else 5 * self.timeout + extra
        try:
            return future.result(limit)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise BackendError("%s did not finish within %g s" % (self.url, limit)) from None

    async def _use(self, video: bool, work, *args):
        if self._idle is not None:
            self._idle.cancel()
        try:
            session = self._session
            if session is None or session.lost or (video and not session.has_video):
                await self._drop()
                session = _Session(self, video)
                try:
                    await session.open(await asyncio.to_thread(self._login))
                except BaseException:
                    await session.close()
                    raise
                LOG.info("%s: connected to %s", self.target, self.url)
                self._session = session
                # A session that ends lets go of what was held.
                if self._held_keys:
                    await session.macro([(self._held_keys, 0)])
                if self._buttons:
                    session.send(pointer_message(*self._pointer, self._buttons))
            return await work(session, *args)
        finally:
            self._schedule_expiry()

    def _schedule_expiry(self) -> None:
        session = self._session
        delay = VIDEO_IDLE if session is not None and session.has_video else self.idle_timeout
        self._idle = asyncio.get_running_loop().call_later(delay, self._expire)

    def _expire(self) -> None:
        if self._held_keys or self._buttons:
            self._schedule_expiry()
        elif self._session is not None:
            # Taken away now, so that a command cannot start on it.
            session, self._session = self._session, None
            self._closing = asyncio.ensure_future(session.close())

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
        closing, self._closing = self._closing, None
        if closing is not None:
            # A failed close must not fail every command after it.
            await asyncio.gather(closing, return_exceptions=True)

    def close(self) -> None:
        if self._loop is None:
            return
        if self._idle is not None:
            self._loop.call_soon_threadsafe(self._idle.cancel)
        try:
            asyncio.run_coroutine_threadsafe(self._drop(), self._loop).result(self.timeout)
        except Exception as error:
            LOG.warning("%s: could not close the session: %s", self.target, error)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(self.timeout)
        if self._thread.is_alive():
            # A daemon thread, stuck in blocking I/O; it ends with koid.
            LOG.warning("%s: the event loop did not stop", self.target)
        else:
            self._loop.close()
        self._loop = None

    def _login(self) -> str | None:
        """Log in, if there is a password; the authToken cookie, or None."""
        if self.password is None:
            return None
        body = json.dumps({"password": self.password}).encode()
        request = urllib.request.Request(self.url + LOGIN_PATH, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self.tls) as response:
                cookies = http.cookies.SimpleCookie()
                for header in response.headers.get_all("Set-Cookie") or []:
                    cookies.load(header)
        except urllib.error.HTTPError as error:
            if error.code == 400:
                # Login is disabled in noPassword mode.
                return None
            if error.code == 401:
                raise BackendError("%s refused the password" % self.url) from error
            if error.code == 429:
                raise Busy("%s: too many login attempts; try again later" % self.url) from error
            raise BackendError("%s answered HTTP %d to the login"
                               % (self.url, error.code)) from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise BackendError("cannot reach %s: %s" % (self.url, reason)) from error
        if "authToken" not in cookies:
            raise BackendError("%s did not give koid a session cookie" % self.url)
        return cookies["authToken"].value

    # ------------------------------------------------------------------
    # Status and screen
    # ------------------------------------------------------------------

    async def _video(self, session: _Session) -> dict:
        state = await session.call("getVideoState") or {}
        session.video = state
        if state.get("ready") and state.get("width") and state.get("height"):
            self._screen = (state["width"], state["height"])
        return state

    def status(self) -> dict:
        state = self._run(self._video)
        video = None
        if state.get("ready"):
            video = {"width": state.get("width"), "height": state.get("height")}
        return {"online": True, "video": video}

    async def _frame(self, session: _Session):
        state = await self._video(session)
        if not state.get("ready"):
            raise BackendError("no video signal from the target (%s)"
                               % (state.get("error") or "not ready"))
        return await session.frame(VIDEO_WAIT)

    def snapshot(self) -> Snapshot:
        picture = self._run(self._frame, video=True).to_image()
        data = io.BytesIO()
        picture.save(data, "PNG")
        self._screen = picture.size
        return Snapshot("png", data.getvalue(), picture.width, picture.height)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def type_text(self, text: str) -> None:
        if text:
            steps = text_steps(text, self._held_keys)
            self._run(lambda session: session.macro(steps, paste=True), extra=_duration(steps))

    def press(self, combinations: list[list[int]]) -> None:
        steps = press_steps(combinations, self._held_keys)
        self._run(lambda session: session.macro(steps), extra=_duration(steps))
        # Released, even if they were held before.
        self._held_keys = [usage for usage in self._held_keys
                           if not any(usage in usages for usages in combinations)]

    def key_down(self, usage: int) -> None:
        held = self._held_keys + [usage] if usage not in self._held_keys else self._held_keys
        self._run(lambda session: session.macro([(held, 0)]))
        self._held_keys = held

    def key_up(self, usage: int) -> None:
        held = [key for key in self._held_keys if key != usage]
        self._run(lambda session: session.macro([(held, 0)]))
        self._held_keys = held

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    async def _pointer_at(self, session: _Session, x: int, y: int) -> None:
        size = self._screen
        if session.video.get("ready") and session.video.get("width"):
            size = (session.video["width"], session.video["height"])
        if size is None:
            state = await self._video(session)
            if not state.get("ready"):
                raise BackendError("no video signal from the target, so pixel positions "
                                   "cannot be placed")
            size = self._screen
        self._pointer = (to_mouse_range(x, size[0]), to_mouse_range(y, size[1]))
        session.send(pointer_message(*self._pointer, self._buttons))

    async def _move(self, session: _Session, x: int, y: int) -> None:
        await self._pointer_at(session, x, y)
        await session.flush()

    def mouse_move(self, x: int, y: int) -> None:
        self._run(self._move, x, y)

    async def _click(self, session: _Session, bit: int, count: int, x, y) -> None:
        if x is not None:
            await self._pointer_at(session, x, y)
        for click in range(count):
            if click:
                await asyncio.sleep(CLICK_INTERVAL)
            session.send(pointer_message(*self._pointer, self._buttons | bit))
            session.send(pointer_message(*self._pointer, self._buttons & ~bit))
        await session.flush()

    def mouse_click(self, button: str, count: int, x: int | None, y: int | None) -> None:
        self._run(self._click, BUTTONS[button], count, x, y)
        self._buttons &= ~BUTTONS[button]

    async def _buttons_now(self, session: _Session, buttons: int) -> None:
        session.send(pointer_message(*self._pointer, buttons))
        await session.flush()

    def mouse_down(self, button: str) -> None:
        self._run(self._buttons_now, self._buttons | BUTTONS[button])
        self._buttons |= BUTTONS[button]

    def mouse_up(self, button: str) -> None:
        self._run(self._buttons_now, self._buttons & ~BUTTONS[button])
        self._buttons &= ~BUTTONS[button]

    async def _scroll(self, session: _Session, notches: int) -> None:
        # Positive scrolls up, as with a USB wheel.  Firmware 0.5.10 takes
        # the wheel on rpc only, not on hidrpc.
        await session.call("wheelReport", wheelY=notches, wheelX=0)

    def mouse_scroll(self, notches: int) -> None:
        self._run(self._scroll, notches)
