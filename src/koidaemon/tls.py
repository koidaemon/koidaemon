# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""TLS settings for backends that reach their KVM over HTTPS."""

from __future__ import annotations

import pathlib
import ssl


def client_context(ca_file: pathlib.Path | None, verify: bool) -> ssl.SSLContext:
    """How to check a KVM's certificate.

    With ca_file, that certificate is trusted whatever name the KVM is
    reached by: KVMs' own certificates name the device, which rarely
    matches, and trusting that one certificate is the check that matters.
    Otherwise the system's CAs are used, unless verify is false.
    """
    if ca_file is not None:
        context = ssl.create_default_context(cafile=str(ca_file))
        context.check_hostname = False
        return context
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
