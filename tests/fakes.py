# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A backend that records its calls, for testing everything above backends."""

from __future__ import annotations

import datetime
import ipaddress
import pathlib
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from koidaemon import commands
from koidaemon.backends.base import Backend, Snapshot

FAKE_PNG = b"\x89PNG\r\n\x1a\nfake"


class RecordingBackend(Backend):
    name = "fake"

    def __init__(self, target: str = "board", capabilities=commands.ALL_CAPABILITIES,
                 resource: str | None = None) -> None:
        super().__init__(target, None)
        self.capabilities = frozenset(capabilities)
        self._resource = resource or "fake:%s" % target
        self.calls: list[tuple] = []
        # Set to an exception to make every call raise it.
        self.error: Exception | None = None

    @property
    def resource(self) -> str:
        return self._resource

    def _record(self, *call) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(call)

    def status(self) -> dict:
        self._record("status")
        return {"online": True, "video": {"width": 640, "height": 480}}

    def snapshot(self) -> Snapshot:
        self._record("snapshot")
        return Snapshot("png", FAKE_PNG, 640, 480)

    def type_text(self, text):
        self._record("type_text", text)

    def press(self, combinations):
        self._record("press", combinations)

    def key_down(self, usage):
        self._record("key_down", usage)

    def key_up(self, usage):
        self._record("key_up", usage)

    def mouse_move(self, x, y):
        self._record("mouse_move", x, y)

    def mouse_click(self, button, count, x, y):
        self._record("mouse_click", button, count, x, y)

    def mouse_down(self, button):
        self._record("mouse_down", button)

    def mouse_up(self, button):
        self._record("mouse_up", button)

    def mouse_scroll(self, notches):
        self._record("mouse_scroll", notches)

    def storage_insert(self, image: pathlib.Path, mode, writable):
        self._record("storage_insert", image, mode, writable)
        return {"inserted": True, "image": image.name, "mode": mode, "writable": writable}

    def storage_eject(self):
        self._record("storage_eject")

    def storage_status(self):
        self._record("storage_status")
        return {"inserted": False, "image": None, "mode": "stick", "writable": False}


def make_certificate(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A self-signed certificate for 127.0.0.1, and its key, as PEM files."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "koid-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    address = x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
    certificate = (x509.CertificateBuilder()
                   .subject_name(name)
                   .issuer_name(name)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now)
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([address]), critical=False)
                   .sign(key, hashes.SHA256()))

    cert_file = directory / "test.pem"
    key_file = directory / "test.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_file, key_file


def short_socket_path() -> pathlib.Path:
    """A socket path short enough for AF_UNIX (about 107 bytes at most)."""
    return pathlib.Path(tempfile.mkdtemp(prefix="koid-", dir="/tmp")) / "koid.sock"
