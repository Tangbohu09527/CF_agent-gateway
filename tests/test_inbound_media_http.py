"""No production connections. HTTP fixtures contain no real account or file data."""

import asyncio
import base64
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from cf_agent_gateway.adapters.wechat.inbound_media import ExpectedOriginal, MediaReadiness
from cf_agent_gateway.adapters.wechat.inbound_media_http import (
    InboundMediaHTTPClient,
    MediaFetchError,
    MediaSource,
)
from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging

# Headers only: these are not claims of complete PDF/JPEG structural validation.
PDF = b"%PDF-1.7\nsynthetic-fixture\n"
IMAGE = b"\xff\xd8\xffsynthetic-jpeg\xff\xd9"
TOKEN = "synthetic-http-token-not-real"


def source(kind=49, **kwargs):
    text = "synthetic.pdf" if kind == 49 else ""
    return replace(
        MediaSource(
            "account-test",
            "chat-test",
            "sender-test",
            "12",
            "10012",
            kind,
            datetime(2026, 1, 1, tzinfo=UTC),
            hashlib.sha256(text.encode()).hexdigest(),
        ),
        **kwargs,
    )


def message(kind=49):
    return {
        "localId": 12,
        "serverId": "10012",
        "chatId": "chat-test",
        "sender": "sender-test",
        "type": kind,
        "timestamp": "2026-01-01T00:00:00Z",
        "isSelf": False,
        "content": "synthetic.pdf" if kind == 49 else "",
    }


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data, *, delay=0):
        self.data, self.delay, self.closed = data, delay, False

    async def __aiter__(self):
        for i in range(0, len(self.data), 1024):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield self.data[i : i + 1024]

    async def aclose(self):
        self.closed = True


class Fixture:
    def __init__(self, kind=49):
        self.kind, self.calls, self.streams = kind, [], []
        self.mutations = {}
        self.status, self.media_headers, self.delay = 200, {}, 0
        self.media_raw = None
        self.media = {
            "type": "file" if kind == 49 else "image",
            "filename": "synthetic.pdf",
            "data": base64.b64encode(PDF if kind == 49 else IMAGE).decode(),
        }

    def payload(self, path, number):
        if path.endswith("/auth"):
            obj = {"status": "logged_in", "loggedInUser": "account-test"}
        elif "/media/" in path:
            obj = self.media
        else:
            obj = [message(self.kind)]
        mut = self.mutations.get(number)
        return mut(obj) if mut else obj

    async def handler(self, req):
        self.calls.append(str(req.url))
        assert req.headers["Authorization"] == "Bearer " + TOKEN
        assert req.headers["X-Session-Id"] == "default"
        assert req.headers["Accept-Encoding"] == "identity"
        assert req.method == "GET"
        number = len(self.calls)
        is_media = "/media/" in req.url.path
        raw = json.dumps(self.payload(req.url.path, number)).encode()
        if is_media and self.media_raw is not None:
            raw = self.media_raw
        stream = Chunks(raw, delay=self.delay if is_media else 0)
        self.streams.append(stream)
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        if is_media:
            headers.update(self.media_headers)
        return httpx.Response(self.status if is_media else 200, stream=stream, headers=headers)

    def client(self, **kwargs):
        return InboundMediaHTTPClient(
            "http://wechat.test:6174", TOKEN, transport=httpx.MockTransport(self.handler), **kwargs
        )


@pytest.mark.parametrize("kind", [3, 49])
def test_fetch_bound_bytes_empty_image_text_is_valid(kind):
    f = Fixture(kind)
    data = IMAGE if kind == 3 else PDF
    result = f.client().fetch(
        source(kind), expected=ExpectedOriginal(len(data), hashlib.sha256(data).hexdigest())
    )
    assert result.media.data == data
    assert result.media.original_comparison == "match"
    assert result.source_fingerprint == source(kind).fingerprint
    assert len(f.calls) == 5
    assert all(s.closed for s in f.streams)
    assert "account-test" not in repr(source(kind)) and TOKEN not in repr(result)


@pytest.mark.parametrize("state", ["pending", "unsupported"])
def test_no_inline_bytes_no_retry_no_staging(state, tmp_path):
    f = Fixture()
    f.media = {"type": state, "format": "", "filename": ""}
    result = f.client().fetch(source())
    assert result.media.readiness.value == state
    assert len([u for u in f.calls if "/media/" in u]) == 1
    tmp_path.chmod(0o700)
    with pytest.raises(MediaFetchError, match="media_not_ready_for_staging"):
        InboundMediaStaging(tmp_path).publish(result)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("when", [1, 4])
def test_account_change_prevents_media_or_discard_after(when):
    f = Fixture()
    f.mutations[when] = lambda obj: {**obj, "loggedInUser": "another-account"}
    with pytest.raises(MediaFetchError, match="media_account_changed"):
        f.client().fetch(source())
    assert len(f.calls) == when


