# Still to test on hardware

What has been tried on real devices is in the README and each backend's
page under [docs](docs/). This is what has not been tried yet.

## nanokvm-usb

Tested so far: status, snapshots, and the keyboard (keys, typing,
combinations) on a NanoKVM-USB in front of a Radxa Dragon Q8B, in its
UEFI menus and shell.

- [ ] `mouse.move`: does a pixel position land where it should on the
      snapshot? (The device is told 0..4095 scaled by the snapshot size.)
- [ ] `mouse.click`, including `--count 2`, with each button
- [ ] `mouse.down` / `mouse.up`: a drag
- [ ] `mouse.scroll`: does a positive number scroll up?
- [ ] All of the above need a target running an OS with a pointer; UEFI
      on the Q8B has none.
- [ ] `keyboard.down` held across other commands, e.g. shift held while
      clicking
- [ ] Capture sizes other than 1920x1080 (`width`/`height`), and a target
      running at a different resolution from the capture size
- [ ] Whether the 50 ms `key_interval` is needed once Linux is running,
      or only in UEFI

## linkr

Nothing tried yet: the Linkr did not come up over USB-C (the host saw a
"Realtek Linkr USB LAN" adapter but received no packets and got no DHCP
lease). Before testing, it needs first-time setup in linkr.now and an
access token (System Settings, Access Token).

- [ ] Reaching the Linkr at all, over USB-C or Ethernet, and over HTTPS
      with its certificate as `ca_file`
- [ ] The access token: accepted, and a wrong one refused with 403
- [ ] `status`: does an empty list of events to `/api/public/control`
      work as a cheap check, or does the Linkr refuse it?
- [ ] `snapshot`: JPEG, and the size koid reads from it
- [ ] `keyboard.type`: text, Enter and tab; long text over several
      requests arrives in order (does a request wait for its events to
      finish, or return at once?)
- [ ] `keyboard.press`, `.down`, `.up` with browser key codes, including
      ctrl+alt+delete
- [ ] `mouse.move` and clicks land where they should
- [ ] `mouse.scroll`: does a positive `wheelV` scroll down, as Radxa's
      own client says?
- [ ] USB storage (virtual media). The public API has none, but the
      device's web interface can upload, mount, unmount and delete .iso
      and .img images, which the target sees as a USB drive. It does this
      through the device's private API, whose login Radxa's own
      harness-linkr documents: `POST /api/account/login` with
      `{"username", "passwd": sha256("RADXA_FIXED_SALT_v1" + password)}`,
      answered with a JWT for `Authorization: Bearer ...`. The virtual
      disk endpoints themselves are not documented anywhere public, and
      the firmware images on GitHub are encrypted, so read them from the
      web interface's JavaScript as a live Linkr serves it. The 1.6.0
      release notes say uploads are checked with MD5 once sent, so expect
      a digest in the upload. Then check whether images can be presented
      as a CD as well as a stick, and read-only.
