# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""The koid daemon and the koictl client.

koictl exit status: 0 on success, 1 on failure, 2 for bad usage, 3 if the
target's KVM cannot do what was asked.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import signal
import ssl
import sys
import threading

from koidaemon import __version__, config
from koidaemon.client import Client
from koidaemon.daemon import Daemon
from koidaemon.errors import ConfigError, KoiError, Unsupported
from koidaemon.httpapi import HTTPApi

LOG = logging.getLogger("koidaemon")

EXIT_FAILED = 1
EXIT_UNSUPPORTED = 3


def setup_logging(verbosity: int) -> None:
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


# ----------------------------------------------------------------------
# koid
# ----------------------------------------------------------------------

def koid_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="koid",
        description="Drive the keyboard, mouse, screen and USB storage of machines "
                    "behind KVM-over-IP devices.")
    parser.add_argument("-c", "--config", type=pathlib.Path, default=config.DEFAULT_CONFIG,
                        help="configuration file (default: %(default)s)")
    parser.add_argument("--socket", type=pathlib.Path,
                        help="listen here instead of the configured socket")
    parser.add_argument("--check", action="store_true",
                        help="check the configuration and exit")
    parser.add_argument("-v", "--verbose", action="count", default=1,
                        help="more logging (default: info)")
    parser.add_argument("-q", "--quiet", action="store_true", help="only log problems")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    setup_logging(0 if args.quiet else args.verbose)

    try:
        settings = config.load(args.config)
    except ConfigError as error:
        print("koid: %s" % error, file=sys.stderr)
        return EXIT_FAILED

    if args.check:
        for name, backend in sorted(settings.targets.items()):
            print("%s: %s" % (name, backend.name))
        return 0

    daemon = Daemon(settings.targets)
    try:
        daemon.listen(args.socket or settings.socket, settings.socket_mode,
                      settings.socket_group)
    except (KoiError, OSError, KeyError) as error:
        print("koid: cannot listen: %s" % error, file=sys.stderr)
        return EXIT_FAILED

    http_api = None
    if settings.http.enabled:
        try:
            http_api = _start_http_api(daemon, settings.http)
        except (OSError, ValueError, ssl.SSLError) as error:
            print("koid: cannot start the HTTP API: %s" % error, file=sys.stderr)
            daemon.close()
            return EXIT_FAILED

    def stop(signum, frame):
        LOG.info("stopping")
        # shutdown() waits for serve_forever() to return, so it cannot run
        # in the thread that is serving.
        threading.Thread(target=daemon.shutdown).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        daemon.serve_forever()
    finally:
        if http_api is not None:
            http_api.stop()
        daemon.close()
    return 0


def _start_http_api(daemon: Daemon, http: config.HTTPConfig) -> HTTPApi:
    tls = None
    if http.cert_file is not None:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(http.cert_file, http.key_file)
    http_api = HTTPApi(daemon, http.listen, http.token, tls)
    http_api.start()
    return http_api


# ----------------------------------------------------------------------
# koictl
# ----------------------------------------------------------------------

