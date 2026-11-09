# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import pytest

from koidaemon.backends.raritan_legacy.rc4 import RC4


# Widely published RC4 test vectors (key, plaintext, ciphertext).
VECTORS = [
    (b"Key", b"Plaintext", "bbf316e8d940af0ad3"),
    (b"Wiki", b"pedia", "1021bf0420"),
    (b"Secret", b"Attack at dawn", "45a01f645fc35b383552544b9bf5"),
]


@pytest.mark.parametrize("key, plaintext, ciphertext", VECTORS)
def test_known_vectors(key, plaintext, ciphertext):
    assert RC4(key).crypt(plaintext).hex() == ciphertext


def test_decrypting_returns_the_plaintext():
    secret = RC4(b"k").crypt(b"hello world")
    assert RC4(b"k").crypt(secret) == b"hello world"


def test_keystream_continues_across_calls():
    whole = RC4(b"Secret").crypt(b"Attack at dawn")
    split = RC4(b"Secret")
    assert split.crypt(b"Attack ") + split.crypt(b"at dawn") == whole


def test_empty_key_is_rejected():
    with pytest.raises(ValueError):
        RC4(b"")
