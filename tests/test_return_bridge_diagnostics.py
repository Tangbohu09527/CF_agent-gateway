"""Rejection logs identify fixed branches without retaining request material."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re

import pytest
from test_return_bridge_plugin import KEY, bridge, capability, headers

from cf_agent_gateway.hermes.return_bridge import plugin
from cf_agent_gateway.hermes.return_bridge.files import ReturnBridgeError
from cf_agent_gateway.hermes.return_bridge.protocol import RETURN_ACK_HEADER, RETURN_AUTH_HEADER

PRIVATE = "private-body-header-session-path-exception-marker"
BODY = {"messages": [{"role": "user", "content": PRIVATE}]}


def private_headers():
    return headers(capability(session=PRIVATE), session=PRIVATE)


def assert_rejection(caplog, stage, reason, incoming, *, count=1):
    records = [record for record in caplog.records if record.name == plugin.__name__]
    assert len(records) == count
    for record in records:
        assert record.args[:2] == (stage, reason)
        assert re.fullmatch(r"[0-9a-f]{32}", record.args[2])
        assert record.exc_info is None and record.stack_info is None
        assert record.getMessage() == (
            f"Artifact return rejected stage={stage} reason={reason} request_id={record.args[2]}"
        )
        assert PRIVATE not in record.getMessage()
        for value in incoming.values():
            assert value not in record.getMessage()
    return records


@pytest.mark.parametrize(
    ("case", "status", "stage", "reason"),
    [
        ("missing-key", 401, "auth", "api_key_missing"),
        ("wrong-key", 401, "auth", "native_auth_rejected"),
        ("get", 400, "request", "invalid_endpoint"),
        ("wrong-path", 400, "request", "invalid_endpoint"),
        ("missing-header", 400, "request", "invalid_headers"),
        ("duplicate-header", 400, "request", "invalid_headers"),
        ("non-object", 400, "request", "invalid_body"),
        ("stream", 400, "request", "invalid_body"),
        ("malformed-json", 403, "request", "value_error"),
        ("wrong-session", 403, "context", "invalid_return_context"),
        ("wrong-profile", 403, "profile", "profile_mismatch"),
        ("wrong-return-url", 403, "scope", "invalid_return_context"),
        ("cached", 409, "completion", "agent_not_bound"),
        ("deadline", 504, "execution", "execution_deadline"),
    ],
)
def test_denials_preserve_responses_and_log_only_fixed_fields(
    tmp_path, caplog, case, status, stage, reason
):
    caplog.set_level(logging.WARNING, logger=plugin.__name__)

    async def run():
        mode = "cached" if case == "cached" else "late" if case == "deadline" else "agent"
        async with bridge(
            tmp_path / PRIVATE,
            deadline=1 if case == "deadline" else 5,
            handler_mode=mode,
            key="" if case == "missing-key" else KEY,
        ) as data:
            client = data[0]
            incoming = private_headers()
            body = dict(BODY)
            if case == "wrong-key":
                incoming["Authorization"] = "Bearer " + PRIVATE
            elif case == "missing-header":
                incoming.pop("X-Hermes-Session-Id")
            elif case == "wrong-session":
                incoming["X-Hermes-Session-Id"] += "-mismatch"
            elif case == "wrong-profile":
                token = incoming[RETURN_AUTH_HEADER].split()[1].split(".")[0]
                claims = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
                claims["profile"] = PRIVATE
                encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
                incoming[RETURN_AUTH_HEADER] = "Bearer " + encoded.decode() + "." + "b" * 64
            elif case == "wrong-return-url":
                incoming["X-CF-Artifact-Return-URL"] += "-" + PRIVATE
            elif case == "stream":
                body["stream"] = True
            elif case == "non-object":
                body = [PRIVATE]
            sent_headers = (
                [*incoming.items(), (RETURN_AUTH_HEADER, PRIVATE)]
                if case == "duplicate-header"
                else incoming
            )
            response = await client.request(
                "GET" if case == "get" else "POST",
                "/private-path" if case == "wrong-path" else "/v1/chat/completions",
                headers=sent_headers,
                **({"content": PRIVATE} if case == "malformed-json" else {"json": body}),
            )
            assert response.status_code == status
            assert RETURN_ACK_HEADER not in response.headers
            assert response.json() == {
                "error": "unauthorized" if case == "wrong-key" else "artifact return unavailable"
            }
            if case != "wrong-key":
                assert response.headers["Cache-Control"] == "no-store"
            assert_rejection(caplog, stage, reason, incoming)

    (tmp_path / PRIVATE).mkdir()
    asyncio.run(run())


def test_unready_tls_logs_transport_rejection(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=plugin.__name__)
    configure = plugin.configure_app
    states = []

    def capture(*args, **kwargs):
        result = configure(*args, **kwargs)
        states.append(result)
        return result

    monkeypatch.setattr(plugin, "configure_app", capture)

    async def run():
        async with bridge(tmp_path) as data:
            states[0]["ready"] = False
            incoming = private_headers()
            response = await data[0].post("/v1/chat/completions", headers=incoming, json=BODY)
            assert response.status_code == 503 and RETURN_ACK_HEADER not in response.headers
            assert_rejection(caplog, "transport", "tls_unavailable", incoming)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error", "reason", "status"),
    [
        (ReturnBridgeError("return_ledger_unavailable"), "return_ledger_unavailable", 403),
        (ReturnBridgeError("return_capacity_exceeded"), "return_capacity_exceeded", 403),
        (ReturnBridgeError("return_context_consumed"), "return_context_consumed", 409),
        (ReturnBridgeError("invalid_return_context"), "invalid_return_context", 403),
        (ReturnBridgeError(PRIVATE), "bridge_error", 403),
        (ValueError(PRIVATE), "value_error", 403),
        (TypeError(PRIVATE), "type_error", 403),
        (KeyError(PRIVATE), "key_error", 403),
    ],
)
def test_exception_diagnostics_never_log_exception_values(
    tmp_path, monkeypatch, caplog, error, reason, status
):
    caplog.set_level(logging.WARNING, logger=plugin.__name__)

    def fail(*_args):
        raise error

    monkeypatch.setattr(plugin, "_consume_capability", fail)

    async def run():
        async with bridge(tmp_path) as data:
            incoming = private_headers()
            for _ in range(2):
                response = await data[0].post("/v1/chat/completions", headers=incoming, json=BODY)
                assert response.status_code == status and RETURN_ACK_HEADER not in response.headers
                assert response.json() == {"error": "artifact return unavailable"}
            records = assert_rejection(caplog, "ledger", reason, incoming, count=2)
            assert records[0].args[2] != records[1].args[2]
            assert data[1] == data[3] == []

    asyncio.run(run())


def test_success_has_no_rejection_diagnostic(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=plugin.__name__)

    async def run():
        async with bridge(tmp_path) as data:
            response = await data[0].post(
                "/v1/chat/completions", headers=private_headers(), json=BODY
            )
            assert response.status_code == 200 and RETURN_ACK_HEADER in response.headers
            assert not [record for record in caplog.records if record.name == plugin.__name__]

    asyncio.run(run())


def test_closed_scope_identifies_completion_stage(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=plugin.__name__)
    bind = plugin.pre_llm_call

    def close_after_binding(**kwargs):
        result = bind(**kwargs)
        plugin._execution.get().scope.revoke()
        return result

    monkeypatch.setattr(plugin, "pre_llm_call", close_after_binding)

    async def run():
        async with bridge(tmp_path) as data:
            incoming = private_headers()
            response = await data[0].post("/v1/chat/completions", headers=incoming, json=BODY)
            assert response.status_code == 403 and RETURN_ACK_HEADER not in response.headers
            assert_rejection(caplog, "completion", "return_scope_closed", incoming)

    asyncio.run(run())


def test_disconnect_is_distinguished_from_deadline(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=plugin.__name__)

    async def run():
        async with bridge(tmp_path, handler_mode="late") as data:
            incoming = private_headers()
            pending = asyncio.create_task(
                data[0].post("/v1/chat/completions", headers=incoming, json=BODY)
            )
            async with asyncio.timeout(5):
                while not data[1]:
                    await asyncio.sleep(0.01)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                while not data[1][0]._cancelled.is_set():
                    await asyncio.sleep(0.01)
            assert_rejection(caplog, "execution", "client_disconnected", incoming)

    asyncio.run(run())
