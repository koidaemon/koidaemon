# raritan-legacy

For the first generation of Raritan **Dominion KX** switches: KX101,
KX116, KX132, KX216, KX232, KX464 and similar. They only ever shipped a
Java client; this backend is an independent Python implementation of the
same protocol. The Dominion KX II and later speak a different protocol and
are not supported.

Tested with a Dominion KX running firmware 1.4.9.5.16, a DCIM-USBG2 on the
target port, and Linux on the target.

| | |
|---|---|
| Keyboard | yes (PS/2 scan codes, or HID usages if the switch asks for them) |
| Mouse | yes, absolute pixels |
| Snapshot | yes, as PNG; tested at 640x480 and 800x600 (see [Video](#video)) |
| USB storage | no: these switches cannot emulate it |

## Configuration

```toml
[targets.board-a]
backend = "raritan-legacy"
host = "192.0.2.10"          # the switch
port = 1                     # numbered from 1, as on the switch
user = "admin"               # optional; default admin
password_file = "/run/secrets/raritan"
wait = 30                    # optional; default 30
key_delay = 0.03             # optional; default 0.03
tcp_port = 5000              # optional; default 5000
```

`wait` is how many seconds a command keeps trying while the switch is
busy with someone else (see below) before failing with `busy`. `key_delay`
is the time between key events.

## How it works

A target is one port of a switch. Each command logs in, connects to the
port, does its job and logs out, which takes one to three seconds.

Every port of a switch is one resource in koid, so koid never runs two
commands on the same switch at once. People or other programs using the
switch at the same time are handled by `wait`.

`keyboard.down` and `keyboard.up` each run in a session of their own;
whether the switch keeps a key held between sessions has not been tested.

## Video

The switch digitises VGA. Raritan's user guide lists the modes it
supports, none of them widescreen, up to 1280x1024:

| Resolution | Refresh rates (Hz) |
|---|---|
| 640x480 | 60, 72, 75, 85 |
| 720x400 | 70, 85 |
| 800x600 | 56, 60, 72, 75, 85 |
| 1024x768 | 60, 70, 75, 77, 85 |
| 1152x864 | 60, 70, 75 |
| 1152x900 | 66 |
| 1280x960, 1280x1024 | 60 |

Found with a board whose HDMI output went through an HDMI-to-VGA adapter:

- **640x480 and 800x600 worked.** The switch could not lock onto the
  adapter's 1024x768 at 60 Hz, although that mode is on the list. On Linux,
  `video=HDMI-A-1:800x600@60` on the kernel command line picks a mode.
- **The switch needs something on the screen to lock onto.** A screen that
  is almost entirely black, such as an idle text console, or a smooth
  gradient never locked; text or a test pattern locked within seconds.
  Raritan recommends a "predominantly solid, plain, light-colored"
  desktop background.
- **The switch can lose sync and start again.** When it does, it first
  sends a whole black screen. A snapshot is therefore the first complete
  frame after the switch locks, once a second has passed without it losing
  sync, which skips the black or half-drawn frames sent at that moment.
- **Brightness varies.** The switch adjusts its own gain: the same grey
  came back at different levels on different connections.

`snapshot` fails with "the port has no video signal" or "no complete
frame" when the switch cannot lock.

## Behaviour of the hardware

Found while testing against a real switch; the code works around each one.

- **The switch drops key presses for about a second after a port is
  connected**, while it detects the video mode, so commands wait for its
  "autosense finished" notification first.
- **Typing too fast loses keys.** 10 ms between key events drops
  characters; 20 ms was reliable over long texts; the default is 30 ms.
- **The last key pressed is lost if the session closes at once**, so
  closing waits half a second after the last input.
- **Mouse events can be held back** until the next mouse event arrives. A
  missing button release leaves the button held down, so every mouse
  action ends with an empty wheel event to push it through.
- **The wheel is inverted**, and the first wheel event of a session can be
  lost. Both are handled; positive still scrolls up.
- **Ctrl+NumLock opens the CIM's own setup menu.** The CIM then "types"
  its menu into the target and swallows keys until Escape, so koid refuses
  that combination.
- **Only one port can be connected at a time.** The switch has a single
  video channel, and connecting to a port always uses it, even if only the
  keyboard and mouse are wanted. While another session is connected to a
  different port, connecting is refused until it logs out; koid keeps
  trying for `wait` seconds.
- **A port can be shared.** A second session can connect to a port that
  is already in use. The switch never tells it that video detection has
  finished, so it waits 10 s first. Whether input from both sessions
  reaches the target has not been tested.
- **Up to 8 sessions can be logged in at once**; a 9th login is refused,
  and also retried for `wait` seconds.
- **The CIM is powered by the target**, so a port shows no target
  (`"online": false`) while the machine is off.
- **The CIM appears to keep a name of its own** for its port: a name set
  on the switch was replaced by the old one once the target powered up.
- **Mouse positions are absolute screen pixels.** On connect the switch
  drives the pointer into the top-left corner, then replays the last
  position it set.

## Security

The protocol is from the early 2000s:

- SSL 3.0 with RC4 during login, then plain RC4 for the rest of the
  session. Both are broken, so treat the password and everything typed as
  readable by anyone who can capture the traffic.
- The switch's certificate is self-signed and expired, and is not checked
  (Raritan's own client does not check it either), so nothing stops
  another machine on the network path from pretending to be the switch.

Give koid a switch account of its own, with access to only the ports it
needs, and keep the traffic off untrusted networks where you can.

## The code

| Module | Covers |
|---|---|
| `packets.py` | the plain-text CSC greeting, the 74-byte server id, the handshake acknowledgement, command framing |
| `ssl3.py`, `rc4.py` | the in-band upgrade to SSL 3.0 (`RSA_WITH_RC4_128_MD5` only) |
| `session.py` | the two-socket layout, port listing, connecting and renaming, the background reader and keepalive |
| `auth.py` | the challenge-response, the RC4 session key, and the RADIUS-style access request (CHAP or PAP) |
| `keyboard.py`, `scancodes.py` | key events as PS/2 scan code set 2 |
| `mouse.py` | mouse events |
| `screen.py`, `lzo.py` | video mode packets and LZO1X-compressed screen cells |

Much of it is tested against values recorded from Raritan's own client
logging in to a real switch (`tests/raritan_legacy/data/`).
