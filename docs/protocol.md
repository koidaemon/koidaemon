# koid's protocol

koid listens on a Unix socket (by default `/run/koidaemon/koid.sock`) and,
if enabled, an HTTP API. Both take the same JSON requests and give the
same JSON replies.

## On the socket

One JSON object per line in each direction. A client can send several
requests on one connection, waiting for each reply before sending the
next. Requests are limited to 64 KiB.

A request names a command, the target, and the command's arguments:

```json
{"command": "keyboard.press", "target": "board-a", "args": {"keys": ["ctrl+alt+delete"]}}
```

The reply is either a result or an error:

```json
{"ok": true, "result": {}}
{"ok": false, "error": {"code": "unsupported", "message": "board-a (raritan-legacy) cannot do storage.insert"}}
```

Error codes:

| Code | Meaning |
|---|---|
| `invalid` | the request or its arguments are wrong |
| `unknown-target` | no target has that name |
| `unsupported` | the target's KVM cannot do this command |
| `busy` | the KVM is in use elsewhere; trying again later can work |
| `failed` | the KVM could not do it |

## Over HTTP

`POST /v1/command` takes a request as its body and returns the reply.
`GET /v1/targets` is the `targets` command. Every request needs the
configured token as `Authorization: Bearer TOKEN`; without it the reply is
401 with the code `unauthorised`.

The HTTP status follows the reply: 200 for a result, and for errors 400
(`invalid`), 404 (`unknown-target`), 422 (`unsupported`), 503 (`busy`) or
500 (`failed`).

## Commands

Commands for one KVM device run one at a time, in the order they arrive;
commands for different devices run in parallel. Several targets can share
a device (all ports of a Raritan switch do).

Arguments marked optional can be left out. Positions are screen pixels
from the top-left corner, and key names are as described in the README.

### targets

No target and no arguments. Returns the targets koid knows:

```json
{"targets": {"board-a": {"backend": "raritan-legacy", "capabilities": ["keyboard", "mouse", "snapshot"], "host": "192.0.2.10", "port": 1}}}
```

### status

Whether the target is reachable and powered, its video if the KVM knows
it, and its capabilities:

```json
{"online": true, "video": {"width": 1920, "height": 1080}, "capabilities": ["keyboard", "mouse", "snapshot", "storage.cdrom", "storage.stick"]}
```

`video` is null when there is no signal or the KVM cannot tell without
connecting. Backends may add fields of their own (raritan-legacy adds
`port_name`).

### snapshot

Needs `snapshot`.

| Argument | Type | |
|---|---|---|
| `format` | string | `png` (the default), `jpeg`, or `native` for whatever the KVM sends |

Returns the screen:

```json
{"format": "png", "width": 1920, "height": 1080, "data": "<base64>"}
```

koid converts the picture when the KVM sends another format (a PiKVM
sends JPEG), so clients get PNG whatever the KVM. Converting JPEG to PNG
keeps JPEG's losses; it only changes the file format. With `native`,
`format` says which the KVM sent, `png` or `jpeg`, and `width` and
`height` can be null if the picture does not say.

### keyboard.type

Needs `keyboard`.

| Argument | Type | |
|---|---|---|
| `text` | string | typed literally; a newline is Enter, a tab is Tab |

### keyboard.press

Needs `keyboard`.

| Argument | Type | |
|---|---|---|
| `keys` | list of strings | combinations such as `"f2"` or `"ctrl+alt+delete"`, pressed one after the other |

Each combination's keys are pressed in the order given and released in
reverse.

### keyboard.down, keyboard.up

Needs `keyboard`.

| Argument | Type | |
|---|---|---|
| `key` | string | one key |

### mouse.move

Needs `mouse`.

| Argument | Type | |
|---|---|---|
| `x`, `y` | whole numbers, 0 or more | where to move to |

### mouse.click

Needs `mouse`.

| Argument | Type | |
|---|---|---|
| `button` | `left`, `right` or `middle` | optional; default `left` |
| `count` | 1 to 10 | optional; default 1, 2 for a double click |
| `x`, `y` | whole numbers | optional, together: move here first |

### mouse.down, mouse.up

Needs `mouse`.

| Argument | Type | |
|---|---|---|
| `button` | `left`, `right` or `middle` | optional; default `left` |

### mouse.scroll

Needs `mouse`.

| Argument | Type | |
|---|---|---|
| `notches` | whole number, -100 to 100 | positive scrolls up, negative down |

### storage.insert

Needs `storage.stick`, or `storage.cdrom` for a CD.

| Argument | Type | |
|---|---|---|
| `image` | string | absolute path of the image on the machine running koid |
| `mode` | `stick` or `cdrom` | optional; default `stick` |
| `writable` | true or false | optional; default false; sticks only |

Replaces whatever was presented before. Returns the same as
`storage.status`.

### storage.eject

Needs `storage.stick` or `storage.cdrom`. Disconnects the drive; nothing
happens if nothing is presented.

### storage.status

Needs `storage.stick` or `storage.cdrom`:

```json
{"inserted": true, "image": "koid-0123456789ab-installer.iso", "mode": "cdrom", "writable": false}
```
