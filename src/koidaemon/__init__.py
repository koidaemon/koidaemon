# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""koidaemon: drive machines behind KVM-over-IP devices.

koid, the daemon, owns the connections to the KVMs; koictl and the Python
client in koidaemon.client ask it to type, click, take snapshots and present
USB storage, with the same commands whatever the KVM.
"""

__version__ = "0.1.0"
