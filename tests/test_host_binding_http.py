"""Real Gateway auth/SQL/claims/HTTP; the plugin host is a protocol test peer."""

import asyncio
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from inbound_media_download_peer import fetch_verified
from sqlalchemy import select
from test_inbound_media_integration import gateway_server
from test_inbound_media_queue import admit, worker
from test_inbound_media_queue import rig as queue_rig

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.identity.models import EnterpriseIdentity, IdentityStatus
from cf_agent_gateway.inbound.access import read_authorized
from cf_agent_gateway.inbound.host_binding import (
    SCHEMA,
    expire_bindings,
    mark_binding_running,
    prepare_binding,
    revoke_dispatch,
    runtime_digest,
)
from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.message.models import Attachment
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore

TOKEN = "synthetic-host-service-credential-" + "x" * 32
NONCE = "synthetic-host-nonce-" + "n" * 32
PREFIX = "/internal/hermes/inbound-bindings"
rig = queue_rig


@pytest.fixture
def host(rig, monkeypatch):
    monkeypatch.setenv("TEST_HOST_SERVICE", TOKEN)
    monkeypatch.setenv("TEST_HOST_GRANT_KEY", base64.b64encode(b"k" * 32).decode())
    settings = HostBindingSettings(
        enabled=True,
        dedicated_endpoint_confirmed=True,
        host_id="fixture-host",
        profile_reference="fixture-profile",
        profile_revision=1,
        service_token_env="TEST_HOST_SERVICE",
        encryption_key_env="TEST_HOST_GRANT_KEY",
        runtime_model="fixture-model",
        runtime_provider="custom",
    )
    admit(rig)
    assert worker(rig).run_once() == "ready"
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="fixture-real-claim")
        binding = prepare_binding(
            session,
            session.get(InboundMediaJob, 1),
            settings=settings,
            public_base_url=rig.media.public_base_url,
            parent_session_id=None,
            profile_reference=settings.profile_reference,
            profile_revision=settings.profile_revision,
            expected_claim_token="fixture-real-claim",
        )
        assert binding.dispatch_id == record.id
        # This fixture is the trusted session-preparation peer, not real Hermes.
        binding.preparation_started_at = datetime.now(UTC)
        binding.history_digest = "a" * 64
        binding.runtime_config_digest = runtime_digest(settings)
        session.commit()
        mark_binding_running(session, binding)
    app_settings = Settings(
        database=DatabaseSettings(url=str(rig.engine.url)),
        inbound_media=rig.media,
        host_binding=settings,
    )
    body = {
        "schema": SCHEMA,
        "session_id": binding.session_id,
        "task_id": binding.session_id,
        "host_instance_id": "fixture-instance",
        "host_nonce": NONCE,
    }
    with TestClient(create_app(app_settings)) as client:
        yield rig, client, body, binding, app_settings


def resolve(client, body, token=TOKEN):
    return client.post(PREFIX + "/resolve", json=body, headers={"Authorization": "Bearer " + token})


def owner_body(body, grant):
    return {**body, "claim_epoch": grant["claim_epoch"]}


def event_headers(body, grant):
    return {
        "Authorization": "Bearer " + TOKEN,
        "X-CF-Session-Id": body["session_id"],
        "X-CF-Task-Id": body["task_id"],
        "X-CF-Host-Instance-Id": body["host_instance_id"],
        "X-CF-Host-Nonce": body["host_nonce"],
        "X-CF-Claim-Epoch": grant["claim_epoch"],
    }


def test_concurrent_resolve_same_grant_and_restart_do_not_refresh_budget(host, caplog):
    rig, client, body, binding, settings = host
    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(lambda _: resolve(client, body), range(6)))
    assert all(r.status_code == 200 for r in responses)
    grants = [r.json() for r in responses]
    assert all(g == grants[0] for g in grants)
    with TestClient(create_app(settings)) as restarted:
        assert resolve(restarted, body).json() == grants[0]
    descriptor = grants[0]["attachments"][0]
    with rig.sessions() as session:
        persisted = session.get(InboundHostBinding, binding.id)
        assert descriptor["authorization"].encode() not in persisted.grant_ciphertext
        assert NONCE not in persisted.host_nonce_hash
    assert descriptor["authorization"] not in caplog.text and TOKEN not in caplog.text
    assert responses[0].headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "unknown-session"),
        ("task_id", "wrong-task"),
        ("host_instance_id", "different-host-instance"),
        ("host_nonce", "z" * 40),
    ],
)
def test_unknown_old_session_or_changed_owner_denied(host, field, value):
    _, client, body, _, _ = host
    assert resolve(client, body).status_code == 200
    response = resolve(client, {**body, field: value})
    assert response.status_code == 403
    assert response.json() == {"detail": "host binding unavailable"}


