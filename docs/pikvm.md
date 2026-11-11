# pikvm

For [PiKVM](https://pikvm.org), and other devices running its kvmd daemon
and HTTP API. Tested with a PiKVM V4 Mini running kvmd 4.61, on a board
running Debian with a 1920x1080 console: every command, with USB storage
checked from the board as a stick and from the PiKVM as a CD.

| | |
|---|---|
| Keyboard | yes |
| Mouse | yes, absolute pixels |
| Snapshot | JPEG |
| USB storage | yes, as a stick or a CD drive |

## Configuration

```toml
[targets.board-b]
backend = "pikvm"
url = "https://192.0.2.20"
user = "admin"                       # optional; default admin
password_file = "/run/secrets/pikvm-b"
ca_file = "pikvm-b.pem"              # optional; trust this certificate
verify_tls = true                    # optional; default true
keymap = "en-us"                     # optional; kvmd's default otherwise
timeout = 30                         # optional; seconds per request
```

A PiKVM's certificate is self-signed. Give it to koid as `ca_file`: koid
then trusts that certificate whatever name the PiKVM is reached by.
`verify_tls = false` turns checking off altogether.

## How commands map to kvmd

Every request carries the `X-KVMD-User` and `X-KVMD-Passwd` headers.

| Command | kvmd API |
|---|---|
| `status` | `GET /api/streamer` |
| `snapshot` | `GET /api/streamer/snapshot` |
| `keyboard.type` | `POST /api/hid/print` (kvmd turns text into keys with `keymap`) |
| `keyboard.press` | `POST /api/hid/events/send_key`: each key down in order, then up in reverse |
| `keyboard.down`, `.up` | `POST /api/hid/events/send_key` with a state |
| `mouse.move` | `POST /api/hid/events/send_mouse_move` |
| `mouse.click`, `.down`, `.up` | `POST /api/hid/events/send_mouse_button` |
| `mouse.scroll` | `POST /api/hid/events/send_mouse_wheel`, at most 127 per request |
| `storage.*` | `/api/msd` and its `write`, `set_params` and `set_connected` |

kvmd's `send_shortcut` would press a whole combination in one request,
but older kvmd versions do not have it, so koid sends the keys one by one.

kvmd takes mouse positions scaled to -32768..32767 whatever the screen
size, so koid converts pixels using the video resolution.

## Video

kvmd only runs its video streamer while a client watches through its
WebSocket (`/api/ws`), such as an open web interface. While the streamer
is stopped, kvmd has no snapshot and no screen size to give. So for
`snapshot`, and for mouse commands with a position, koid connects to the
WebSocket itself, waits up to 10 seconds for the streamer to see a signal,
does its work, and disconnects. kvmd stops the streamer again a little
later.

With no signal from the target, such as when it is off, these commands
fail with "no video signal from the target" after those 10 seconds.
`status` reports video only while the streamer happens to be running,
since starting it just to look would make `status` slow.

To keep the streamer running all the time, at the cost of some CPU on the
PiKVM, set this in its `/etc/kvmd/override.yaml`:

```yaml
kvmd:
  streamer:
    forever: true
```

## USB storage

`storage.insert` uploads the image to the PiKVM's storage, then presents
it:

1. disconnects the drive if something is connected;
2. uploads the image, named `koid-<first 12 hex digits of its SHA-256>-<file name>`,
   unless an image with that name is already there, and waits for kvmd to
   list it (kvmd finds new images by watching its storage, so cannot use
   one for a moment after it is uploaded);
3. sets it as a stick or CD, read-only or writable;
4. connects the drive, and checks that kvmd did what was asked.

kvmd quietly ignores settings it cannot honour, such as presenting an
image too big for a CD as one; koid checks the result and reports a
failure rather than presenting something else. (koid itself refuses a
writable CD before asking kvmd.)

A CD image must be at least 600 KiB (300 sectors): Linux's USB gadget
refuses anything smaller, so koid refuses it first.

**The target sees the drive as whichever kind it was when the target
found the PiKVM's USB device**, normally at boot. Switching between stick
and CD is not noticed until the target finds the device again: insert the
image before powering the target on, or make the target re-enumerate it
(on Linux, write 0 and then 1 to the device's `authorized` file in
`/sys/bus/usb/devices/`). A target also needs a driver for the kind of
drive: on Linux, `CONFIG_BLK_DEV_SD` for a stick and `CONFIG_BLK_DEV_SR`
for a CD.

Uploaded images stay on the PiKVM, so inserting the same image again is
quick. koid does not delete old ones; remove them in the PiKVM's web
interface when its storage fills up.
