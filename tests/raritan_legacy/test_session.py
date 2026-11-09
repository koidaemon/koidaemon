# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""End-to-end tests of KXSession against the fake switch in fake_kx.py."""

import pytest

from koidaemon.backends.raritan_legacy import auth, packets
from koidaemon.backends.raritan_legacy import session as kx
from koidaemon.backends.raritan_legacy.keyboard import Keyboard
from koidaemon.backends.raritan_legacy.mouse import Mouse

from fake_kx import FakeKX


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    # The real delays exist for the hardware; the fake does not need them.
    monkeypatch.setattr(kx, "ENCRYPTION_SWITCH_DELAY", 0.05)
    monkeypatch.setattr(kx, "INPUT_DRAIN_TIME", 0.0)
    monkeypatch.setattr(kx, "BUSY_RETRY_INTERVAL", 0.05)


def open_session(fake, password="secret", wait=0.0):
    return kx.KXSession("127.0.0.1", "admin", password, tcp_port=fake.port, wait=wait)


def check(fake):
    """Fail the test with the fake switch's own error, if it had one."""
    assert fake.logged_out.wait(10), "the session never logged out"
    if fake.error is not None:
        raise fake.error


@pytest.mark.parametrize("security_flags", [
    0x1401,  # SSL for login, then RC4, CHAP (what a stock switch uses)
    0x0401,  # the same with PAP
    0x0201,  # SSL for login, then plain
    0x0801,  # SSL all the time
])
def test_login_and_type(security_flags):
    with FakeKX(security_flags=security_flags) as fake:
        with open_session(fake) as session:
            assert session.server.name == "Fake-KX"
            session.connect_port(1)
            Keyboard(session, delay=0).type_text("hi")
        check(fake)

    assert fake.login_user == "admin"
    key_bytes = [packet[5:] for packet in fake.keyboard_packets]
    assert key_bytes == [bytes.fromhex(code) for code in ("33", "f033", "43", "f043")]
    # Every keyboard packet goes to the device id the switch assigned.
    assert {packet[4] for packet in fake.keyboard_packets} == {7}


def test_wrong_password():
    with FakeKX() as fake:
        with pytest.raises(auth.LoginError, match="wrong user name or password"):
            open_session(fake, password="nope").open()


def test_list_ports():
    with FakeKX() as fake:
        with open_session(fake) as session:
            ports = session.list_ports()
        check(fake)

    assert [(port.number, port.name, port.active) for port in ports] == [
        (1, "Board A", True),
        (2, "Board B", False),
    ]
    assert ports[0].target_id == "P_TEST_0"
    assert ports[0].portal_id == "AA_FG0"


def test_connect_port_sends_portal_and_target():
    with FakeKX() as fake:
        with open_session(fake) as session:
            assert session.connect_port(2, force=True) == 7
        check(fake)

    assert fake.connect_xml == ('<Connect ForceConnection="1"><Portal>//*[@id=AA_FG0]</Portal>'
                                '<Target>//*[@id=P_TEST_1]</Target></Connect>')


def test_missing_port():
    with FakeKX() as fake:
        with open_session(fake) as session:
            with pytest.raises(kx.SessionError, match="port 9 does not exist"):
                session.connect_port(9)
        check(fake)


def test_keys_need_a_connected_port():
    with FakeKX() as fake:
        with open_session(fake) as session:
            with pytest.raises(kx.SessionError, match="connect to a port first"):
                session.send_key_bytes(b"\x1c")
        check(fake)


def test_mouse_packets():
    with FakeKX() as fake:
        with open_session(fake) as session:
            session.connect_port(1)
            Mouse(session, delay=0).move_to(300, 200)
        check(fake)

    move, flush = fake.mouse_packets
    assert move == packets.pack_command(packets.CMD_MOUSE, bytes.fromhex("0706012c00c8"))
    assert flush == packets.pack_command(packets.CMD_MOUSE, bytes.fromhex("070800000000"))


