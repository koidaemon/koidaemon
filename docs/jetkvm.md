# jetkvm

For [JetKVM](https://jetkvm.com). koid talks to the JetKVM itself, over WebRTC, as the JetKVM's web interface does. Tested with firmware 0.5.10 on a board with a 1920x1080 Linux console: `status`, `snapshot` and `storage.status`. Keyboard, mouse and storage commands are tested against a fake JetKVM that follows the 0.5.10 firmware source.

| | |
|---|---|
| Keyboard | yes |
| Mouse | yes, absolute pixels |
| Snapshot | yes; the JetKVM sends H.264 video, which koid decodes and hands out as PNG |
| USB storage | yes, as a stick or a CD drive, read-only |

## Install

WebRTC needs [aiortc](https://github.com/aiortc/aiortc) and [PyAV](https://github.com/PyAV-Org/PyAV), which are big, so koidaemon only installs them when asked:

```sh
.venv/bin/pip install '.[jetkvm]'
```

Without them, koid warns as it starts, and every command on a jetkvm target fails.

## Configuration

```toml
[targets.board-c]
backend = "jetkvm"
url = "http://192.0.2.30"
password_file = "/run/secrets/jetkvm-c"   # optional; leave out in noPassword mode
ca_file = "jetkvm-c.pem"                  # optional; trust this certificate
verify_tls = true                         # optional; default true
timeout = 30                              # optional; seconds to connect, and per request
idle_timeout = 60                         # optional; seconds koid keeps an unused session
```

A JetKVM has one login token: koid logging in logs out a browser, and the other way round.

With HTTPS turned on in the JetKVM's settings, give its certificate as `ca_file`, or set `verify_tls = false`.

## Sessions

A JetKVM serves one WebRTC session at a time. When koid connects, it takes over from the web interface; opening the web interface takes over from koid, which connects again for its next command.

koid connects for the first command, which takes about a second, and keeps the session until it has been unused for `idle_timeout` seconds. It keeps it longer while it holds keys or mouse buttons down, because the JetKVM releases the keys when a session ends.

Only a session for a `snapshot` carries video, because decoding 1080p video takes about a quarter of a core. koid opens one when a snapshot needs it, keeps it for 5 seconds, and goes back to a session without video for the other commands, holding keys and buttons again when it does.

## How commands map to the JetKVM

The session carries two data channels (and video, for a snapshot): `rpc`, with JSON-RPC 2.0, and `hidrpc`, with binary keyboard and mouse reports.

| Command | JetKVM |
|---|---|
| `status` | `getVideoState` |
| `snapshot` | the next decoded frame of the video |
| `keyboard.*` | keyboard macros on `hidrpc` |
| `mouse.*` | absolute pointer reports on `hidrpc`; the wheel is `wheelReport` on `rpc`, as firmware 0.5.10 has no wheel message on `hidrpc` |
| `storage.*` | `listStorageFiles`, `startStorageFileUpload` and an HTTP POST to `/storage/upload`, `mountWithStorage`, `unmountImage`, `getVirtualMediaState` |

The JetKVM runs a keyboard macro itself, step by step, and says when it has finished. So koid sends `keyboard.type` as one macro (or one per 512 characters) instead of a request per key, and the command returns once the text is typed. Each key is held for 20 ms and released for 20 ms, as when the web interface pastes text: 40 ms a character, whatever the network. `keyboard.type` types the text as given: keys held with `keyboard.down` are let go while it types and pressed again after. Macros send whole keyboard reports, which the JetKVM does not release on its own, so `keyboard.down` holds a key until `keyboard.up`.

Mouse positions are scaled to 0..32767 using the video size. Every pointer report says where the pointer is, so a click without a position clicks where koid last put the pointer, or the top-left corner before koid has moved it. The JetKVM does not answer pointer reports: mouse commands return once koid has sent them.

With no signal from the target, `snapshot` fails with "no video signal from the target (no_signal)", and so do mouse commands with a position until koid has seen the screen's size.

## USB storage

`storage.insert` uploads the image to the JetKVM's storage, then presents it:

1. lists the JetKVM's images, and uploads this one if it is not there, named `koid-<first 12 hex digits of its SHA-256>-<file name>`. The upload streams the file, or a pushed image (`koictl storage insert --push-file`) straight from the client, over HTTP. If a pushed image does not match its SHA-256, or the upload stops, koid deletes what was uploaded;
2. ejects whatever is presented, since the JetKVM will not mount over it;
3. mounts the image as a stick or CD, and checks that the JetKVM did.

The JetKVM presents images read-only, so a writable stick is refused. A CD image must be at least 600 KiB, as Linux's USB gadget refuses anything smaller.

Changing between stick and CD makes the JetKVM rebind its USB device, so the target sees the keyboard and mouse unplugged and plugged in again too.

Uploaded images stay on the JetKVM, so inserting the same image again is quick. koid does not delete old ones; remove them in the JetKVM's web interface when its storage fills up.
