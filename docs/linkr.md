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