@pytest.mark.parametrize("when", [2, 5])
@pytest.mark.parametrize(
    "field,value",
    [
        ("sender", "someone-else"),
        ("chatId", "another-chat"),
        ("serverId", "different-id"),
        ("type", 3),
        ("content", "changed.pdf"),
        ("isSelf", True),
        ("timestamp", "2026-01-02T00:00:00Z"),
    ],
)
def test_bound_identity_rejected(field, value, when):
    f = Fixture()
    f.mutations[when] = lambda rows: [{**rows[0], field: value}]
    with pytest.raises(MediaFetchError, match="media_source_binding_changed"):
        f.client().fetch(source())
    assert len(f.calls) == when


@pytest.mark.parametrize("transform", [lambda rows: [], lambda rows: rows + rows])
def test_missing_or_duplicate_local_id_not_guessed(transform):
    f = Fixture()
    f.mutations[2] = transform
    with pytest.raises(MediaFetchError, match="media_source_not_unique_or_not_visible"):
        f.client().fetch(source())
    assert len(f.calls) == 2


@pytest.mark.parametrize(
    "code,retryable",
    [(302, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_status_classification_and_no_network_retry(code, retryable):
    f = Fixture()
    f.status = code
    f.media_headers["Location"] = "https://elsewhere.invalid/secret"
    with pytest.raises(MediaFetchError) as err:
        f.client().fetch(source())
    assert err.value.code == f"media_http_{code}"
    assert err.value.retryable is retryable
    assert len(f.calls) == 3 and all(s.closed for s in f.streams)
    assert "secret" not in str(err.value) and TOKEN not in repr(err.value)


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Content-Encoding": "gzip"}, "media_content_encoding_not_allowed"),
        ({"Content-Type": "text/html"}, "media_json_content_type_required"),
        ({"Content-Length": "-1"}, "invalid_media_content_length"),
        ({"Content-Length": "2, 2"}, "invalid_media_content_length"),
        ({"Content-Length": "100000000"}, "media_http_body_too_large"),
        ({"Content-Length": "1"}, "media_http_body_incomplete"),
    ],
)
def test_http_frame_limits(headers, code):
    f = Fixture()
    f.media_headers = headers
    with pytest.raises(MediaFetchError, match=code):
        f.client().fetch(source())


def test_unknown_length_stream_bounded():
    f = Fixture()
    f.media_raw = b"x" * 150000
    original = f.handler

    async def handler(req):
        reply = await original(req)
        del reply.headers["Content-Length"]
        return reply

    client = InboundMediaHTTPClient(
        "http://wechat.test", TOKEN, max_bytes=100, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(MediaFetchError, match="media_http_body_too_large"):
        client.fetch(source())
    assert all(s.closed for s in f.streams)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type":"pending","type":"file"}',
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b"not-json",
        b'"\xff"',
    ],
)
def test_invalid_json_rejected_without_payload(raw):
    f = Fixture()
    f.media_raw = raw
    with pytest.raises(MediaFetchError, match="invalid_media_json"):
        f.client().fetch(source())


def test_total_deadline_even_for_slow_trickle():
    f = Fixture()
    f.delay = 0.02
    f.media_raw = b" " * 5000
    with pytest.raises(MediaFetchError) as err:
        f.client(deadline_seconds=0.05).fetch(source())
    assert err.value.code == "media_fetch_timeout" and err.value.retryable
    assert len(f.calls) == 3


def test_type_mismatch_not_published():
    f = Fixture()
    f.media = {"type": "image", "data": base64.b64encode(IMAGE).decode()}
    with pytest.raises(MediaFetchError, match="media_type_message_mismatch"):
        f.client().fetch(source())


def test_claimed_full_image_is_not_source_proof():
    f = Fixture(3)
    f.media["quality"] = "full"
    result = f.client().fetch(source(3), expected=ExpectedOriginal(287476, "c" * 64))
    assert result.media.original_comparison == "different"
    assert result.media.declared_quality == "full"


@pytest.mark.parametrize(
    "path",
    [
        "http://user:pass@example.test",
        "ftp://example.test",
        "http://example.test/other",
        "http://example.test/?token=x",
        "http://example.test/#x",
        "http://example.test:0",
    ],
)
def test_operator_endpoint_contract(path):
    with pytest.raises(MediaFetchError, match="invalid_media_endpoint"):
        InboundMediaHTTPClient(path, TOKEN)


def test_no_server_id_requires_exact_other_fields():
    f = Fixture(3)
    f.mutations[2] = f.mutations[5] = lambda rows: [{**rows[0], "serverId": None}]
    assert f.client().fetch(source(3, server_id=None)).media.readiness is MediaReadiness.READY


def test_real_http_file_fetch_then_private_stage(tmp_path):
    f = Fixture()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            assert self.headers["Authorization"] == "Bearer " + TOKEN
            raw = json.dumps(f.payload(self.path, len(calls))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = InboundMediaHTTPClient(f"http://127.0.0.1:{server.server_port}", TOKEN)
        result = client.fetch(
            source(), expected=ExpectedOriginal(len(PDF), hashlib.sha256(PDF).hexdigest())
        )
        tmp_path.chmod(0o700)
        staged = InboundMediaStaging(tmp_path).publish(result)
        assert (tmp_path / (staged.reference + ".blob")).read_bytes() == PDF
        assert len(calls) == 5 and staged.original_comparison == "match"
        assert not staged.existing
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
