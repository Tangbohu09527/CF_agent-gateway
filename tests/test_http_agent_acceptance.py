"""Offline acceptance-driver regressions; all HTTP is synthetic MockTransport.

These exercise the production HermesClient serializer, transport and parser, not
the installed Hermes Agent or either approved live sample.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from cf_agent_gateway.hermes.errors import HermesAPIError

PROBE_PATH = Path(__file__).parent / "http_agent_acceptance" / "run.py"
SYNTHETIC_KEY = "offline-synthetic-http-key-never-a-real-credential"


def load_probe():
    spec = importlib.util.spec_from_file_location("http_acceptance_offline", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return load_probe()


@pytest.fixture
def args(tmp_path, monkeypatch):
    ca = tmp_path / "synthetic-ca.pem"
    ca.write_bytes(b"offline fixture, not a usable certificate")
    reference = tmp_path / "synthetic-reference.json"
    reference.write_text(
        json.dumps(
            {
                "base_url": "https://files.invalid",
                "ca_file": str(ca),
                "ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(),
                "token_file": str(tmp_path / "never-read-token-reference"),
                "max_download_bytes": 1048576,
                "direct_connection": True,
            }
        ),
        encoding="utf-8",
    )
    # python-dotenv belongs to the installed acceptance runtime, not Gateway.
    # A tiny synthetic module keeps normal repository CI independent of it.
    dotenv = ModuleType("dotenv")
    dotenv.dotenv_values = lambda _path: {"API_SERVER_KEY": SYNTHETIC_KEY}
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    output = tmp_path / "evidence"
    output.mkdir()
    return SimpleNamespace(
        output=output,
        reference=reference,
        auth_env=tmp_path / "not-read.env",
        python=Path(sys.executable),
        task="pdf",
        remote_path="/approved-synthetic.pdf",
        origin="https://hermes.invalid/p/approved",
        file_origin="https://files.invalid/",
    )


def install_http(
    monkeypatch, args, *, failure=None, fresh_status=404, trace_count=2, trace_session=None
):
    """Attach a mock below the real HermesClient transport and serialization."""
    requests = []
    real_async_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            if isinstance(failure, Exception):
                raise failure
            session = request.headers["X-Hermes-Session-Id"]
            payload = {
                "id": "chatcmpl-offline",
                "object": "chat.completion",
                "model": "hermes-agent",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Synthetic final"}}
                ],
            }
            headers = {"X-Hermes-Session-Id": session}
            if failure is not None:
                payload.update(failure.get("body", {}))
                headers.update(failure.get("headers", {}))
            return httpx.Response(200, json=payload, headers=headers)
        if len(requests) == 1:
            return httpx.Response(fresh_status, json={"error": "synthetic freshness check"})
        session = request.url.path.split("/sessions/")[1].split("/")[0]
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "session_id": trace_session or session,
                    "data": [
                        {"role": "assistant", "tool_calls": [{"id": "synthetic-tool-call"}]},
                        {
                            "role": "tool",
                            "tool_call_id": "synthetic-tool-call",
                            "tool_name": "read_file",
                            "content": "Synthetic tool output",
                        },
                    ],
                    "pagination": {
                        "returned": trace_count,
                        "limit": 500,
                        "offset": 0,
                        "order": "oldest",
                    },
                },
            )
        return httpx.Response(200, json={"object": "hermes.session", "session": {"id": session}})

    transport = httpx.MockTransport(handle)

    def make_client(*positional, **kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        kwargs["transport"] = transport
        return real_async_client(*positional, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return requests


def read_case(args, name):
    return json.loads((args.output / args.task / name).read_text(encoding="utf-8"))


def test_import_does_not_resolve_hosts_or_execute_requests(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Import must not run acceptance")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert callable(load_probe().run)


def test_real_client_request_and_private_evidence(probe, args, monkeypatch, capsys):
    requests = install_http(monkeypatch, args)
    probe.run(args)
    assert [r.method for r in requests] == ["GET", "POST", "GET", "GET"]
    post = requests[1]
    assert post.url.path == "/p/approved/v1/chat/completions"
    assert post.headers["Authorization"] == f"Bearer {SYNTHETIC_KEY}"
    body = json.loads(post.content)
    assert set(body) == {"model", "messages"}
    assert body["model"] == "hermes-agent"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert "/approved-synthetic.pdf" in body["messages"][0]["content"]
    intent = read_case(args, "intent.json")
    assert post.headers["X-Hermes-Session-Id"] == intent["session_id"]
    assert post.headers["Idempotency-Key"] == intent["idempotency_key"]
    assert intent["entry"] == "cf_agent_gateway.hermes.client.HermesClient.chat"
    assert "not CFserver" in intent["network_origin"]
    assert "unverified" in intent["origin_provenance"]
    assert read_case(args, "result.json") == {
        "client_accepted": True,
        "session_id": intent["session_id"],
    }
    assert (args.output / "pdf" / "final.txt").read_text(encoding="utf-8") == "Synthetic final"
    transcript = read_case(args, "response-04.json")["body"]
    assert transcript["session_id"] == intent["session_id"]
    assert transcript["data"][1]["tool_call_id"] == "synthetic-tool-call"
    assert read_case(args, "trace-completeness.json") == {
        "complete_page": True,
        "session_matches": True,
    }
    assert read_case(args, "downloads.json") == {}  # A mock is never proof of a download.
    frozen = read_case(args, "frozen.json")
    for relative, proof in frozen.items():
        data = (args.output / "pdf" / relative).read_bytes()
        assert proof == {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        assert SYNTHETIC_KEY.encode() not in data
    assert SYNTHETIC_KEY not in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [
        {"body": {"hermes": {"failed": True, "completed": False, "error": "model failed"}}},
        {"body": {"hermes": {"partial": True}}},
        {"headers": {"X-Hermes-Completed": "false"}},
        {"headers": {"X-Hermes-Error": "synthetic backend failure"}},
    ],
)
def test_http_200_incomplete_is_recorded_as_failure_without_retry(
    probe, args, monkeypatch, failure
):
    requests = install_http(monkeypatch, args, failure=failure)
    probe.run(args)
    assert read_case(args, "result.json") == {
        "client_accepted": False,
        "error_type": "HermesResponseError",
    }
    assert read_case(args, "response-02.json")["status"] == 200
    assert sum(r.method == "POST" for r in requests) == 1
    assert [r.method for r in requests[2:]] == ["GET", "GET"]
    assert not (args.output / "pdf" / "final.txt").exists()
    assert (args.output / "pdf" / "frozen.json").is_file()


def test_timeout_preserves_intent_and_reads_same_session_without_resending(
    probe, args, monkeypatch
):
    requests = install_http(monkeypatch, args, failure=httpx.ReadTimeout("synthetic timeout"))
    probe.run(args)
    assert read_case(args, "result.json")["error_type"] == "HermesTimeoutError"
    assert sum(r.method == "POST" for r in requests) == 1
    session = read_case(args, "intent.json")["session_id"]
    assert all(f"/sessions/{session}" in r.url.path for r in requests[2:])
    assert not (args.output / "pdf" / "response-02.json").exists()
    assert (args.output / "pdf" / "request-02.json").is_file()
    assert (args.output / "pdf" / "frozen.json").is_file()
    with pytest.raises(FileExistsError):
        probe.run(args)
    assert sum(r.method == "POST" for r in requests) == 1


def test_completed_case_cannot_be_repeated_or_overwritten(probe, args, monkeypatch):
    requests = install_http(monkeypatch, args)
    probe.run(args)
    before = (args.output / "pdf" / "frozen.json").read_bytes()
    with pytest.raises(FileExistsError):
        probe.run(args)
    assert sum(r.method == "POST" for r in requests) == 1
    assert (args.output / "pdf" / "frozen.json").read_bytes() == before


def test_response_session_mismatch_is_not_accepted(probe, args, monkeypatch):
    requests = install_http(
        monkeypatch, args, failure={"headers": {"X-Hermes-Session-Id": "another-session"}}
    )
    probe.run(args)
    assert read_case(args, "result.json") == {
        "client_accepted": False,
        "error_type": "ValueError",
    }
    assert not (args.output / "pdf" / "final.txt").exists()
    session = read_case(args, "intent.json")["session_id"]
    assert all(f"/sessions/{session}" in r.url.path for r in requests[2:])


@pytest.mark.parametrize(
    ("count", "session", "expected"),
    [
        (500, None, {"complete_page": False, "session_matches": True}),
        (2, "another-session", {"complete_page": True, "session_matches": False}),
    ],
)
def test_incomplete_or_wrong_session_trace_is_explicit(
    probe, args, monkeypatch, count, session, expected
):
    install_http(monkeypatch, args, trace_count=count, trace_session=session)
    probe.run(args)
    assert read_case(args, "trace-completeness.json") == expected


@pytest.mark.parametrize("status", [200, 403, 503])
def test_unproven_fresh_session_never_sends_task(probe, args, monkeypatch, status):
    requests = install_http(monkeypatch, args, fresh_status=status)
    with pytest.raises(ValueError if status == 200 else HermesAPIError):
        probe.run(args)
    assert [r.method for r in requests] == ["GET"]
    assert (args.output / "pdf" / "intent.json").is_file()


@pytest.mark.parametrize("mutation", ["origin", "limit", "proxy", "ca"])
def test_reference_policy_mismatch_stops_before_http(probe, args, monkeypatch, mutation):
    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    if mutation == "origin":
        reference["base_url"] = "https://unapproved.invalid"
    elif mutation == "limit":
        reference["max_download_bytes"] += 1
    elif mutation == "proxy":
        reference["direct_connection"] = False
    else:
        reference["ca_sha256"] = "0" * 64
    args.reference.write_text(json.dumps(reference), encoding="utf-8")
    requests = install_http(monkeypatch, args)
    with pytest.raises(ValueError):
        probe.run(args)
    assert requests == []


def test_http_authorization_echo_is_not_saved(probe, args, monkeypatch):
    requests = install_http(
        monkeypatch,
        args,
        failure={"body": {"unexpected_auth_echo": SYNTHETIC_KEY}},
    )
    probe.run(args)
    assert read_case(args, "result.json")["client_accepted"] is False
    assert not (args.output / "pdf" / "response-02.json").exists()
    assert sum(r.method == "POST" for r in requests) == 1
    for path in (args.output / "pdf").rglob("*"):
        if path.is_file():
            assert SYNTHETIC_KEY.encode() not in path.read_bytes()


def test_http_authorization_echo_in_header_is_not_saved(probe, args, monkeypatch):
    requests = install_http(
        monkeypatch,
        args,
        failure={"headers": {"X-Hermes-Error": f"synthetic echo: {SYNTHETIC_KEY}"}},
    )
    probe.run(args)
    assert read_case(args, "result.json") == {
        "client_accepted": False,
        "error_type": "ValueError",
    }
    assert not (args.output / "pdf" / "response-02.json").exists()
    assert sum(request.method == "POST" for request in requests) == 1
    for path in (args.output / "pdf").rglob("*"):
        if path.is_file():
            assert SYNTHETIC_KEY.encode() not in path.read_bytes()


def test_work_root_junction_is_rejected_before_traversal_or_fingerprinting(
    probe, tmp_path, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    (work / "synthetic.pdf").write_bytes(b"This file must not be read through a linked root")
    # A synthetic junction avoids requiring Windows symlink privileges. It also
    # exercises the separate junction guard on Linux CI, where is_symlink=False.
    monkeypatch.setattr(Path, "is_junction", lambda path: path == work)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Linked-root evidence must be refused before reading its contents")

    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(probe, "fingerprint", forbidden)
    with pytest.raises(ValueError, match="Evidence root is a link"):
        probe.freeze(work)
    assert not (work / "frozen.json").exists()
