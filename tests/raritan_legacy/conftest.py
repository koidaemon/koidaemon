# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import json
import pathlib

import pytest

DATA = pathlib.Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def capture():
    """Login messages captured from Raritan's own client and a real switch.

    Values are hex strings in the JSON file; this returns them as bytes
    (except the password).
    """
    raw = json.loads((DATA / "login_capture.json").read_text())
    values = {}
    for name, value in raw.items():
        if name.startswith("_") or name == "password":
            values[name] = value
        else:
            values[name] = bytes.fromhex(value)
    return values
