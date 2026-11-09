# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Login checks against a session captured from Raritan's own client."""

import pytest

from koidaemon.backends.raritan_legacy import auth, packets
from koidaemon.backends.raritan_legacy.rc4 import RC4


@pytest.fixture
def exchange(capture):
    """The challenge values both sides used in the captured login."""
    client_packet = capture["client_challenge"]
    ours = auth.ClientChallenge(client_packet[8:72], client_packet[72:136])
    theirs = auth.ServerChallenge.parse(capture["server_challenge"])
    response = auth.ServerResponse.parse(capture["server_response"])
    server = packets.ServerId.parse(capture["server_id"])
    return ours, theirs, response, server


def test_client_challenge_packet_layout(exchange, capture):
    ours, _theirs, _response, _server = exchange
    assert ours.pack() == capture["client_challenge"]


def test_client_response_matches_the_official_client(exchange, capture):
    ours, theirs, _response, _server = exchange
    assert auth.client_response(ours, theirs) == capture["client_response"]


def test_real_switch_passes_the_server_check(exchange):
    ours, theirs, response, _server = exchange
    auth.check_server_response(response, ours, theirs)


def test_wrong_private_key_fails_the_server_check(exchange):
    ours, theirs, response, _server = exchange
    with pytest.raises(auth.LoginError):
        auth.check_server_response(response, ours, theirs, private_key=b"x" * 24)


def test_session_key_matches_the_capture(exchange, capture):
    ours, theirs, _response, _server = exchange
    assert auth.session_key(ours, theirs) == capture["rc4_key"]


def test_session_key_decrypts_the_traffic_after_login(exchange, capture):
    ours, theirs, _response, _server = exchange
    plain = RC4(auth.session_key(ours, theirs)).crypt(capture["write_socket_first_rc4_bytes"])
    # The official client's first command after login is a database query.
    assert plain[2] == packets.CMD_DATABASE_REQUEST
    assert b"<Database><Get><Select>/System/Device[@Type='IP-Reach']/Port" in plain


def test_chap_access_request_matches_the_official_client(exchange, capture):
    _ours, _theirs, response, server = exchange
    request = auth.access_request("admin", capture["password"], server, response)
    assert request == capture["access_request"]


def test_pap_access_request_layout(exchange):
    _ours, _theirs, response, server = exchange
    server.security_flags = packets.SECURITY_SSL_LOGIN_THEN_RC4  # PAP login type
    request = auth.access_request("admin", "raritan", server, response)

    # Worked example: header, flags, permissions, RADIUS header, user name,
    # then the password zero-padded to 16 bytes.
    expected = bytes.fromhex("00390600" "00000000" "00000000" "0100002d") + bytes(16)
    expected += b"\x01\x07admin" + b"\x02\x12raritan" + bytes(9)
    assert request == expected


def test_pap_password_padding():
    assert auth.pap_password(b"") == bytes(16)
    assert auth.pap_password(b"a" * 16) == b"a" * 16
    assert auth.pap_password(b"a" * 17) == b"a" * 17 + bytes(15)


def test_accepted_login(capture):
    auth.check_access_response(capture["access_response"])


def test_rejected_login():
    radius = bytes([auth.RADIUS_ACCESS_REJECT, 0, 0, 20]) + bytes(16)
    packet = packets.pack_command(packets.RSP_ACCESS_RESPONSE, bytes(8) + radius)
    with pytest.raises(auth.LoginError, match="wrong user name or password"):
        auth.check_access_response(packet)


def test_expired_password_is_reported():
    radius = bytes([auth.RADIUS_ACCESS_ACCEPT, 0, 0, 20]) + bytes(16)
    flags = auth.FLAG_PASSWORD_EXPIRED.to_bytes(4, "big") + bytes(4)
    packet = packets.pack_command(packets.RSP_ACCESS_RESPONSE, flags + radius)
    with pytest.raises(auth.LoginError, match="expired"):
        auth.check_access_response(packet)


def test_server_challenge_must_be_the_right_packet():
    with pytest.raises(auth.LoginError):
        auth.ServerChallenge.parse(bytes(136))
