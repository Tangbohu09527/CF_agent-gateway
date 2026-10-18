"""Real resolve route/DB: versioned server clock without renewing authority."""

from datetime import UTC, datetime, timedelta

import pytest
from test_host_binding_http import NONCE, PREFIX, TOKEN, host, owner_body, resolve, rig

from cf_agent_gateway.inbound import host_binding
from cf_agent_gateway.inbound.host_binding import RESOLVE_SCHEMA_V2, SCHEMA
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.store import aware

# Expose the real fixture dependency without reimplementing its admission path.
__all__ = ["host", "rig"]


def v2(body):
    return {**body, "schema": RESOLVE_SCHEMA_V2}


def test_v1_exact_shape_and_v2_same_binding_with_authenticated_server_time(host):
    fixture, client, body, binding, _ = host
    old = resolve(client, body)
    assert old.status_code == 200
    original = old.json()
    assert original["schema"] == SCHEMA and "server_now" not in original
    assert len(original) == 18
    response = resolve(client, v2(body))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    current = response.json()
    assert set(current) == set(original) | {"server_now"}
    stamp = datetime.fromisoformat(current.pop("server_now"))
    assert stamp.tzinfo is not None
    assert current.pop("schema") == RESOLVE_SCHEMA_V2
    assert current == {key: value for key, value in original.items() if key != "schema"}
    assert 0 < (datetime.fromisoformat(current["lease_valid_until"]) - stamp).total_seconds() <= 30
    with fixture.sessions() as session:
        persisted = session.get(InboundHostBinding, binding.id)
        assert aware(persisted.host_lease_until) == datetime.fromisoformat(
            current["lease_valid_until"]
        )


def test_repeated_v2_returns_current_sample_and_decreasing_original_lease(host, monkeypatch):
    _, client, body, _, _ = host
    first = resolve(client, v2(body)).json()
    later = datetime.fromisoformat(first["server_now"]) + timedelta(seconds=2)

    class Advanced(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return later

    monkeypatch.setattr(host_binding, "datetime", Advanced)
    second_response = resolve(client, v2(body))
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["server_now"] == later.isoformat()
    assert second["lease_valid_until"] == first["lease_valid_until"]
    assert {k: v for k, v in first.items() if k != "server_now"} == {
        k: v for k, v in second.items() if k != "server_now"
    }
    later = datetime.fromisoformat(first["lease_valid_until"]) + timedelta(microseconds=1)
    assert resolve(client, v2(body)).status_code == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "different-session"),
        ("task_id", "different-task"),
        ("host_instance_id", "different-owner"),
        ("host_nonce", "x" * len(NONCE)),
    ],
)
def test_v2_preserves_session_task_owner_nonce_fence(host, field, value):
    _, client, body, _, _ = host
    assert resolve(client, v2(body)).status_code == 200
    assert resolve(client, {**v2(body), field: value}).status_code == 403


def test_v2_rejects_wrong_service_and_unknown_schema_or_extra_clock(host):
    _, client, body, _, _ = host
    assert resolve(client, v2(body), "incorrect").status_code == 403
    for invalid in ({**body, "schema": "unknown"}, {**v2(body), "server_now": "model-input"}):
        assert resolve(client, invalid).status_code == 422


def test_v2_does_not_change_closed_protocol_or_reopen_ended_scope(host):
    _, client, body, binding, _ = host
    grant = resolve(client, v2(body)).json()
    headers = {"Authorization": "Bearer " + TOKEN}
    path = PREFIX + f"/{binding.id}/closed"
    assert client.post(path, json=owner_body(v2(body), grant), headers=headers).status_code == 422
    response = client.post(path, json=owner_body(body, grant), headers=headers)
    assert response.status_code == 200 and response.json()["schema"] == SCHEMA
    assert resolve(client, v2(body)).status_code == 403
