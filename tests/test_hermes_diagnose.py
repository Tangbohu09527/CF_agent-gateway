from __future__ import annotations

import json
from contextlib import nullcontext

import httpx
import pytest

from cf_agent_gateway.config import HermesSettings
from cf_agent_gateway.hermes.client import HermesClient
from cf_agent_gateway.hermes.diagnose import diagnose, main
from cf_agent_gateway.hermes.errors import HermesResponseError

SETTINGS = HermesSettings(enabled=True, base_url="http://hermes.test:8642")
KEY = "unit-test-secret-do-not-log"


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def connect(address: object, *, timeout: float) -> object:
        calls.append((address, timeout))
        return nullcontext()

    monkeypatch.setattr("cf_agent_gateway.hermes.diagnose.socket.create_connection", connect)
    return calls


def test_default_only_connects_socket_without_key_or_http(connected: list[object]) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("default diagnostic sent HTTP")

    result = diagnose(SETTINGS, environ={}, transport=httpx.MockTransport(forbidden))
    assert connected == [(("hermes.test", 8642), 5.0)]
    assert result["ok"] is True
    assert result["network"] == "tcp_connected"
    assert result["authentication"] == "not_checked"
    assert result["application"] == "not_checked"
    assert result["production_acceptance"] is False


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError("private target"), "connect_timeout"),
        (OSError("private target"), "connect_failed"),
    ],
)
def test_socket_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr("cf_agent_gateway.hermes.diagnose.socket.create_connection", fail)
    result = diagnose(SETTINGS, environ={})
    assert result["error"] == code
    assert "private target" not in json.dumps(result)


def _models(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.path == "/v1/models"
    if request.headers["authorization"] != f"Bearer {KEY}":
        return httpx.Response(401)
    return httpx.Response(200, json={"object": "list", "data": [{"id": "hermes-agent"}]})


def test_auth_opt_in_never_calls_model(connected: list[object]) -> None:
    result = diagnose(
        SETTINGS,
        environ={"HERMES_API_KEY": KEY},
        check_auth=True,
        transport=httpx.MockTransport(_models),
    )
    assert result["ok"] is True
    assert result["application"] == "not_checked"
    assert result["authentication"] == "models_authenticated_and_wrong_key_rejected"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {}, "configured_key_rejected"),
        (404, {}, "models_contract_unavailable"),
        (200, {"status": "ok"}, "models_response_invalid"),
    ],
)
def test_authentication_failures(
    connected: list[object], status: int, payload: dict, expected: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] != f"Bearer {KEY}":
            return httpx.Response(401)
        return httpx.Response(status, json=payload)

    result = diagnose(
        SETTINGS,
        environ={"HERMES_API_KEY": KEY},
        check_auth=True,
        transport=httpx.MockTransport(handler),
    )
    assert result["error"] == expected
    assert KEY not in json.dumps(result)


def test_open_auth_server_is_not_accepted(connected: list[object]) -> None:
    result = diagnose(
        SETTINGS,
        environ={"HERMES_API_KEY": KEY},
        check_auth=True,
        transport=httpx.MockTransport(lambda req: httpx.Response(200)),
    )
    assert result["error"] == "wrong_key_not_rejected"


@pytest.mark.parametrize("options", [{"check_replay": True}, {"allow_model_call": True}])
def test_model_opt_in_and_v2_profile_fail_before_network(
    connected: list[object], options: dict
) -> None:
    result = diagnose(SETTINGS, environ={"HERMES_API_KEY": KEY}, **options)
    assert not connected
    assert result["ok"] is False


def test_v2_model_and_replay_preserve_request_without_claiming_dedup(
    connected: list[object],
) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _models(request)
        if request.headers["authorization"] != f"Bearer {KEY}":
            return httpx.Response(401)
        posts.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["profile_reference"] == "approved-test-profile"
        assert payload["profile_revision"] == 7
        assert payload["session_metadata"]["thread_policy"] == "private_sender"
        assert payload["session_metadata"]["context_available"] is False
        assert payload["thread_id"] == request.headers["X-Hermes-Session-Id"]
        assert request.headers["Idempotency-Key"] == payload["thread_id"]
        marker = payload["messages"][0]["content"].split("Reply exactly: ")[1]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": marker}}]},
            headers={"X-Hermes-Session-Id": payload["thread_id"]},
        )

    result = diagnose(
        SETTINGS,
        environ={"HERMES_API_KEY": KEY},
        allow_model_call=True,
        check_replay=True,
        profile_reference="approved-test-profile",
        profile_revision=7,
        transport=httpx.MockTransport(handler),
    )
    assert result["ok"] is True
    assert len(posts) == 2
    assert posts[0].content == posts[1].content
    assert result["application"] == "unique_marker_returned"
    assert result["replay"] == "response_consistent_execution_count_unverified"
    assert result["durable_idempotency"] == "requires_upstream_evidence"
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("problem", ["missing_session", "invalid_json", "wrong_marker", "timeout"])
def test_http_200_is_not_model_acceptance(connected: list[object], problem: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _models(request)
        if request.headers["authorization"] != f"Bearer {KEY}":
            return httpx.Response(401)
        if problem == "timeout":
            raise httpx.ReadTimeout(KEY, request=request)
        if problem == "invalid_json":
            return httpx.Response(200, text=KEY)
        headers = {} if problem == "missing_session" else {"X-Hermes-Session-Id": "test"}
        return httpx.Response(
            200,
            headers=headers,
            json={"choices": [{"message": {"role": "assistant", "content": KEY}}]},
        )

    result = diagnose(
        SETTINGS,
        environ={"HERMES_API_KEY": KEY},
        allow_model_call=True,
        profile_reference="approved-test-profile",
        profile_revision=1,
        transport=httpx.MockTransport(handler),
    )
    assert result["ok"] is False
    assert result["application"] == "not_checked"
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize(
    ("headers", "metadata"),
    [
        ({"X-Hermes-Completed": "false"}, {}),
        ({"X-Hermes-Partial": "true"}, {}),
        ({"X-Hermes-Error": KEY}, {}),
        ({}, {"completed": False}),
        ({}, {"partial": True}),
        ({}, {"failed": True}),
        ({}, {"error": KEY}),
    ],
)
def test_client_rejects_explicit_partial_or_failed_http_200(headers: dict, metadata: dict) -> None:
    with (
        HermesClient(
            SETTINGS.base_url,
            KEY,
            SETTINGS.model,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"X-Hermes-Session-Id": "test", **headers},
                    json={
                        "hermes": metadata,
                        "choices": [{"message": {"role": "assistant", "content": KEY}}],
                    },
                )
            ),
        ) as client,
        pytest.raises(HermesResponseError) as caught,
    ):
        client.chat("acceptance")
    assert KEY not in str(caught.value)


def test_cli_redacts_config_error(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("hermes: [\n" + KEY, encoding="utf-8")
    assert main(["--config", str(path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"] == "config_load_failed"
    assert KEY not in output


def test_model_probe_checks_post_auth_before_valid_request(connected: list[object]) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _models(request)
        posts.append(request)
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": "test"},
            json={"choices": [{"message": {"role": "assistant", "content": "bad auth"}}]},
        )

    result = diagnose(
        SETTINGS,
        environ={"HERMES_API_KEY": KEY},
        allow_model_call=True,
        profile_reference="approved-test-profile",
        profile_revision=1,
        transport=httpx.MockTransport(handler),
    )
    assert result["error"] == "wrong_post_key_not_rejected"
    assert len(posts) == 1
    assert posts[0].headers["authorization"] != f"Bearer {KEY}"
    assert result["application"] == "not_checked"
