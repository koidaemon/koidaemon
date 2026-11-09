# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""The server side of an SSL 3.0 handshake, for testing the client.

It reuses the key derivation and MAC functions from koidaemon.backends.raritan_legacy.ssl3, so it
checks the client's message flow and record layer rather than those
functions themselves (test_ssl3.py has separate checks for those).
"""

from __future__ import annotations

import datetime
import os
import struct

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from koidaemon.backends.raritan_legacy import ssl3
from koidaemon.backends.raritan_legacy.rc4 import RC4

_KEY = None
_CERTIFICATE_DER = None


def throwaway_certificate() -> tuple[rsa.RSAPrivateKey, bytes]:
    """A throwaway self-signed certificate, generated once per test run."""
    global _KEY, _CERTIFICATE_DER
    if _KEY is None:
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-kx")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder()
                       .subject_name(name)
                       .issuer_name(name)
                       .public_key(_KEY.public_key())
                       .serial_number(1)
                       .not_valid_before(now)
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .sign(_KEY, hashes.SHA256()))
        _CERTIFICATE_DER = certificate.public_bytes(serialization.Encoding.DER)
    return _KEY, _CERTIFICATE_DER


class SSL3Server:
    """Server end of an SSL 3.0 connection over an accepted socket."""

    def __init__(self, sock) -> None:
        self.sock = sock
        self.transcript = b""
        self.buffer = b""
        self.app_buffer = b""
        self.read_cipher = None
        self.write_cipher = None
        self.read_mac = b""
        self.write_mac = b""
        self.read_seq = 0
        self.write_seq = 0

    def _send_record(self, content_type: int, data: bytes) -> None:
        if self.write_cipher is not None:
            mac = ssl3.record_mac(self.write_mac, self.write_seq, content_type, data)
            data = self.write_cipher.crypt(data + mac)
            self.write_seq += 1
        header = struct.pack(">B2sH", content_type, ssl3.SSL3_VERSION, len(data))
        self.sock.sendall(header + data)

    def _recv_record(self) -> tuple[int, bytes]:
        content_type, _version, length = struct.unpack(">B2sH", ssl3.recv_exact(self.sock, 5))
        body = ssl3.recv_exact(self.sock, length)
        if self.read_cipher is not None:
            body = self.read_cipher.crypt(body)
            data, mac = body[:-16], body[-16:]
            assert mac == ssl3.record_mac(self.read_mac, self.read_seq, content_type, data)
            self.read_seq += 1
            body = data
        return content_type, body

    def _have_whole_message(self) -> bool:
        if len(self.buffer) < 4:
            return False
        length = int.from_bytes(self.buffer[1:4], "big")
        return len(self.buffer) >= 4 + length

    def _recv_handshake(self) -> tuple[int, bytes]:
        while not self._have_whole_message():
            content_type, data = self._recv_record()
            assert content_type == ssl3.HANDSHAKE, content_type
            self.buffer += data
        length = int.from_bytes(self.buffer[1:4], "big")
        message = self.buffer[:4 + length]
        self.buffer = self.buffer[4 + length:]
        self.transcript += message
        return message[0], message[4:]

    def _send_handshake(self, message_type: int, body: bytes) -> None:
        message = ssl3.handshake_message(message_type, body)
        self.transcript += message
        self._send_record(ssl3.HANDSHAKE, message)

    def handshake(self) -> None:
        key, certificate = throwaway_certificate()

        message_type, hello = self._recv_handshake()
        assert message_type == ssl3.CLIENT_HELLO
        assert hello[:2] == ssl3.SSL3_VERSION
        client_random = hello[2:34]

        server_random = os.urandom(32)
        server_hello = ssl3.SSL3_VERSION + server_random + b"\x00"
        server_hello += struct.pack(">HB", ssl3.RSA_WITH_RC4_128_MD5, 0)
        self._send_handshake(ssl3.SERVER_HELLO, server_hello)

        certificate_entry = len(certificate).to_bytes(3, "big") + certificate
        certificate_list = len(certificate_entry).to_bytes(3, "big") + certificate_entry
        self._send_handshake(ssl3.CERTIFICATE, certificate_list)
        self._send_handshake(ssl3.SERVER_HELLO_DONE, b"")

        message_type, encrypted = self._recv_handshake()
        assert message_type == ssl3.CLIENT_KEY_EXCHANGE
        pre_master = key.decrypt(encrypted, padding.PKCS1v15())

        master = ssl3.derive_secret(pre_master, client_random + server_random, 48)
        key_block = ssl3.derive_secret(master, server_random + client_random, 64)

        content_type, _ = self._recv_record()
        assert content_type == ssl3.CHANGE_CIPHER_SPEC
        self.read_cipher = RC4(key_block[32:48])
        self.read_mac = key_block[0:16]

        expected = ssl3.finished_hash(self.transcript, ssl3.CLIENT_SENDER, master)
        message_type, client_finished = self._recv_handshake()
        assert message_type == ssl3.FINISHED
        assert client_finished == expected, "client Finished did not verify"

        server_finished = ssl3.finished_hash(self.transcript, ssl3.SERVER_SENDER, master)
        self._send_record(ssl3.CHANGE_CIPHER_SPEC, b"\x01")
        self.write_cipher = RC4(key_block[48:64])
        self.write_mac = key_block[16:32]
        self._send_handshake(ssl3.FINISHED, server_finished)

    def sendall(self, data: bytes) -> None:
        self._send_record(ssl3.APPLICATION_DATA, data)

    def recv_exact(self, length: int) -> bytes:
        while len(self.app_buffer) < length:
            content_type, data = self._recv_record()
            assert content_type == ssl3.APPLICATION_DATA
            self.app_buffer += data
        data = self.app_buffer[:length]
        self.app_buffer = self.app_buffer[length:]
        return data
