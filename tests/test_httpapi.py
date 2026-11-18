# Copyright (c) 2026 Matt H
#
# SPDX-License-Identifier: MIT

import hashlib
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


# ----------------------------------------------------------------------
# Pushed images
# ----------------------------------------------------------------------

@pytest.fixture
def storage_api():
    backend = RecordingBackend(capabilities={"storage.stick", "storage.cdrom"})
    http_api = HTTPApi(Daemon({"board": backend}), "127.0.0.1:0", TOKEN)
    http_api.start(0.02)
    yield http_api, backend
    http_api.stop()


def client_for(api) -> Client:
    host, port = api.address
    return Client(url="http://%s:%d" % (host, port), token=TOKEN)


def insert_request(contents: bytes) -> dict:
    upload = {"name": "disk.img", "size": len(contents),
              "sha256": hashlib.sha256(contents).hexdigest()}
    return {"command": "storage.insert", "target": "board", "args": {"upload": upload}}


def test_client_pushes_an_image_over_http(storage_api, tmp_path):
    api, backend = storage_api
    contents = bytes(range(256)) * 40
    image = tmp_path / "disk.img"
    image.write_bytes(contents)
    status = client_for(api).storage_insert("board", image, mode="cdrom", push=True)
    assert status["mode"] == "cdrom"
    assert backend.calls == [("storage_insert", ("pushed", "disk.img", contents), "cdrom", False)]


def test_an_image_the_kvm_has_is_not_sent_over_http(storage_api, tmp_path):
    api, backend = storage_api
    image = tmp_path / "disk.img"
    image.write_bytes(b"x" * 600)
    backend.has_image = True
    client_for(api).storage_insert("board", image, push=True)
    assert backend.calls == [("storage_insert", ("pushed", "disk.img", None), "stick", False)]


def test_command_asks_for_the_image(storage_api):
    api, backend = storage_api
    status, reply = send(api, "POST", "/v1/command", json.dumps(insert_request(b"x" * 600)))
    assert (status, reply) == (200, {"ok": True, "send": 600})
    assert backend.calls == []


def test_upload_of_an_image_that_does_not_match(storage_api):
    api, backend = storage_api
    headers = {"Koid-Request": json.dumps(insert_request(b"a" * 600)),
               "Content-Type": "application/octet-stream"}
    status, reply = send(api, "POST", "/v1/upload", b"b" * 600, headers=headers)
    assert status == 500
    assert reply["error"]["message"] == "the image does not match its SHA-256"


@pytest.mark.parametrize("koid_request, body", [
    (None, b"x" * 600),
    ("not json", b"x" * 600),
    (json.dumps({"command": "keyboard.type", "target": "board", "args": {"text": "x"}}),
     b"x" * 600),
    # The body's length must be the size announced.
    (json.dumps(insert_request(b"x" * 600)), b"x" * 599),
])
def test_bad_uploads(storage_api, koid_request, body):
    api, backend = storage_api
    headers = {} if koid_request is None else {"Koid-Request": koid_request}
    status, reply = send(api, "POST", "/v1/upload", body, headers=headers)
    assert status == 400
    assert "Koid-Request" in reply["error"]["message"]
    assert backend.calls == []


def test_upload_needs_the_token(storage_api):
    api, _backend = storage_api
    headers = {"Koid-Request": json.dumps(insert_request(b"x" * 600))}
    status, _reply = send(api, "POST", "/v1/upload", b"x" * 600, token=None, headers=headers)
    assert status == 401
