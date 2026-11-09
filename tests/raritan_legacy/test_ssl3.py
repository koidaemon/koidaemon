# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import hashlib
import pathlib
import socket
import threading
import warnings

import pytest

from koidaemon.backends.raritan_legacy import ssl3

from fake_ssl3_server import SSL3Server, throwaway_certificate

DATA = pathlib.Path(__file__).parent / "data"


def run_pair(server_work, client_work):
    """Run a fake SSL server and the real client on a socket pair."""
    client_sock, server_sock = socket.socketpair()
    result = {}

    def server():
        try:
            result["server"] = server_work(SSL3Server(server_sock))
        except Exception as error:
            result["server_error"] = error

    thread = threading.Thread(target=server)
    thread.start()
    try:
        result["client"] = client_work(ssl3.SSL3Client(client_sock))
    finally:
        thread.join(timeout=10)
        client_sock.close()
        server_sock.close()

    if "server_error" in result:
        raise result["server_error"]
    return result


def test_handshake_and_data_both_ways():
    def server(conn):
        conn.handshake()
        received = conn.recv_exact(5)
        conn.sendall(b"pong!" * 4000)  # longer than one record
        return received

    def client(conn):
        conn.handshake()
        conn.sendall(b"ping!")
        return conn.recv_exact(20000)

    result = run_pair(server, client)
    assert result["server"] == b"ping!"
    assert result["client"] == b"pong!" * 4000


def test_client_reads_the_server_certificate():
    def server(conn):
        conn.handshake()

    def client(conn):
        conn.handshake()
        return conn.server_certificate, conn.server_public_key

    certificate, public_key = run_pair(server, client)["client"]
    key, expected_certificate = throwaway_certificate()
    assert certificate == expected_certificate
    assert public_key.public_numbers() == key.public_key().public_numbers()


def test_public_key_from_the_switch_certificate():
    # The certificate a real Dominion KX sent.  Its serial number is 0,
    # which the cryptography package warns about when it parses the whole
    # certificate, so make any warning fail the test.
    certificate = (DATA / "kx_certificate.der").read_bytes()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        public_key = ssl3.certificate_public_key(certificate)

    numbers = public_key.public_numbers()
    assert public_key.key_size == 1024
    assert numbers.e == 65537
    # The start of the modulus, as printed by "openssl x509 -modulus".
    assert ("%X" % numbers.n).startswith("C4C3E60F23C6395CA4C8753BD09F6ED17BEC000A7B676B3E6370")


def test_garbage_certificate_is_an_ssl_error():
    with pytest.raises(ssl3.SSLError, match="server certificate"):
        ssl3.certificate_public_key(b"\x30\x82\x01\x00\x30")


def test_tampered_record_is_rejected():
    def server(conn):
        conn.handshake()
        # Flip a bit inside an encrypted record by hand.
        mac = ssl3.record_mac(conn.write_mac, conn.write_seq, ssl3.APPLICATION_DATA, b"data")
        body = bytearray(conn.write_cipher.crypt(b"data" + mac))
        body[0] ^= 1
        conn.sock.sendall(bytes([ssl3.APPLICATION_DATA]) + ssl3.SSL3_VERSION
                          + len(body).to_bytes(2, "big") + bytes(body))

    def client(conn):
        conn.handshake()
        with pytest.raises(ssl3.SSLError, match="MAC"):
            conn.recv_exact(4)

    run_pair(server, client)


def test_leftover_returns_unread_application_data():
    def server(conn):
        conn.handshake()
        conn.sendall(b"abcdef")

    def client(conn):
        conn.handshake()
        first = conn.recv_exact(2)
        return first, conn.leftover(), conn.leftover()

    assert run_pair(server, client)["client"] == (b"ab", b"cdef", b"")


def test_derive_secret_matches_the_ssl3_definition():
    secret, seed = b"secret", b"seed"
    expected = b""
    for label in (b"A", b"BB", b"CCC"):
        expected += hashlib.md5(secret + hashlib.sha1(label + secret + seed).digest()).digest()
    assert ssl3.derive_secret(secret, seed, 48) == expected
    assert ssl3.derive_secret(secret, seed, 20) == expected[:20]


def test_handshake_message_header():
    assert ssl3.handshake_message(14, b"") == b"\x0e\x00\x00\x00"
    assert ssl3.handshake_message(1, b"abc") == b"\x01\x00\x00\x03abc"