def test_service_identity_profile_key_loss_registry_and_claim_changes_fail_closed(
    host, monkeypatch
):
    rig, client, body, binding, settings = host
    assert resolve(client, body, "incorrect").status_code == 403
    with TestClient(
        create_app(
            replace(settings, host_binding=replace(settings.host_binding, host_id="not-approved"))
        )
    ) as other:
        assert resolve(other, body).status_code == 403
    with TestClient(
        create_app(
            replace(
                settings,
                host_binding=replace(settings.host_binding, profile_reference="other-profile"),
            )
        )
    ) as other:
        assert resolve(other, body).status_code == 403
    monkeypatch.setenv("TEST_HOST_GRANT_KEY", base64.b64encode(b"x" * 32).decode())
    assert resolve(client, body).status_code == 403
    monkeypatch.setenv("TEST_HOST_GRANT_KEY", base64.b64encode(b"k" * 32).decode())
    assert resolve(client, body).status_code == 200
    with rig.sessions() as session:
        session.get(HermesDispatchRecord, binding.dispatch_id).claim_token = "new-real-claim"
        session.commit()
    assert resolve(client, body).status_code == 403
    with rig.sessions() as session:
        expire_bindings(session)
        assert session.get(InboundHostBinding, binding.id).grant_ciphertext is None


def test_authority_revocation_closed_idempotency_and_cipher_destruction(host):
    rig, client, body, binding, _ = host
    grant = resolve(client, body).json()
    with rig.sessions() as session:
        revoke_dispatch(
            session, binding.dispatch_id, "model_failed", claim_token="fixture-real-claim"
        )
        session.commit()
        first_sequence = session.get(InboundHostBinding, binding.id).sequence
        revoke_dispatch(
            session, binding.dispatch_id, "model_failed", claim_token="fixture-real-claim"
        )
        session.commit()
        assert session.get(InboundHostBinding, binding.id).sequence == first_sequence
        assert session.get(InboundHostBinding, binding.id).grant_ciphertext is None
        assert session.get(InboundMediaJob, 1).read_token_hash is None
    assert resolve(client, body).status_code == 403
    url = PREFIX + f"/{binding.id}/closed"
    wrong = {**owner_body(body, grant), "host_instance_id": "another-owner"}
    assert (
        client.post(url, json=wrong, headers={"Authorization": "Bearer " + TOKEN}).status_code
        == 403
    )
    for _ in range(2):
        assert (
            client.post(
                url, json=owner_body(body, grant), headers={"Authorization": "Bearer " + TOKEN}
            ).status_code
            == 200
        )


def test_expired_host_lease_never_reissues_grant(host):
    rig, client, body, binding, _ = host
    assert resolve(client, body).status_code == 200
    with rig.sessions() as session:
        session.get(InboundHostBinding, binding.id).host_lease_until = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        session.commit()
    assert resolve(client, body).status_code == 403
    with rig.sessions() as session:
        expire_bindings(session)
        persisted = session.get(InboundHostBinding, binding.id)
        assert persisted.grant_ciphertext is None and persisted.state == "revoked"


@pytest.mark.parametrize("change", ["identity", "registry", "expired_claim"])
def test_current_identity_registry_and_claim_are_rechecked(host, change):
    rig, client, body, binding, _ = host
    assert resolve(client, body).status_code == 200
    with rig.sessions() as session:
        if change == "identity":
            identity = session.scalar(select(EnterpriseIdentity))
            identity.status = IdentityStatus.DISABLED
        elif change == "registry":
            session.scalar(select(Attachment)).hash = "f" * 64
        else:
            session.get(HermesDispatchRecord, binding.dispatch_id).lease_expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        session.commit()
    assert resolve(client, body).status_code == 403