def test_rename_port():
    with FakeKX() as fake:
        with open_session(fake) as session:
            session.rename_port(2, "rb3 board")
        check(fake)

    assert fake.port_names == ["Board A", "rb3 board"]
    # The port's mouse scale factors are sent back unchanged.
    assert fake.updates == [
        '<Database><Update><Select>/System/Device/Port[@id="P_TEST_1"]</Select>'
        '<Data><Name>rb3 board</Name>'
        '<XMouseScaleFactor>1.5</XMouseScaleFactor>'
        '<YMouseScaleFactor>2.0</YMouseScaleFactor></Data></Update></Database>'
    ]


@pytest.mark.parametrize("reply, message", [
    ("<Database><Error>5</Error></Database>", r"refused to rename port 1 \(error 5\)"),
    ("<Database><Update><Error>7</Error></Update></Database>", r"\(error 7\)"),
    ("<Database><Update>0</Update></Database>", r"changed 0 entries"),
])
def test_rename_port_failures(reply, message):
    with FakeKX() as fake:
        fake.update_reply = reply
        with open_session(fake) as session:
            with pytest.raises(kx.SessionError, match=message):
                session.rename_port(1, "new name")
        check(fake)


def test_rename_missing_port():
    with FakeKX() as fake:
        with open_session(fake) as session:
            with pytest.raises(kx.SessionError, match="port 3 does not exist"):
                session.rename_port(3, "new name")
        check(fake)
    assert fake.updates == []


@pytest.mark.parametrize("name, message", [
    ("", "cannot be empty"),
    ("x" * 21, "at most 20 characters"),
    ("a<b>", "not '<' '>'"),
    ("caf\u00e9", "not '\u00e9'"),
])
def test_bad_port_names(name, message):
    with pytest.raises(ValueError, match=message):
        kx.check_port_name(name)


def test_good_port_names():
    for name in ("Channel 1", "x" * 20, "rb3/usb+hdmi-1.0"):
        kx.check_port_name(name)


def test_connect_waits_while_the_video_channel_is_busy():
    with FakeKX() as fake:
        fake.busy_connects = 2
        with open_session(fake, wait=5) as session:
            assert session.connect_port(1) == 7
        check(fake)
    assert fake.connect_attempts == 3


def test_busy_video_channel_fails_at_once_without_a_wait():
    with FakeKX() as fake:
        fake.busy_connects = 1
        with open_session(fake) as session:
            with pytest.raises(kx.SwitchBusyError, match="video channel is in use"):
                session.connect_port(1)
        check(fake)
    assert fake.connect_attempts == 1


def test_connect_gives_up_when_the_wait_runs_out():
    with FakeKX() as fake:
        fake.busy_connects = 1000
        with open_session(fake, wait=0.3) as session:
            with pytest.raises(kx.SwitchBusyError, match="only connect one port at a time"):
                session.connect_port(1)
        check(fake)
    assert 2 <= fake.connect_attempts < 1000


def test_login_waits_while_too_many_sessions_are_logged_in():
    with FakeKX() as fake:
        fake.busy_logins = 2
        with open_session(fake, wait=5) as session:
            session.connect_port(1)
        check(fake)
    assert fake.login_attempts == 3
    assert fake.login_user == "admin"


def test_too_many_sessions_fails_at_once_without_a_wait():
    with FakeKX() as fake:
        fake.busy_logins = 1
        with pytest.raises(kx.SwitchBusyError, match="too many sessions"):
            open_session(fake).open()
        # Log in properly so the fake switch can finish.
        with open_session(fake):
            pass
        check(fake)
    assert fake.login_attempts == 2


def test_notifications_reach_the_video_listener():
    commands_seen = []
    with FakeKX() as fake:
        with open_session(fake) as session:
            session.video_listener = lambda packet: commands_seen.append(packet[2])
            session.connect_port(1)
        check(fake)
    # The fake switch reports its autosense after connecting.
    assert packets.RSP_NOTIFY in commands_seen
