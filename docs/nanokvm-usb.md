# nanokvm-usb

For the [Sipeed NanoKVM-USB](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_USB/introduction.html),
a KVM that plugs into the machine running koid rather than the network.
Its HDMI input comes in as a USB video capture device, and its keyboard and
mouse are driven through a serial port. koid talks to both directly, so
Sipeed's browser or desktop client is not needed (and must not hold the
serial port while koid uses it).

| | |
|---|---|
| Keyboard | yes, at most 6 keys besides the modifiers held at once |
| Mouse | yes, absolute pixels |
| Snapshot | yes, captured with GStreamer as JPEG, which koid turns into PNG unless asked not to |
| USB storage | no; the NanoKVM-USB has none |

## Configuration

```toml
[targets.board-c]
backend = "nanokvm-usb"
serial = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5C82073351-if00"
video = "/dev/v4l/by-id/usb-MACROSILICON_USB3_Video_87538134-video-index0"
width = 1920                       # optional; default 1920
height = 1080                      # optional; default 1080
timeout = 10                       # optional; seconds
gst_launch = "gst-launch-1.0"      # optional; GStreamer's command
```

The NanoKVM-USB shows up as two USB devices: a QinHeng serial port
(`1a86:55d3`) and a MacroSilicon MS2130 capture device (`345f:2131`).
Name them by their `/dev/serial/by-id/` and `/dev/v4l/by-id/` links, which
stay the same across reboots and replugging, unlike `ttyACM2` or `video0`.
The capture device has two nodes; use `video-index0`.

`width` and `height` are the size koid asks the capture device for. The
MS2130 scales the target's picture to it, so it need not match the
target's resolution; mouse positions are pixels in this size, which is
also the snapshot's size.

## Setting up the host

koid needs GStreamer's command line tool and its V4L2 source:

```sh
apt install gstreamer1.0-tools gstreamer1.0-plugins-good
```

and the user koid runs as must be able to open both devices: usually by
being in the `video` group for the capture device and the group that owns
the serial port (`dialout` on most systems).

## How commands map to the device

The serial port takes the CH9329 chip's framed commands at 57600 baud,
8N1, as Sipeed's own client sends them: `57 AB`, an address (0), a
command, the length of the data, the data, and a checksum (the sum of
every byte before it). The chip answers every frame, and koid checks the
answer.

| Command | Frame |
|---|---|
| `status` | `0x01` GET_INFO: chip version, whether the target has found the USB keyboard and mouse, and its lock lights |
| `keyboard.*` | `0x02`: an 8-byte keyboard report, sent for every key going down or up |
| `mouse.*` | `0x04`: buttons, a position scaled to 0..4095, and a wheel step of at most 127 |
| `snapshot` | `gst-launch-1.0 v4l2src ! image/jpeg ! multifilesink` |

Every report the chip takes gives the whole state of the keyboard or
mouse, so koid keeps what is held down with `keyboard.down` and
`mouse.down` and sends it with every report. `status` says
`usb_connected: false` while the target is off or has not found the
NanoKVM-USB's USB device; keyboard and mouse commands are then accepted
but go nowhere.

For a snapshot koid captures a few frames and keeps the last, since the
MS2130's first frames after it is opened can be dark or torn. Starting
GStreamer each time makes a snapshot take about a second.
