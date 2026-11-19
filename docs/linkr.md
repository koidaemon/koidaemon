# linkr

For the [Radxa Linkr](https://docs.radxa.com/en/linkr/linkr), through its
public API. Not yet tried on a real Linkr: what follows is from Radxa's
[developer documentation](https://docs.radxa.com/en/linkr/linkr/develop-guide).

| | |
|---|---|
| Keyboard | yes; text only as ASCII, tab and newline |
| Mouse | yes, absolute pixels |
| Snapshot | yes; the Linkr sends JPEG, which koid turns into PNG unless asked not to |
| USB storage | no; the public API cannot reach the Linkr's virtual media |

## Configuration

```toml
[targets.board-d]
backend = "linkr"
url = "https://192.0.2.30"
token_file = "/run/secrets/linkr"    # an access token
ca_file = "linkr.pem"                # optional; trust this certificate
verify_tls = true                    # optional; default true
timeout = 30                         # optional; seconds per request
```

The API takes an access token rather than the Linkr's password. Make one
in the Linkr's web interface, under System Settings, Access Token,
Generate; it starts with `radxa_linkr_` and is shown only once. Put it in
`token_file`. Deleting it in the same place stops koid at once.

As with a PiKVM, a Linkr's own certificate is best given to koid as
`ca_file`, which koid then trusts whatever name the Linkr is reached by.

## How commands map to the API

Every request carries `Authorization: token TOKEN`.

| Command | Linkr API |
|---|---|
| `status` | `POST /api/public/control` with no events, to check the Linkr answers and takes the token |
| `snapshot` | `GET /api/public/snapshot` |
| `keyboard.type` | `["text", ...]` events of at most 30 characters, with `["delay", 1000]` between them |
| `keyboard.press`, `.down`, `.up` | `["keyboard", code, pressed]`, with browser key codes |
| `mouse.*` | `["mouse_abs", buttons, x, y, wheel, 0]`, with x and y from 0 to 1, and a wheel step of at most 20 that scrolls down when positive (the opposite of koid's notches) |

Every event goes to `POST /api/public/control` as `{"events": [...]}`.
Radxa advise a pause of about a second after every 30 or so characters
typed, so typing long text is slow: about a second for every 30
characters.

The Linkr takes mouse positions as fractions of the screen, so koid
converts pixels with the size of the last snapshot, taking one first if it
has none. `status` reports that size, and no video until koid has taken a
snapshot. Each mouse event says which buttons are held and where the
pointer is, so koid keeps both.

## USB storage: how it could work

koid cannot present disk images on a Linkr yet. The public API has no
virtual media, but the Linkr's own web interface does: its Virtual Disk
page uploads, mounts, unmounts and deletes `.iso` and `.img` images, and
the target sees a mounted image as a USB drive. This is how that page
does it, read from the JavaScript a Linkr running firmware 1.6.0 serves
(none of it is documented by Radxa, and it may change between firmware
versions):

**Login.** The private API does not take the access token. It takes a
login with the Linkr's own user and password, as Radxa's harness-linkr
client also does:

- `POST /api/account/login` with `{"username": ..., "passwd": ...}`,
  where `passwd` is the lowercase hex SHA-256 of `RADXA_FIXED_SALT_v1`
  followed by the password;
- the answer's `data.token` is a JWT, sent as `Authorization: Bearer
  JWT` on later requests (`/api/account/refresh` renews it);
- answers are `{"code": 0, "message": ..., "data": ...}`, and HTTP 403
  means the token was refused.

**Managing images**, over HTTP:

| Request | Does |
|---|---|
| `GET /api/storage/list` | the images: `data.list`, and `data.mounted`, the one presented |
| `GET /api/storage/space` | free, used and total space on the Linkr |
| `POST /api/storage/mount` `{"filename": NAME}` | presents an image to the target |
| `POST /api/storage/unmount` | disconnects it (the page does this before every mount) |
| `DELETE /api/storage/file` `{"filename": NAME, "md5": MD5}` | deletes an image |

Mounting takes only a file name: the page offers no choice of CD or
stick, and no read-only setting.

**Uploading** goes over the WebSocket at `/ws`, as protobuf `WsMessage`
messages. Each carries the JWT in its `Token` field (2), a
`MessageType` (field 1) and one request:

1. `UploadFileStart` (type 8, request in field 20): upload type
   `VirtualDisk` (0), file name, the file's MD5 as hex, its size, and the
   number of 2 MiB chunks. The answer (field 21) has `NextChunkIndex`,
   where to start, so an interrupted upload can carry on.
2. `UploadFileChunk` (type 9, field 22): upload type, name, MD5, chunk
   index and up to 2 MiB of data. Each answer (field 23) gives the next
   chunk index wanted.
3. `UploadFileEnd` (type 10, field 24) once every chunk is sent. The
   Linkr then checks the MD5, which takes a while for large files.
4. `UploadFileCancel` (type 11, field 26) gives up.

An answer with `MessageCode` 1 and `Message` `no_space` means the Linkr
is full; `MessageCode` 403 means the token has expired.

To support it koid would need the Linkr's password as well as the
access token, a small protobuf encoder (the messages use only varints,
strings and bytes), and binary messages over its own WebSocket client.
