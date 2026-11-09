# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import http.client
import json
import logging
import ssl

import pytest

from koidaemon.client import Client
from koidaemon.daemon import MAX_REQUEST, Daemon
from koidaemon.errors import Busy, KoiError, Unsupported
from koidaemon.httpapi import HTTPApi, is_loopback, split_listen

from fake_kvmd import FakeKVMD
from fakes import FAKE_PNG, RecordingBackend, make_certificate

TOKEN = "s3cret"


@pytest.fixture
def backend():
    return RecordingBackend(capabilities={"keyboard", "snapshot"})


@pytest.fixture
def api(backend):
    http_api = HTTPApi(Daemon({"board": backend}), "127.0.0.1:0", TOKEN)
    http_api.start(0.02)
    yield http_api
    http_api.stop()


def send(api, method, path, body=None, token=TOKEN, headers=None):
    """Make a raw request; returns (status, decoded JSON reply)."""
    host, port = api.address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    all_headers = dict(headers or {})
    if token is not None:
        all_headers["Authorization"] = "Bearer " + token
    connection.request(method, path, body=body, headers=all_headers)
    response = connection.getresponse()
    reply = json.loads(response.read())
    connection.close()
    return response.status, reply


def command(name, target="board", **args):
    return json.dumps({"command": name, "target": target, "args": args})


def test_command(api, backend):
    status, reply = send(api, "POST", "/v1/command", command("keyboard.type", text="hi"))
    assert (status, reply) == (200, {"ok": True, "result": {}})
    assert backend.calls == [("type_text", "hi")]


def test_targets(api):
    status, reply = send(api, "GET", "/v1/targets")
    assert status == 200
    assert reply["result"]["targets"]["board"]["capabilities"] == ["keyboard", "snapshot"]


@pytest.mark.parametrize("token", [None, "wrong", TOKEN + "x"])
def test_a_token_is_required(api, backend, token):
    status, reply = send(api, "POST", "/v1/command", command("keyboard.type", text="hi"),
                         token=token)
    assert status == 401
    assert reply["error"]["code"] == "unauthorised"
    assert backend.calls == []


def test_a_token_is_required_for_targets_too(api):
    status, _reply = send(api, "GET", "/v1/targets", token=None)
    assert status == 401


@pytest.mark.parametrize("method, path", [("GET", "/v1/command"), ("POST", "/v2/command"),
                                          ("GET", "/")])
def test_unknown_paths(api, method, path):
    status, reply = send(api, method, path, body=b"{}")
    assert status == 404
    assert reply["error"]["message"] == "no such path"


def test_not_json(api):
    status, reply = send(api, "POST", "/v1/command", body=b"{nope")
    assert (status, reply["error"]["message"]) == (400, "not JSON")


def test_request_size_is_limited(api):
    status, _reply = send(api, "POST", "/v1/command", body=b"x" * (MAX_REQUEST + 1))
    assert status == 400


@pytest.mark.parametrize("body, error, status", [
    (command("mouse.move", x=1, y=1), None, 422),
    (command("status", "nobody"), None, 404),
    (command("keyboard.type"), None, 400),
    (command("keyboard.type", text="x"), Busy("in use"), 503),
])
def test_http_status_follows_the_reply(api, backend, body, error, status):
    backend.error = error
    got, reply = send(api, "POST", "/v1/command", body)
    assert got == status
    assert reply["ok"] is False


def test_client_over_http(api, backend):
    host, port = api.address
    client = Client(url="http://%s:%d/" % (host, port), token=TOKEN)
    client.call("keyboard.type", "board", text="hello")
    assert backend.calls == [("type_text", "hello")]
    assert client.snapshot("board").data == FAKE_PNG
    with pytest.raises(Unsupported):
        client.call("mouse.move", "board", x=0, y=0)


def test_client_with_a_wrong_token(api):
    host, port = api.address
    client = Client(url="http://%s:%d" % (host, port), token="wrong")
    with pytest.raises(KoiError, match="valid bearer token"):
        client.targets()


def test_client_given_something_that_is_not_koid():
    with FakeKVMD() as other_server:
        other_server.answer_everything_with = (200, b"<html>hello</html>")
        client = Client(url=other_server.url, token=TOKEN)
        with pytest.raises(KoiError, match="not JSON"):
            client.targets()


def test_client_needs_a_token():
    with pytest.raises(KoiError, match="needs a token"):
        Client(url="http://127.0.0.1:1")


def test_client_cannot_connect():
    client = Client(url="http://127.0.0.1:1", token=TOKEN, timeout=2)
    with pytest.raises(KoiError, match="cannot reach koid"):
        client.targets()


def test_an_api_needs_a_token(backend):
    with pytest.raises(ValueError, match="needs a token"):
        HTTPApi(Daemon({"board": backend}), "127.0.0.1:0", "")


def test_listening_on_the_network_without_tls_warns(backend, caplog):
    with caplog.at_level(logging.WARNING):
        http_api = HTTPApi(Daemon({"board": backend}), "0.0.0.0:0", TOKEN)
    http_api.stop()
    assert "without TLS" in caplog.text


def test_listening_on_localhost_does_not_warn(backend, caplog):
    with caplog.at_level(logging.WARNING):
        http_api = HTTPApi(Daemon({"board": backend}), "127.0.0.1:0", TOKEN)
    http_api.stop()
    assert caplog.text == ""


@pytest.mark.parametrize("listen, parts", [
    ("127.0.0.1:8470", ("127.0.0.1", 8470)),
    ("[::1]:80", ("::1", 80)),
    ("worker.lab:9000", ("worker.lab", 9000)),
])
def test_split_listen(listen, parts):
    assert split_listen(listen) == parts


@pytest.mark.parametrize("listen", ["8470", "host:", "host:http"])
def test_split_listen_errors(listen):
    with pytest.raises(ValueError, match="host:port"):
        split_listen(listen)


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("::1", True), ("localhost", True),
    ("0.0.0.0", False), ("192.0.2.1", False), ("worker.lab", False),
])
def test_is_loopback(host, loopback):
    assert is_loopback(host) is loopback


# ----------------------------------------------------------------------
# HTTPS
# ----------------------------------------------------------------------

@pytest.fixture
def certificate(tmp_path):
    return make_certificate(tmp_path)


def test_https(backend, certificate):
    cert_file, key_file = certificate
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    http_api = HTTPApi(Daemon({"board": backend}), "127.0.0.1:0", TOKEN, tls)
    http_api.start(0.02)
    try:
        url = "https://127.0.0.1:%d" % http_api.address[1]

        trusting = Client(url=url, token=TOKEN, ca_file=cert_file)
        trusting.call("keyboard.type", "board", text="secure")
        assert backend.calls == [("type_text", "secure")]

        # Without the certificate the client refuses to talk.
        doubting = Client(url=url, token=TOKEN, timeout=5)
        with pytest.raises(KoiError, match="cannot reach koid"):
            doubting.targets()
    finally:
        http_api.stop()
