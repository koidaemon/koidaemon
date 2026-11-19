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