def test_concurrent_first_owner_has_exactly_one_winner(host):
    _, client, body, _, _ = host
    other = {**body, "host_instance_id": "second-instance", "host_nonce": "q" * 40}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda b: resolve(client, b), [body, other]))
    assert sorted(r.status_code for r in results) == [200, 403]


def test_real_event_disconnect_revokes_without_end_hook(host):
    rig, client, body, binding, settings = host
    grant = resolve(client, body).json()
    with gateway_server(settings) as live:
        with (
            httpx.Client(base_url=live.inbound_media.public_base_url, trust_env=False) as peer,
            peer.stream(
                "GET", PREFIX + f"/{binding.id}/events", headers=event_headers(body, grant)
            ) as stream,
        ):
            assert stream.status_code == 200
            assert next(stream.iter_lines()) == "id: 1"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with rig.sessions() as session:
                if session.get(InboundHostBinding, binding.id).state == "revoked":
                    break
            time.sleep(0.02)
        else:
            pytest.fail("disconnected host retained authority")
    assert resolve(client, body).status_code == 403


def test_live_events_kept_through_real_http_download_and_closed(host, monkeypatch):
    rig, client, body, binding, settings = host
    # Portable storage double only. Linux full integration separately uses the
    # real private filesystem; HTTP, auth, fences, events and downloader are real.
    monkeypatch.setattr(
        "cf_agent_gateway.inbound.access.InboundMediaStaging", lambda _path: rig.staging
    )
    grant = resolve(client, body).json()
    with gateway_server(settings) as live:
        origin = live.inbound_media.public_base_url
        with (
            httpx.Client(base_url=origin, trust_env=False) as peer,
            peer.stream(
                "GET", PREFIX + f"/{binding.id}/events", headers=event_headers(body, grant)
            ) as stream,
        ):
            assert stream.status_code == 200
            event_lines = stream.iter_lines()
            for line in event_lines:
                if line.startswith("data: "):
                    assert json.loads(line[6:])["state"] == "running"
                    break
            descriptor = dict(grant["attachments"][0])
            descriptor["url"] = origin + "/inbound-media/1/content"
            assert asyncio.run(fetch_verified(descriptor))
            assert (
                peer.post(
                    PREFIX + f"/{binding.id}/closed",
                    json=owner_body(body, grant),
                    headers={"Authorization": "Bearer " + TOKEN},
                ).status_code
                == 200
            )
            assert (
                peer.get(
                    descriptor["url"], headers={"Authorization": descriptor["authorization"]}
                ).status_code
                == 403
            )


def test_real_gateway_events_read_gate_disconnect_and_sequence_gap(host):
    rig, client, body, binding, settings = host
    grant = resolve(client, body).json()
    descriptor = grant["attachments"][0]
    with rig.sessions() as session, pytest.raises(MediaFetchError):
        read_authorized(session, 1, descriptor["authorization"], rig.staging)
    with gateway_server(settings) as live:
        origin = live.inbound_media.public_base_url
        with (
            httpx.Client(base_url=origin, trust_env=False) as peer,
            peer.stream(
                "GET", PREFIX + f"/{binding.id}/events", headers=event_headers(body, grant)
            ) as stream,
        ):
            assert stream.status_code == 200
            lines = stream.iter_lines()
            assert next(lines) == "id: 1"
            assert next(lines) == "event: binding"
            event = json.loads(next(lines)[6:])
            assert event["state"] == "running" and "attachments" not in event
            with rig.sessions() as session:
                assert read_authorized(session, 1, descriptor["authorization"], rig.staging)[0]
            # A sequence gap on the real authenticated endpoint revokes this scope.
            wrong = {**event_headers(body, grant), "Last-Event-ID": "900"}
            assert peer.get(PREFIX + f"/{binding.id}/events", headers=wrong).status_code == 403
            for line in lines:
                if line.startswith("data: ") and json.loads(line[6:])["state"] == "revoked":
                    break
            else:
                pytest.fail("authoritative revocation event not observed")
    assert resolve(client, body).status_code == 403
    with rig.sessions() as session, pytest.raises(MediaFetchError):
        read_authorized(session, 1, descriptor["authorization"], rig.staging)
