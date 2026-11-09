# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

"""RC4 stream cipher.

The Dominion KX uses RC4 twice: as the SSLv3 cipher during login, and on its
own for the rest of the session.  RC4 is broken and modern crypto libraries
have dropped it, so it is implemented here.  It is only ever used to talk to
this one piece of old hardware.
"""

from __future__ import annotations


class RC4:
    """One direction of an RC4 stream.

    The cipher is stateful: each call to crypt() continues the keystream
    where the previous call stopped, so use one instance per direction.
    """

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ValueError("RC4 key must not be empty")

        # Key-scheduling algorithm (KSA).
        state = list(range(256))
        j = 0
        for i in range(256):
            j = (j + state[i] + key[i % len(key)]) % 256
            state[i], state[j] = state[j], state[i]

        self._state = state
        self._i = 0
        self._j = 0

    def crypt(self, data: bytes) -> bytes:
        """Encrypt or decrypt data (the operation is the same)."""
        state = self._state
        i = self._i
        j = self._j
        out = bytearray(len(data))

        # Pseudo-random generation algorithm (PRGA), XORed with the input.
        for pos, byte in enumerate(data):
            i = (i + 1) % 256
            j = (j + state[i]) % 256
            state[i], state[j] = state[j], state[i]
            keystream_byte = state[(state[i] + state[j]) % 256]
            out[pos] = byte ^ keystream_byte

        self._i = i
        self._j = j
        return bytes(out)
