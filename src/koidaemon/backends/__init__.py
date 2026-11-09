# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""KVM backends, looked up by the name used in the configuration file.

Backends are imported only when a target uses them.
"""

from __future__ import annotations

import importlib

from koidaemon.errors import ConfigError

# Backend name -> "module:class".
BACKENDS = {
    "pikvm": "koidaemon.backends.pikvm:PiKVMBackend",
    "raritan-legacy": "koidaemon.backends.raritan_legacy:RaritanLegacyBackend",
}


def backend_class(name: str) -> type:
    """The class implementing a backend name from the configuration."""
    if name not in BACKENDS:
        raise ConfigError("unknown backend %r (known: %s)" % (name, ", ".join(sorted(BACKENDS))))
    module_name, class_name = BACKENDS[name].split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)
