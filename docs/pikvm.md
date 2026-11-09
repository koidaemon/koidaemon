# pikvm

For [PiKVM](https://pikvm.org), and other devices running its kvmd daemon
and HTTP API. Written from kvmd's source and tested against a fake kvmd;
**not yet run against a real PiKVM**.

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
| `keyboard.press` | `POST /api/hid/events/send_shortcut`, once per combination |
| `keyboard.down`, `.up` | `POST /api/hid/events/send_key` with a state |
| `mouse.move` | `POST /api/hid/events/send_mouse_move` |
| `mouse.click`, `.down`, `.up` | `POST /api/hid/events/send_mouse_button` |
| `mouse.scroll` | `POST /api/hid/events/send_mouse_wheel`, at most 127 per request |
| `storage.*` | `/api/msd` and its `write`, `set_params` and `set_connected` |

kvmd takes mouse positions scaled to -32768..32767 whatever the screen
size, so koid converts pixels using the video resolution. kvmd only runs
its video streamer while someone watches; if it is stopped, koid takes a
snapshot to start it and reads the size from that.

## USB storage

`storage.insert` uploads the image to the PiKVM's storage, then presents
it:

1. disconnects the drive if something is connected;
2. uploads the image, named `koid-<first 12 hex digits of its SHA-256>-<file name>`,
   unless an image with that name is already there;
3. sets it as a stick or CD, read-only or writable;
4. connects the drive, and checks that kvmd did what was asked.

kvmd quietly ignores settings it cannot honour, such as presenting an
image too big for a CD as one; koid checks the result and reports a
failure rather than presenting something else. (koid itself refuses a
writable CD before asking kvmd.)

Uploaded images stay on the PiKVM, so inserting the same image again is
quick. koid does not delete old ones; remove them in the PiKVM's web
interface when its storage fills up.
