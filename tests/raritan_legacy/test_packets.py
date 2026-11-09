# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pytest

from koidaemon.backends.raritan_legacy import packets


def test_csc_framing_matches_the_official_client():
    # Bytes seen on the wire from Raritan's client.
    assert packets.pack_csc("<CSC_Ack/>") == b"\x00\x00\x00\x0f<CSC_Ack/>\x00"
    start = packets.pack_csc('<CSC_Start_Session ProtocolID="IP-Reach"/>')
    assert start[:4] == b"\x00\x00\x00\x2f"
    assert start.endswith(b"/>\x00")


def test_csc_length_excludes_the_header():
    assert packets.csc_length(b"\x00\x00\x00\x0b") == 7
    with pytest.raises(packets.ProtocolError):
        packets.csc_length(b"\x00\x00\x00\x02")


def test_server_id_from_a_real_switch(capture):
    server = packets.ServerId.parse(capture["server_id"])
    assert server.name == "Dominion-KX"
    assert server.ip_address == "192.168.25.251"
    assert server.port == 5000
    assert server.protocol_version == 30
    assert server.oldest_protocol_version == 20
    assert server.encryption == packets.SECURITY_SSL_LOGIN_THEN_RC4
    assert server.uses_chap
    assert not server.wants_hid_keycodes


def test_server_id_must_be_74_bytes():
    with pytest.raises(packets.ProtocolError):
        packets.ServerId.parse(bytes(73))


def test_handshake_ack_round_trip(capture):
    ack = packets.HandshakeAck.parse(capture["handshake_ack_from_server"])
    assert ack.signature == packets.READ_SOCKET_SIGNATURE
    assert ack.checksum == ack.compute_checksum()
    assert ack.pack() == capture["handshake_ack_from_server"]


def test_handshake_acks_we_send_match_the_official_client(capture):
    ack = packets.HandshakeAck.parse(capture["handshake_ack_from_server"])

    read_ack = ack.with_signature(packets.READ_SOCKET_SIGNATURE).pack()
    write_ack = ack.with_signature(packets.WRITE_SOCKET_SIGNATURE).pack()

    assert read_ack == capture["handshake_ack_read_socket"]
    assert write_ack == capture["handshake_ack_write_socket"]


def test_with_signature_leaves_the_original_alone(capture):
    ack = packets.HandshakeAck.parse(capture["handshake_ack_from_server"])
    ack.with_signature(packets.WRITE_SOCKET_SIGNATURE)
    assert ack.signature == packets.READ_SOCKET_SIGNATURE


def test_handshake_ack_rejects_other_packets():
    with pytest.raises(packets.ProtocolError):
        packets.HandshakeAck.parse(b"\x00\x22\x2a\x00" + bytes(30))


def test_identify_remote_matches_the_official_client(capture):
    assert packets.identify_remote() == capture["identify_remote"]


def test_pack_command_header():
    assert packets.pack_command(packets.CMD_LOGOUT) == b"\x00\x04\x07\x00"
    assert packets.pack_command(17, b"\x00\x1c", packet_id=5) == b"\x00\x06\x11\x05\x00\x1c"
    assert packets.parse_command_header(b"\x00\x06\x11\x05") == (6, 17, 5)


def test_pack_command_refuses_oversized_payloads():
    with pytest.raises(ValueError):
        packets.pack_command(54, bytes(0x10000))
