# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""A minimal SSL 3.0 client.

The Dominion KX upgrades its plain TCP connection to SSL 3.0 part-way
through the protocol, and only offers the SSL_RSA_WITH_RC4_128_MD5 cipher
suite.  Current OpenSSL builds (and so Python's ssl module) support neither
SSL 3.0 nor RC4, so this module implements just enough of RFC 6101 to talk
to the switch:

  * client side only,
  * one cipher suite: RSA key exchange, RC4-128, MD5 MAC,
  * no session resumption, renegotiation or client certificates.

The server certificate is NOT verified.  The switch presents a self-signed
certificate that expired years ago, and Raritan's own client trusts anything
as well.  Treat the connection as readable, and the switch as possible to
impersonate, by anyone on the network path.
"""

from __future__ import annotations

import hashlib
import os
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from koidaemon.backends.raritan_legacy.rc4 import RC4

SSL3_VERSION = b"\x03\x00"
RSA_WITH_RC4_128_MD5 = 0x0004

# Record content types.
CHANGE_CIPHER_SPEC = 20
ALERT = 21
HANDSHAKE = 22
APPLICATION_DATA = 23

# Handshake message types.
CLIENT_HELLO = 1
SERVER_HELLO = 2
CERTIFICATE = 11
SERVER_HELLO_DONE = 14
CLIENT_KEY_EXCHANGE = 16
FINISHED = 20

# Sender labels used in the Finished message hashes.
CLIENT_SENDER = b"CLNT"
SERVER_SENDER = b"SRVR"

MAX_FRAGMENT = 16384
MD5_PAD1 = b"\x36" * 48
MD5_PAD2 = b"\x5c" * 48
SHA_PAD1 = b"\x36" * 40
SHA_PAD2 = b"\x5c" * 40


class SSLError(Exception):
    """The SSL handshake or record layer failed."""


def derive_secret(secret: bytes, seed: bytes, length: int) -> bytes:
    """The SSL 3.0 key derivation function.

    Used both to turn the pre-master secret into the master secret and to
    expand the master secret into the key block:

        MD5(secret + SHA("A" + secret + seed)) +
        MD5(secret + SHA("BB" + secret + seed)) +
        MD5(secret + SHA("CCC" + secret + seed)) + ...
    """
    output = b""
    round_number = 1
    while len(output) < length:
        label = bytes([ord("A") + round_number - 1]) * round_number
        inner = hashlib.sha1(label + secret + seed).digest()
        output += hashlib.md5(secret + inner).digest()
        round_number += 1
    return output[:length]


def record_mac(mac_secret: bytes, sequence: int, content_type: int, data: bytes) -> bytes:
    """MD5 MAC of one record, as defined for SSL 3.0 (not HMAC)."""
    header = struct.pack(">QBH", sequence, content_type, len(data))
    inner = hashlib.md5(mac_secret + MD5_PAD1 + header + data).digest()
    return hashlib.md5(mac_secret + MD5_PAD2 + inner).digest()


def finished_hash(handshake_messages: bytes, sender: bytes, master_secret: bytes) -> bytes:
    """The 36-byte verify data carried in a Finished message."""
    md5_inner = hashlib.md5(handshake_messages + sender + master_secret + MD5_PAD1).digest()
    md5_part = hashlib.md5(master_secret + MD5_PAD2 + md5_inner).digest()

    sha_inner = hashlib.sha1(handshake_messages + sender + master_secret + SHA_PAD1).digest()
    sha_part = hashlib.sha1(master_secret + SHA_PAD2 + sha_inner).digest()

    return md5_part + sha_part


def handshake_message(message_type: int, body: bytes) -> bytes:
    """Wrap a handshake body in its 1-byte type and 3-byte length header."""
    length = struct.pack(">I", len(body))[1:]
    return bytes([message_type]) + length + body


def _der_element(data: bytes, offset: int) -> tuple[int, int]:
    """Find the DER element at offset: returns (start of contents, end)."""
    length = data[offset + 1]
    start = offset + 2
    # Lengths over 127 are given as 0x80 + the number of length bytes,
    # followed by the length itself.
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[start:start + count], "big")
        start += count
    end = start + length
    if end > len(data):
        raise SSLError("the server certificate is truncated")
    return start, end


def certificate_public_key(certificate: bytes) -> rsa.RSAPublicKey:
    """Get the RSA public key out of a DER-encoded X.509 certificate.

    Nothing else in the certificate is used or checked, so rather than
    parse all of it, walk the DER structure just far enough to find the
    key.  This matters for the switch's certificate: its serial number is
    0, which RFC 5280 forbids, so the cryptography package warns about it
    today and will refuse to load it in a future release.

    The layout, from RFC 5280:

      Certificate ::= SEQUENCE {
          tbsCertificate       SEQUENCE {
              version          [0] EXPLICIT, optional
              serialNumber, signature, issuer, validity, subject,
              subjectPublicKeyInfo,
              ... }
          signatureAlgorithm, signatureValue }
    """
    try:
        tbs_start, _ = _der_element(certificate, 0)
        offset, _ = _der_element(certificate, tbs_start)

        # Skip the optional version, then the five fields before the key.
        if certificate[offset] == 0xA0:
            _, offset = _der_element(certificate, offset)
        for _field in range(5):
            _, offset = _der_element(certificate, offset)

        _, key_end = _der_element(certificate, offset)
        public_key = serialization.load_der_public_key(certificate[offset:key_end])
    except (IndexError, ValueError) as error:
        raise SSLError("cannot read the server certificate: %s" % error) from error

    if not isinstance(public_key, rsa.RSAPublicKey):
        raise SSLError("the server certificate does not hold an RSA key")
    return public_key


class SSL3Client:
    """An SSL 3.0 connection on top of an already connected socket."""

    def __init__(self, sock) -> None:
        self._sock = sock
        self._handshake_messages = b""
        self._handshake_buffer = b""
        self._app_buffer = b""

        # Set once ChangeCipherSpec has been sent / received.
        self._write_cipher: RC4 | None = None
        self._read_cipher: RC4 | None = None
        self._write_mac_secret = b""
        self._read_mac_secret = b""
        self._write_sequence = 0
        self._read_sequence = 0

        # The server's certificate (DER) and the RSA key taken from it.
        self.server_certificate: bytes | None = None
        self.server_public_key: rsa.RSAPublicKey | None = None

    # ------------------------------------------------------------------
    # Record layer
    # ------------------------------------------------------------------

    def _send_record(self, content_type: int, data: bytes) -> None:
        for offset in range(0, max(len(data), 1), MAX_FRAGMENT):
            fragment = data[offset:offset + MAX_FRAGMENT]

            if self._write_cipher is not None:
                mac = record_mac(self._write_mac_secret, self._write_sequence,
                                 content_type, fragment)
                fragment = self._write_cipher.crypt(fragment + mac)
                self._write_sequence += 1

            header = struct.pack(">B2sH", content_type, SSL3_VERSION, len(fragment))
            self._sock.sendall(header + fragment)

    def _recv_record(self) -> tuple[int, bytes]:
        header = recv_exact(self._sock, 5)
        content_type, _version, length = struct.unpack(">B2sH", header)
        body = recv_exact(self._sock, length)

        if self._read_cipher is not None:
            body = self._read_cipher.crypt(body)
            data = body[:-16]
            mac = body[-16:]
            expected = record_mac(self._read_mac_secret, self._read_sequence,
                                  content_type, data)
            if mac != expected:
                raise SSLError("record MAC check failed")
            self._read_sequence += 1
            body = data

        if content_type == ALERT:
            raise SSLError("server sent alert level %d, description %d" % (body[0], body[1]))

        return content_type, body

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def _send_handshake(self, message_type: int, body: bytes) -> None:
        message = handshake_message(message_type, body)
        self._handshake_messages += message
        self._send_record(HANDSHAKE, message)

    def _recv_handshake(self, expected_type: int) -> bytes:
        """Read the next handshake message, which must be of expected_type."""
        # Several handshake messages can share one record, and one message can
        # span records, so collect bytes until a whole message is available.
        while True:
            buffered = self._handshake_buffer
            if len(buffered) >= 4:
                length = int.from_bytes(buffered[1:4], "big")
                if len(buffered) >= 4 + length:
                    break

            content_type, data = self._recv_record()
            if content_type != HANDSHAKE:
                raise SSLError("expected a handshake record, got type %d" % content_type)
            self._handshake_buffer += data

        length = int.from_bytes(self._handshake_buffer[1:4], "big")
        message = self._handshake_buffer[:4 + length]
        self._handshake_buffer = self._handshake_buffer[4 + length:]
        self._handshake_messages += message

        if message[0] != expected_type:
            raise SSLError("expected handshake message %d, got %d" % (expected_type, message[0]))
        return message[4:]

    def handshake(self) -> None:
        """Run the full client handshake.  Raises SSLError on failure."""
        client_random = os.urandom(32)

        # ClientHello: version, random, empty session id, one cipher suite,
        # null compression only.
        hello = SSL3_VERSION + client_random
        hello += b"\x00"
        hello += struct.pack(">HH", 2, RSA_WITH_RC4_128_MD5)
        hello += b"\x01\x00"
        self._send_handshake(CLIENT_HELLO, hello)

        server_hello = self._recv_handshake(SERVER_HELLO)
        server_random = server_hello[2:34]
        session_id_length = server_hello[34]
        suite_offset = 35 + session_id_length
        suite = struct.unpack(">H", server_hello[suite_offset:suite_offset + 2])[0]
        if server_hello[:2] != SSL3_VERSION or suite != RSA_WITH_RC4_128_MD5:
            raise SSLError("server picked version %s, cipher suite 0x%04x"
                           % (server_hello[:2].hex(), suite))

        # Certificate: a 3-byte list length, then 3-byte length + DER for
        # each certificate.  Only the first (the server's own) is needed.
        certificates = self._recv_handshake(CERTIFICATE)
        first_length = int.from_bytes(certificates[3:6], "big")
        first_der = certificates[6:6 + first_length]
        self.server_certificate = first_der
        self.server_public_key = certificate_public_key(first_der)

        self._recv_handshake(SERVER_HELLO_DONE)

        # ClientKeyExchange: the pre-master secret, RSA-encrypted with the
        # server's public key.  Unlike TLS, SSL 3.0 sends it without a
        # length prefix.
        pre_master = SSL3_VERSION + os.urandom(46)
        encrypted = self.server_public_key.encrypt(pre_master, padding.PKCS1v15())
        self._send_handshake(CLIENT_KEY_EXCHANGE, encrypted)

        master = derive_secret(pre_master, client_random + server_random, 48)
        key_block = derive_secret(master, server_random + client_random, 64)
        client_mac_secret = key_block[0:16]
        server_mac_secret = key_block[16:32]
        client_key = key_block[32:48]
        server_key = key_block[48:64]

        # Our Finished covers every handshake message so far, so compute it
        # before sending it (it is then added to the transcript).
        client_finished = finished_hash(self._handshake_messages, CLIENT_SENDER, master)
        self._send_record(CHANGE_CIPHER_SPEC, b"\x01")
        self._write_cipher = RC4(client_key)
        self._write_mac_secret = client_mac_secret
        self._send_handshake(FINISHED, client_finished)

        expected_server_finished = finished_hash(self._handshake_messages, SERVER_SENDER, master)
        content_type, _ = self._recv_record()
        if content_type != CHANGE_CIPHER_SPEC:
            raise SSLError("expected ChangeCipherSpec, got record type %d" % content_type)
        self._read_cipher = RC4(server_key)
        self._read_mac_secret = server_mac_secret

        server_finished = self._recv_handshake(FINISHED)
        if server_finished != expected_server_finished:
            raise SSLError("server Finished message did not verify")

    # ------------------------------------------------------------------
    # Application data
    # ------------------------------------------------------------------

    def sendall(self, data: bytes) -> None:
        self._send_record(APPLICATION_DATA, data)

    def recv_exact(self, length: int) -> bytes:
        while len(self._app_buffer) < length:
            content_type, data = self._recv_record()
            if content_type == APPLICATION_DATA:
                self._app_buffer += data
        data = self._app_buffer[:length]
        self._app_buffer = self._app_buffer[length:]
        return data

    def leftover(self) -> bytes:
        """Application bytes already decrypted but not yet read.

        Needed when the protocol drops SSL and carries on in the clear (or
        with plain RC4) on the same socket.
        """
        data = self._app_buffer
        self._app_buffer = b""
        return data


def recv_exact(sock, length: int) -> bytes:
    """Read exactly length bytes from a socket, or raise ConnectionError."""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed after %d of %d bytes" % (len(data), length))
        data += chunk
    return data
