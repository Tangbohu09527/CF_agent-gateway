"""Keep the public synthetic consumer fixture tied to real Gateway wire responses."""

import json
from datetime import datetime
from pathlib import Path

import httpx
from test_host_binding_http import PREFIX, TOKEN, event_headers
from test_host_binding_http import host as host_fixture
from test_host_binding_http import rig as queue_rig
from test_inbound_media_integration import gateway_server

from cf_agent_gateway.inbound.host_binding import SCHEMA, revoke_dispatch
from cf_agent_gateway.inbound.host_binding_routes import OwnerRequest, ResolveRequest

host = host_fixture
rig = queue_rig
FIXTURE = Path(__file__).parent / "fixtures" / "inbound-host-binding-v1.json"


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return type(value).__name__


def event_from(lines):
    event = {}
    for line in lines:
        if not line:
            if event:
                return event
            continue
        name, value = line.split(": ", 1)
        event[name] = json.loads(value) if name == "data" else value
    raise AssertionError("Gateway SSE ended without a complete event")


def test_consumer_fixture_is_self_consistent_and_has_valid_request_schemas():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["synthetic_only"] is True
    assert fixture["protocol_schema"] == SCHEMA
    resolve = fixture["resolve"]["request"]["json"]
    grant = fixture["resolve"]["response"]["json"]
    closed = fixture["closed"]["request"]["json"]
    ResolveRequest.model_validate(resolve)
    OwnerRequest.model_validate(closed)
    assert resolve == {key: grant[key] for key in resolve}
    assert closed == {**resolve, "claim_epoch": grant["claim_epoch"]}
    descriptor = grant["attachments"][0]
    assert datetime.fromisoformat(grant["lease_valid_until"]) < datetime.fromisoformat(
        descriptor["expires_at"]
    )
    for section in ("resolve", "events", "closed"):
        assert "synthetic-only-" in fixture[section]["request"]["headers"]["Authorization"]
    assert descriptor["url"].startswith("https://gateway.example.invalid/")
    for event in fixture["events"]["response"]["snapshots"]:
        assert event["id"] == str(event["data"]["sequence"])
        assert event["data"]["binding_id"] == grant["binding_id"]
        assert event["data"]["claim_epoch"] == grant["claim_epoch"]
        assert "authorization" not in json.dumps(event).lower()


def test_consumer_fixture_matches_real_resolve_sse_revocation_and_closed(host):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rig, client, body, binding, settings = host
    request = fixture["resolve"]["request"]
    actual_body = {**request["json"], "session_id": body["session_id"], "task_id": body["task_id"]}
    headers = {**request["headers"], "Authorization": "Bearer " + TOKEN}
    response = client.request(request["method"], request["path"], json=actual_body, headers=headers)
    assert response.status_code == fixture["resolve"]["response"]["status"]
    assert response.headers["cache-control"] == "no-store"
    grant = response.json()
    example = fixture["resolve"]["response"]["json"]
    assert shape(grant) == shape(example)
    assert all(grant[key] == actual_body[key] for key in actual_body)
    for key in ("schema", "profile_reference", "profile_revision", "state", "event_sequence"):
        assert grant[key] == example[key]
    descriptor = grant["attachments"][0]
    for key in (
        "schema",
        "download_policy",
        "size",
        "sha256",
        "mime_type",
        "filename",
        "declared_quality",
        "original_comparison",
        "formal_archive",
    ):
        assert descriptor[key] == example["attachments"][0][key]
    assert grant["budget_scope"] == (
        f"message:{grant['message_id']}:attachment:{descriptor['attachment_id']}"
    )
    assert descriptor["thread_id"] == grant["thread_id"]
    assert descriptor["enterprise_identity_id"] == grant["enterprise_identity_id"]

    with gateway_server(settings) as live:
        stream_headers = {
            **event_headers(actual_body, grant),
            "Last-Event-ID": str(grant["event_sequence"]),
        }
        assert set(stream_headers) == set(fixture["events"]["request"]["headers"])
        with (
            httpx.Client(base_url=live.inbound_media.public_base_url, trust_env=False) as peer,
            peer.stream("GET", PREFIX + f"/{binding.id}/events", headers=stream_headers) as stream,
        ):
            assert stream.status_code == fixture["events"]["response"]["status"]
            assert (
                stream.headers["content-type"]
                == fixture["events"]["response"]["headers"]["Content-Type"]
            )
            assert stream.headers["cache-control"] == "no-store"
            lines = stream.iter_lines()
            running = event_from(lines)
            expected_running, expected_revoked = fixture["events"]["response"]["snapshots"]
            assert shape(running) == shape(expected_running)
            assert running["event"] == "binding"
            assert running["id"] == str(grant["event_sequence"])
            assert running["data"]["state"] == "running"
            assert running["data"]["lease_valid_until"] == grant["lease_valid_until"]
            with rig.sessions() as session:
                revoke_dispatch(
                    session, binding.dispatch_id, "model_failed", claim_token="fixture-real-claim"
                )
                session.commit()
            for _ in range(10):
                revoked = event_from(lines)
                if revoked["data"]["state"] == "revoked":
                    break
            else:
                raise AssertionError("Gateway did not emit revocation")
            assert shape(revoked) == shape(expected_revoked)
            assert revoked["data"]["sequence"] == running["data"]["sequence"] + 1
            assert revoked["data"]["reason"] == expected_revoked["data"]["reason"]

    close_body = {**actual_body, "claim_epoch": grant["claim_epoch"]}
    assert shape(close_body) == shape(fixture["closed"]["request"]["json"])
    expected_closed = {**fixture["closed"]["response"]["json"], "binding_id": grant["binding_id"]}
    for _ in range(2):
        closed = client.post(PREFIX + f"/{binding.id}/closed", json=close_body, headers=headers)
        assert closed.status_code == fixture["closed"]["response"]["status"]
        assert closed.json() == expected_closed
        assert closed.headers["cache-control"] == "no-store"
