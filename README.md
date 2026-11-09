# koidaemon

koidaemon drives the keyboard, mouse, screen and USB storage of machines
behind KVM-over-IP devices, with the same commands whatever the device.
It was written for automated testing of boards, but works for anything
that needs to type into a machine it cannot otherwise reach.

- **`koid`** is the daemon. It owns the connections to the KVMs, keeps the
  passwords, and runs commands one at a time per device.
- **`koictl`** is the command line client; `koidaemon.client` is the same
  for Python.

The name is short for **K**VM **o**ver **I**P.

## Backends

| Backend | Devices | Keyboard | Mouse | Snapshot | USB stick | USB CD |
|---|---|---|---|---|---|---|
| [`pikvm`](docs/pikvm.md) | PiKVM, and devices running its kvmd | yes | yes | JPEG | yes | yes |
| [`raritan-legacy`](docs/raritan-legacy.md) | first-generation Raritan Dominion KX | yes | yes | PNG | no | no |

What has been tested on real hardware so far:

- **raritan-legacy:** keyboard and mouse, on a Dominion KX with firmware
  1.4.9.5.16 and a DCIM-USBG2; snapshots of a Linux text console and of
  test patterns at 640x480 and 800x600. These switches handle no
  widescreen modes; see [docs/raritan-legacy.md](docs/raritan-legacy.md)
  for the resolutions that work.
- **pikvm:** written from kvmd's source and tested against a fake kvmd;
  not yet run against a PiKVM.

## Install

Needs Python 3.11 or later.

```sh
python3 -m venv .venv
.venv/bin/pip install .
```

The only dependency is `cryptography`, for the raritan-legacy backend's
SSL 3.0 handshake.

## Configure

koid reads `/etc/koidaemon/koid.toml` (or the file given with `-c`). Each
`[targets.NAME]` table is one machine:

```toml
[daemon]
socket = "/run/koidaemon/koid.sock"    # default
socket_mode = "0660"                   # default
socket_group = "lava"                  # optional: who may use koid

[targets.board-a]
backend = "raritan-legacy"
host = "192.0.2.10"
port = 1
password_file = "/run/secrets/raritan"

[targets.board-b]
backend = "pikvm"
url = "https://192.0.2.20"
password_file = "/run/secrets/pikvm-b"
ca_file = "pikvm-b.pem"
```

Passwords are only ever read from files, never written in the
configuration. Relative paths are relative to the configuration file. Each
backend's settings are described in its page under [docs](docs/), and
[examples/koid.toml](examples/koid.toml) is a starting point.

`koid --check` reads the configuration and lists the targets without
starting.

## Run

```sh
koid -c /etc/koidaemon/koid.toml
```

koid logs to standard error and stops cleanly on SIGTERM or Ctrl-C,
removing its socket. Anyone who can open the socket can use every target,
so `socket_mode` and `socket_group` decide who that is.

## Use

```sh
export KOI_TARGET=board-a

koictl targets
koictl status
koictl keyboard type --enter root
koictl keyboard press f2
koictl keyboard press ctrl+alt+delete
koictl mouse click --at 640 400 --count 2
koictl mouse scroll -3
koictl snapshot screen.png
koictl storage insert --cdrom installer.iso
koictl storage eject
```

`-t NAME` picks the target instead of `KOI_TARGET`; `--socket` (or
`KOID_SOCKET`) points at a socket other than the default. Results such as
`status` are printed as JSON.

Exit status: 0 on success, 1 if the command failed, 2 for bad usage, 3 if
the target's KVM cannot do what was asked.

Keys can be named in two ways: short names (`enter`, `esc`, `f2`, `ctrl`,
`shift`, `alt`, `super`, `a`, `1`, `-`, ..., with `r` in front for the
right-hand modifiers) or browser key codes (`KeyA`, `ControlLeft`,
`NumpadEnter`). Join keys with `+` for a combination. Text is typed with a
US keyboard layout. Mouse positions are screen pixels from the top-left
corner.

### Commands

The same list for every backend; [docs/protocol.md](docs/protocol.md) has
the details.

| Command | Does |
|---|---|
| `status` | whether the target is reachable, its video, and what it can do |
| `snapshot` | a picture of the screen |
| `keyboard.type` | type text |
| `keyboard.press` | press and release keys or combinations, in order |
| `keyboard.down`, `keyboard.up` | hold and release a key |
| `mouse.move` | move the pointer |
| `mouse.click` | click a button, optionally after moving |
| `mouse.down`, `mouse.up` | hold and release a button |
| `mouse.scroll` | turn the wheel; positive scrolls up |
| `storage.insert` | present a disk image as a USB stick or CD drive |
| `storage.eject` | disconnect it |
| `storage.status` | what is being presented |

A command the target's KVM cannot do is refused as unsupported before the
KVM is touched. `storage.insert` takes a path on the machine running koid,
since koid reads the image; koictl turns relative paths into absolute
ones.

### From Python

```python
from koidaemon.client import Client

koi = Client()
koi.call("keyboard.type", "board-a", text="root\n")
koi.call("keyboard.press", "board-a", keys=["ctrl+alt+delete"])
snapshot = koi.snapshot("board-a")
```

## HTTP API

For clients that cannot reach the socket, koid can also serve the same
requests over HTTP. It is off unless enabled, because it puts the targets'
keyboards and USB drives on the network:

```toml
[http]
enabled = true
listen = "127.0.0.1:8470"            # default
token_file = "/etc/koidaemon/token"  # required
cert_file = "koid.pem"               # optional: HTTPS
key_file = "koid.key"
```

Every request needs the token as `Authorization: Bearer TOKEN`. koid will
not start the API without a token, and warns when it listens beyond
localhost without TLS. `koictl --url URL --token-file FILE` (or
`KOID_URL` and `KOID_TOKEN_FILE`) uses it; `--ca-file` trusts a
certificate for HTTPS.

## Security

- The Unix socket gives full control of every target to whoever can open
  it.
- The raritan-legacy protocol uses SSL 3.0 and RC4, both broken: treat the
  switch's password and everything typed as readable by anyone who can
  capture the traffic. See [docs/raritan-legacy.md](docs/raritan-legacy.md).
- A PiKVM's certificate is self-signed; give it to koid as `ca_file`
  rather than turning verification off.

## Tests

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

No KVM is needed. The tests run the daemon, the HTTP API and koictl for
real, against a recording backend; the pikvm backend against a fake kvmd;
and the raritan-legacy backend against a fake switch, with protocol values
recorded from Raritan's own client talking to a real one.

## License

MIT; see [LICENSE](LICENSE).
