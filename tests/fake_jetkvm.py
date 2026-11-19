# Copyright (c) 2026 Koen Kooi
#
# SPDX-License-Identifier: MIT

"""A fake JetKVM, for testing the jetkvm backend.

Like firmware 0.5.10: the login at /auth/login-local, the signaling
WebSocket at /webrtc/signaling/client, and a WebRTC peer (aiortc) that
sends H.264 video of a test picture and answers on the "rpc" (JSON-RPC)
and "hidrpc" data channels.  It records the keyboard and mouse reports it
is sent.
"""

from __future__ import annotations

import asyncio
import base64
import fractions
import http.server
import json
import struct
import threading
import unittest.mock

import aioice.ice
import av
from aiortc import (RTCConfiguration, RTCPeerConnection, RTCRtpSender,
                    RTCSessionDescription, VideoStreamTrack)
from PIL import Image, ImageDraw

from koidaemon import websocket

TOKEN = "fake-token"


def test_picture(width: int, height: int) -> Image.Image:
    """Red on the left half, blue on the right."""
    picture = Image.new("RGB", (width, height), (0, 0, 255))
    ImageDraw.Draw(picture).rectangle((0, 0, width // 2 - 1, height - 1), fill=(255, 0, 0))
    return picture


class _Video(VideoStreamTrack):
    def __init__(self, fake: "FakeJetKVM") -> None:
        super().__init__()
        self.fake = fake

    async def recv(self) -> av.VideoFrame:
        # Like the JetKVM, nothing is sent without a signal.
        while self.fake.signal is None:
            await asyncio.sleep(0.05)
        pts, time_base = await self.next_timestamp()
        frame = av.VideoFrame.from_image(test_picture(*self.fake.signal))
        frame.pts, frame.time_base = pts, time_base or fractions.Fraction(1, 90000)
        return frame


class FakeJetKVM:
    def __init__(self, password: str | None = None) -> None:
        # None for noPassword mode.
        self.password = password
        self.signal: tuple[int, int] | None = (320, 240)
        self.logins = 0
        self.sessions = 0
        # Of those, the ones that asked for video.
        self.video_sessions = 0
        self.calls: list[str] = []
        # Keyboard reports as (modifier, keys), pointer reports as
        # (x, y, buttons), wheel steps, and macros by step count.
        self.keyboard: list[tuple[int, bytes]] = []
        self.pointer: list[tuple[int, int, int]] = []
        self.wheel: list[int] = []
        self.macros: list[int] = []
        self._peers: list[tuple[RTCPeerConnection, list]] = []
        self._tasks: list[asyncio.Task] = []
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.url = "http://127.0.0.1:%d" % self._server.server_address[1]

    def __enter__(self) -> "FakeJetKVM":
        # ICE gathers candidates on every interface, and which of them
        # connect depends on the machine; only use loopback, in both peers.
        self._addresses = unittest.mock.patch.object(
            aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"])
        self._addresses.start()
        self._loop_thread.start()
        self._server_thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()
        asyncio.run_coroutine_threadsafe(self._close_peers(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(10)
        self._addresses.stop()

    async def _close_peers(self) -> None:
        for task in self._tasks:
            task.cancel()
        for pc, _channels in self._peers:
            await pc.close()

    @property
    def connected(self) -> int:
        """Peers still connected."""
        return sum(pc.connectionState == "connected" for pc, _channels in self._peers)

    def kick(self) -> None:
        """What the JetKVM does when another session, such as the web
        interface, takes over."""
        asyncio.run_coroutine_threadsafe(self._kick(), self._loop).result(10)

    async def _kick(self) -> None:
        for pc, channels in self._peers:
            for channel in channels:
                if channel.readyState == "open":
                    self._event(channel, "otherSessionConnected", None)
            await pc.close()

    # ------------------------------------------------------------------
    # HTTP and signaling
    # ------------------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path != "/auth/login-local":
                    return self._answer(404, {"error": "not found"})
                fake.logins += 1
                if fake.password is None:
                    return self._answer(400, {"error": "Login is disabled in noPassword mode"})
                if json.loads(body).get("password") != fake.password:
                    return self._answer(401, {"error": "Invalid password"})
                self._answer(200, {"message": "Login successful"},
                             {"Set-Cookie": "authToken=%s; Path=/; HttpOnly" % TOKEN})

            def do_GET(self) -> None:
                if self.path != "/webrtc/signaling/client":
                    return self._answer(404, {"error": "not found"})
                if fake.password is not None and \
                        "authToken=%s" % TOKEN not in self.headers.get("Cookie", ""):
                    return self._answer(401, {"error": "Unauthorized"})
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept",
                                 websocket.accept_key(self.headers["Sec-WebSocket-Key"]))
                self.end_headers()
                self.close_connection = True
                fake._signal(self)

            def _answer(self, status: int, body: dict, headers: dict | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def _signal(self, handler) -> None:
        def send(message: dict) -> None:
            # As Go's coder/websocket sends a message: a frame that is not
            # final, then an empty final one.
            frame = websocket.encode_frame(websocket.OPCODE_TEXT, json.dumps(message).encode())
            handler.wfile.write(b"\x01" + frame[1:])
            handler.wfile.write(websocket.encode_frame(websocket.OPCODE_CONTINUATION))
            handler.wfile.flush()

        send({"type": "device-metadata", "data": {"deviceVersion": "0.5.10"}})
        while True:
            try:
                opcode, payload = websocket.read_frame(handler.rfile)
            except (OSError, websocket.WebSocketError):
                return
            if opcode == websocket.OPCODE_CLOSE:
                handler.wfile.write(websocket.encode_frame(websocket.OPCODE_CLOSE, payload))
                return
            if opcode != websocket.OPCODE_TEXT:
                continue
            message = json.loads(payload)
            if message["type"] == "offer":
                offer = json.loads(base64.b64decode(message["data"]["sd"]))
                answering = asyncio.run_coroutine_threadsafe(self._answer(offer), self._loop)
                answer = answering.result(10)
                # The JetKVM trickles its ICE candidates, and may send them
                # before its answer.
                lines = answer["sdp"].splitlines()
                for line in lines:
                    if line.startswith("a=candidate:"):
                        send({"type": "new-ice-candidate",
                              "data": {"candidate": line[2:], "sdpMid": "0", "sdpMLineIndex": 0}})
                answer["sdp"] = "\r\n".join(line for line in lines if not line.startswith(
                    ("a=candidate:", "a=end-of-candidates"))) + "\r\n"
                send({"type": "answer",
                      "data": base64.b64encode(json.dumps(answer).encode()).decode()})

    async def _answer(self, offer: dict) -> dict:
        if self._peers:
            await self._kick()
        self.sessions += 1
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        channels: list = []
        self._peers.append((pc, channels))
        pc.on("datachannel", lambda channel: self._channel(channel, channels))
        await pc.setRemoteDescription(RTCSessionDescription(**offer))
        for transceiver in pc.getTransceivers():
            if transceiver.kind == "video":
                self.video_sessions += 1
                transceiver.sender.replaceTrack(_Video(self))
                transceiver.direction = "sendonly"
                transceiver.setCodecPreferences([
                    codec for codec in RTCRtpSender.getCapabilities("video").codecs
                    if codec.mimeType == "video/H264"])
        await pc.setLocalDescription(await pc.createAnswer())
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    def _channel(self, channel, channels: list) -> None:
        if channel.label == "rpc":
            channels.append(channel)
            channel.on("message", lambda message: self._rpc(channel, message))
            self._event(channel, "videoInputState", self._video_state())
        elif channel.label == "hidrpc":
            queue = asyncio.Queue()
            channel.on("message", queue.put_nowait)
            self._tasks.append(asyncio.ensure_future(self._hid(channel, queue)))
        else:
            channel.close()

    @staticmethod
    def _event(channel, method: str, params) -> None:
        channel.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _rpc(self, channel, message: str) -> None:
        request = json.loads(message)
        method, params = request["method"], request.get("params") or {}
        self.calls.append(method)
        handler = getattr(self, "_rpc_" + method, None)
        if handler is None:
            reply = {"error": {"code": -32601, "message": "Method not found"}}
        else:
            try:
                reply = {"result": handler(**params)}
            except Exception as error:
                reply = {"error": {"code": -32603, "message": "Internal error",
                                   "data": str(error)}}
        channel.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], **reply}))

    def _video_state(self) -> dict:
        if self.signal is None:
            return {"ready": False, "error": "no_signal", "width": 0, "height": 0, "fps": 0}
        width, height = self.signal
        return {"ready": True, "width": width, "height": height, "fps": 30}

    def _rpc_getVideoState(self) -> dict:
        return self._video_state()

    def _rpc_wheelReport(self, wheelY: int, wheelX: int) -> None:
        # Firmware 0.5.10 has no wheel message on hidrpc.
        self.wheel.append(wheelY)

    # ------------------------------------------------------------------
    # HID RPC
    # ------------------------------------------------------------------

    async def _hid(self, channel, queue: asyncio.Queue) -> None:
        # One message at a time, as the JetKVM's keyboard queue runs them.
        while True:
            message = await queue.get()
            kind, data = message[0], message[1:]
            if kind == 0x01:
                channel.send(bytes([0x01, 0x01]))
            elif kind == 0x02:
                self.keyboard.append((data[0], bytes(data[1:7]).ljust(6, b"\0")))
            elif kind == 0x03:
                self.pointer.append(struct.unpack(">iiB", data))
            elif kind == 0x07:
                paste, count = struct.unpack(">BI", data[:5])
                assert len(data) == 5 + 9 * count
                self.macros.append(count)
                channel.send(bytes([0x34, 1, paste]))
                for offset in range(5, len(data), 9):
                    modifier, keys, delay = struct.unpack(">B6sH", data[offset:offset + 9])
                    self.keyboard.append((modifier, keys))
                    await asyncio.sleep(delay / 1000)
                channel.send(bytes([0x34, 0, paste]))
