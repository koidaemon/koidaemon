# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""Errors, each with the short code the daemon sends back to clients."""

from __future__ import annotations


class KoiError(Exception):
    """Base class.  code is how the error is named on the wire."""

    code = "failed"


class InvalidRequest(KoiError):
    """The request is malformed or its arguments are wrong."""

    code = "invalid"


class UnknownTarget(KoiError):
    """No target with that name is configured."""

    code = "unknown-target"


class Unsupported(KoiError):
    """The target's KVM cannot do this."""

    code = "unsupported"


class Busy(KoiError):
    """The KVM is busy for now; trying again later can work."""

    code = "busy"


class BackendError(KoiError):
    """The KVM failed to do what was asked."""

    code = "failed"


class ConfigError(KoiError):
    """The configuration file is wrong."""

    code = "config"


# Look an error class up by its wire code, for the client side.
ERRORS_BY_CODE = {
    error.code: error
    for error in (InvalidRequest, UnknownTarget, Unsupported, Busy, BackendError, ConfigError)
}