def _koictl_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="koictl",
        description="Ask koid to drive a target's keyboard, mouse, screen or USB storage.",
        epilog="examples:\n"
               "  koictl targets\n"
               "  koictl -t board-a keyboard type --enter root\n"
               "  koictl -t board-a keyboard press ctrl+alt+delete\n"
               "  koictl -t board-a mouse click --at 640 400\n"
               "  koictl -t board-a snapshot screen.jpg\n"
               "  koictl -t board-a storage insert --cdrom installer.iso",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-t", "--target", default=os.environ.get("KOI_TARGET"),
                        help="the target (default: $KOI_TARGET)")
    parser.add_argument("--socket", type=pathlib.Path,
                        help="koid's socket (default: $KOID_SOCKET or %s)" % config.DEFAULT_SOCKET)
    http = parser.add_argument_group("HTTP API", "use koid's HTTP API instead of its socket")
    http.add_argument("--url", default=os.environ.get("KOID_URL"),
                      help="e.g. https://worker:8470 (default: $KOID_URL)")
    http.add_argument("--token-file", type=pathlib.Path,
                      default=os.environ.get("KOID_TOKEN_FILE"),
                      help="file holding the API token (default: $KOID_TOKEN_FILE)")
    http.add_argument("--ca-file", type=pathlib.Path, default=os.environ.get("KOID_CA_FILE"),
                      help="trust this certificate for https (default: $KOID_CA_FILE)")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("targets", help="list the targets koid knows")
    commands.add_parser("status", help="whether the target is reachable, and what it can do")
    snapshot = commands.add_parser("snapshot", help="save a picture of the screen")
    snapshot.add_argument("output", help='file to write, or "-" for standard output')

    keyboard = commands.add_parser("keyboard", help="type and press keys")
    keyboard_actions = keyboard.add_subparsers(dest="action", required=True)
    type_parser = keyboard_actions.add_parser("type", help="type text (US layout)")
    type_parser.add_argument("text")
    type_parser.add_argument("--enter", action="store_true", help="press Enter afterwards")
    press = keyboard_actions.add_parser("press", help="press keys or combinations in turn")
    press.add_argument("keys", nargs="+", help='e.g. "f2" or "ctrl+alt+delete"')
    for action in ("down", "up"):
        held = keyboard_actions.add_parser(action, help="hold or release a key")
        held.add_argument("key")

    mouse = commands.add_parser("mouse", help="move, click and scroll (screen pixels)")
    mouse_actions = mouse.add_subparsers(dest="action", required=True)
    move = mouse_actions.add_parser("move", help="move the pointer")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    click = mouse_actions.add_parser("click", help="click a button")
    click.add_argument("--button", choices=("left", "right", "middle"), default="left")
    click.add_argument("--count", type=int, default=1, help="2 for a double click")
    click.add_argument("--at", nargs=2, type=int, metavar=("X", "Y"), help="move here first")
    for action in ("down", "up"):
        button = mouse_actions.add_parser(action, help="hold or release a button")
        button.add_argument("--button", choices=("left", "right", "middle"), default="left")
    scroll = mouse_actions.add_parser("scroll", help="turn the wheel")
    scroll.add_argument("notches", type=int, help="positive scrolls up, negative down")

    storage = commands.add_parser("storage", help="emulate a USB stick or CD drive")
    storage_actions = storage.add_subparsers(dest="action", required=True)
    insert = storage_actions.add_parser("insert", help="present a disk image")
    insert.add_argument("image", type=pathlib.Path)
    insert.add_argument("--cdrom", action="store_true", help="as a CD drive, not a USB stick")
    insert.add_argument("--writable", action="store_true", help="let the target write to it")
    storage_actions.add_parser("eject", help="disconnect the drive")
    storage_actions.add_parser("status", help="what the drive is presenting")
    return parser


def _request(args: argparse.Namespace) -> tuple[str, dict]:
    """The daemon command and arguments for parsed koictl arguments."""
    if args.command in ("targets", "status", "snapshot"):
        return args.command, {}
    name = "%s.%s" % (args.command, args.action)

    if name == "keyboard.type":
        text = args.text + ("\n" if args.enter else "")
        return name, {"text": text}
    if name == "keyboard.press":
        return name, {"keys": args.keys}
    if name in ("keyboard.down", "keyboard.up"):
        return name, {"key": args.key}

    if name == "mouse.move":
        return name, {"x": args.x, "y": args.y}
    if name == "mouse.click":
        request = {"button": args.button, "count": args.count}
        if args.at:
            request["x"], request["y"] = args.at
        return name, request
    if name in ("mouse.down", "mouse.up"):
        return name, {"button": args.button}
    if name == "mouse.scroll":
        return name, {"notches": args.notches}

    if name == "storage.insert":
        # koid opens the image, so send a path it can resolve.
        return name, {
            "image": str(args.image.resolve()),
            "mode": "cdrom" if args.cdrom else "stick",
            "writable": args.writable,
        }
    return name, {}


def _save_snapshot(client: Client, target: str, output: str) -> None:
    snapshot = client.snapshot(target)
    if output == "-":
        sys.stdout.buffer.write(snapshot.data)
        return
    with open(output, "wb") as image:
        image.write(snapshot.data)
    print("saved %s %sx%s to %s" % (snapshot.format, snapshot.width, snapshot.height, output),
          file=sys.stderr)


def _client(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Client:
    if not args.url:
        return Client(args.socket)
    if args.token_file is None:
        parser.error("the HTTP API needs --token-file or $KOID_TOKEN_FILE")
    try:
        token = pathlib.Path(args.token_file).read_text().strip()
    except OSError as error:
        parser.error("cannot read %s: %s" % (args.token_file, error.strerror))
    return Client(url=args.url, token=token, ca_file=args.ca_file)


def koictl_main(argv: list[str] | None = None) -> int:
    parser = _koictl_parser()
    args = parser.parse_args(argv)
    if args.command != "targets" and not args.target:
        parser.error("give a target with -t or $KOI_TARGET")

    client = _client(parser, args)
    name, request = _request(args)
    target = None if name == "targets" else args.target
    try:
        if name == "snapshot":
            _save_snapshot(client, target, args.output)
            return 0
        result = client.call(name, target, **request)
    except Unsupported as error:
        print("koictl: %s" % error, file=sys.stderr)
        return EXIT_UNSUPPORTED
    except (KoiError, OSError) as error:
        print("koictl: %s" % error, file=sys.stderr)
        return EXIT_FAILED

    if result:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def koid() -> int:
    return koid_main()


def koictl() -> int:
    return koictl_main()
