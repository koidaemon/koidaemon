# linkr

For the [Radxa Linkr](https://docs.radxa.com/en/linkr/linkr). The
keyboard, mouse and screen go through its public API, and disk images
through the API its own web interface uses.

USB storage has been tried with a Linkr running firmware 1.6.0, reached
over USB-C, with a Linux laptop as the target. The keyboard, mouse and
snapshots have not been tried on a real Linkr yet: they follow Radxa's
[developer documentation](https://docs.radxa.com/en/linkr/linkr/develop-guide).

| | |
|---|---|
| Keyboard | yes; text only as ASCII, tab and newline |
| Mouse | yes, absolute pixels |
| Snapshot | yes; the Linkr sends JPEG, which koid turns into PNG unless asked not to |
| USB storage | yes, as a USB stick the target can write to, with the Linkr's password |

## Configuration

```toml
[targets.board-d]
backend = "linkr"
url = "https://192.0.2.30"
token_file = "/run/secrets/linkr"    # an access token
user = "admin"                       # optional; for USB storage
password_file = "/run/secrets/linkr-password"   # optional; ditto
ca_file = "linkr.pem"                # optional; trust this certificate
verify_tls = true                    # optional; default true
timeout = 30                         # optional; seconds per request
```

The API takes an access token rather than the Linkr's password. Make one
in the Linkr's web interface, under System Settings, Access Token,
Generate; it starts with `radxa_linkr_` and is shown only once. Put it in
`token_file`. Deleting it in the same place stops koid at once.

USB storage needs more than the access token: the Linkr's own user and
password, the ones you log in to its web interface with. Without
`password_file` the target has no USB storage.

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

## USB storage

`storage.insert` uploads the image to the Linkr, then presents it:

1. logs in with `user` and `password_file`;
2. uploads the image, named `koid-<first 12 hex digits of its SHA-256>-<file name>`,
   unless an image of that name and size is already there;
3. unmounts whatever else is mounted, and mounts it.

The Linkr can only present an image as a USB stick, and the target can
always write to it, so koid refuses `--cdrom`, and refuses an insert
without `--writable` rather than present a read-only image the target
could write to. Writes reach the image stored on the Linkr and stay
there, so inserting the same image again gives the target what it wrote
last time; to start from a clean copy, delete the image in the Linkr's
web interface first.

The Linkr wants an image's MD5 before any of it is sent, so koid reads a
file twice, once for the MD5 and once to send it. An image a client
pushes (`koictl storage insert --push-file`) is kept in a temporary file
on the machine running koid while it is sent, so that needs room for it.

Uploaded images stay on the Linkr, and koid does not delete old ones;
remove them in the Linkr's web interface when it fills up.

Mounting and unmounting re-plug the Linkr's whole USB device, keyboard
and mouse included, so the target sees them disconnect for a couple of
seconds, and anything held with `keyboard.down` or `mouse.down` is let
go. koid does not mount again an image that is already mounted.

## The Linkr's private API

The public API has no virtual media, but the Linkr's own web interface
does: its Virtual Disk page uploads, mounts, unmounts and deletes `.iso`
and `.img` images. koid does what that page does, read from the
JavaScript a Linkr running firmware 1.6.0 serves. None of it is
documented by Radxa, and it may change between firmware versions.

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
| `GET /api/storage/list` | `{"list": [{"name", "md5", "size", "mod_time"}], "mounted": NAME or ""}` |
| `GET /api/storage/space` | `{"total", "used", "free"}` in bytes (13.5 GB on a 1.6.0 Linkr) |
| `GET /api/virtual/device` | `{"ncm", "uac", "ums"}`: which USB gadget functions are on |
| `POST /api/storage/mount` `{"filename": NAME}` | presents an image to the target |
| `POST /api/storage/unmount` | disconnects it (the page does this before every mount) |
| `DELETE /api/storage/file` `{"filename": NAME, "md5": MD5}` | deletes an image |

Mounting takes only a file name: the page offers no choice of CD or
stick, and no read-only setting. Each of these answers `{"code": 0,
"data": {}}` on success. The list's `md5` is empty even for an image
uploaded with one, so it cannot say whether an image is already there.

**How the target sees it**, tried with a Linux laptop as the target:

- The Linkr is one USB 2.0 device, "Radxa Linkr USB Gadget" (`1d6b:0104`),
  with three HID interfaces for the keyboard and mouse and, while an
  image is mounted, a mass storage interface.
- A mounted image is a removable disk ("Linkr Virtual Media", SCSI type
  0), never a CD, and the target can write to it: writes reach the image
  stored on the Linkr and are still there after unmounting and mounting
  again. There is no read-only setting.
- Mounting and unmounting re-plug the whole gadget, keyboard and mouse
  included: the target sees the Linkr disconnect and come back about two
  seconds later, so anything held down is released.
- A mount usually reached the target within a few seconds, and the
  mount request itself took about four. Once, a mount sent less than a
  second after an unmount took about 90 seconds to show up on the
  target.
- `ums` in `/api/virtual/device` stayed false throughout, image mounted
  or not, so it says nothing about the virtual disk.
- The Linkr's clock went back to 2021 when it restarted, so `mod_time`
  in the list cannot be trusted.

**Uploading** goes over the WebSocket at `/ws`, as protobuf `WsMessage`
messages. Each carries the JWT in its `Token` field (2), a
`MessageType` (field 1) and one request:

Every answer has `Message` (field 4) `success` when it worked.

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

A 4 MiB upload, two chunks, went through without koid sending the web
page's periodic ping messages.
