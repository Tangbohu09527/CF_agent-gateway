"""Real ASGI HTTP/authentication/claims/SQLite; synthetic bytes and credentials only."""

from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_hermes_dispatch_ingestion import allow_sender, normalized_message

from cf_agent_gateway.agent_profile import AgentProfileStatus, AgentProfileStore
from cf_agent_gateway.artifact import ArtifactRepository, ArtifactStatus
from cf_agent_gateway.artifact.models import Artifact
from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
from cf_agent_gateway.artifact.return_handoff import issue_context
from cf_agent_gateway.config import ArtifactSettings, DatabaseSettings, Settings
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import AIThread
from cf_agent_gateway.workspace.thread_keys import build_v2_thread_key

PDF = b"%PDF-1.7\n1 0 obj <</Type /Catalog>> endobj\ntrailer <</Root 1 0 R>>\n%%EOF\n"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII="
)
SIGNING_KEY = "synthetic-artifact-return-http-signing-key-" + "x" * 32
KEY_ENV = "TEST_ARTIFACT_RETURN_HTTP_KEY"


@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    monkeypatch.setenv(KEY_ENV, SIGNING_KEY)
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+pysqlite:///{tmp_path / 'http.db'}"),
        artifact=ArtifactSettings(storage_root=str(tmp_path / "private-artifacts")),
        artifact_return=ArtifactReturnSettings(
            enabled=True,
            host_contract_confirmed=True,
            public_base_url="https://gateway.invalid",
            profile_reference="synthetic-profile",
            signing_key_env=KEY_ENV,
        ),
    )
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        factory = app.state.database_session_factory
        with factory() as session:
            allow_sender(session)
            v2 = getattr(request, "param", False)
            if v2:
                profile_store = AgentProfileStore(session)
                profile, _ = profile_store.create_agent_profile(
                    profile_key="return-http",
                    revision=1,
                    provider="synthetic",
                    external_profile_ref="synthetic-profile",
                    model="synthetic-model",
                )
                conversation = Conversation(
                    source="wechat",
                    source_account_id="wxid-gateway",
                    conversation_id="wxid-alice",
                    conversation_type="private",
                )
                session.add(conversation)
                session.commit()
                profile_store.bind_conversation_agent_profile(
                    conversation_record_id=conversation.id, agent_profile_id=profile.id
                )
            admitted = MessageAdmissionService(session, v2_routing_enabled=v2).process(
                normalized_message(content="把指定 PDF 发回当前聊天。")
            )
            assert admitted.admission.admitted
            record = HermesDispatchRecordStore(session).claim_next(
                claim_token="synthetic-http-current-claim", lease_seconds=3600
            )
            assert record is not None and record.id == admitted.dispatch_record_id
            context = issue_context(
                session, record, settings.artifact_return, session_id="synthetic-session"
            )
            session.commit()
            yield_data = {
                "dispatch_id": record.id,
                "message_id": record.message_id,
                "claim": record.claim_token,
            }
        yield SimpleNamespace(
            client=client,
            app=app,
            factory=factory,
            settings=settings,
            context=context,
            root=tmp_path,
            **yield_data,
        )


def path(rig, slot=0, dispatch_id=None):
    dispatch_id = rig.dispatch_id if dispatch_id is None else dispatch_id
    return f"/internal/hermes/returns/{dispatch_id}/artifacts/{slot}"


def headers(rig, content=PDF, *, filename="资料.pdf", kind="file", mime="application/pdf"):
    return {
        "Authorization": rig.context["authorization"],
        "X-CF-Return-Intent": "current-chat",
        "X-CF-Filename": quote(filename, safe=""),
        "X-CF-Artifact-Kind": kind,
        "Content-Type": mime,
        "Content-Length": str(len(content)),
        "X-CF-Content-SHA256": hashlib.sha256(content).hexdigest(),
    }


def upload(rig, *, content=PDF, slot=0, header_changes=None, **metadata):
    values = headers(rig, content, **metadata)
    values.update(header_changes or {})
    return rig.client.put(path(rig, slot), content=content, headers=values)


def inspect_slot(rig, *, slot=0):
    return rig.client.get(path(rig, slot), headers={"Authorization": rig.context["authorization"]})


def artifact_count(rig):
    with rig.factory() as session:
        return session.scalar(select(func.count()).select_from(Artifact))


def test_disabled_and_unapproved_authentication_are_non_disclosing(rig, caplog):
    original = rig.app.state.settings
    rig.app.state.settings = replace(original, artifact_return=ArtifactReturnSettings())
    assert upload(rig).status_code == 404
    rig.app.state.settings = original
    denied = [
        upload(rig, header_changes={"Authorization": value})
        for value in ("", "Bearer arbitrary-token", "Bearer " + SIGNING_KEY)
    ]
    assert {r.status_code for r in denied} == {403}
    assert all(r.json() == {"detail": "artifact return unavailable"} for r in denied)
    assert all(r.headers["cache-control"] == "no-store" for r in denied)
    assert artifact_count(rig) == 0
    assert SIGNING_KEY not in caplog.text
    assert rig.context["authorization"] not in caplog.text


def test_unknown_or_other_dispatch_cannot_use_capability(rig):
    with rig.factory() as session:
        other = MessageAdmissionService(session).process(
            normalized_message(
                event_id="wechat:other-event",
                source_message_id="other-message",
                source_local_id="other-local",
                source_server_id="other-server",
                conversation_id="other-conversation",
                content="另一任务",
            )
        )
        assert other.dispatch_record_id != rig.dispatch_id
    for dispatch_id in (other.dispatch_record_id, rig.dispatch_id + 999):
        response = rig.client.put(
            path(rig, dispatch_id=dispatch_id), content=PDF, headers=headers(rig)
        )
        assert response.status_code == 403
    assert artifact_count(rig) == 0


@pytest.mark.parametrize(
    "change", ["claim", "lease", "completed", "account", "chat", "mapping", "identity"]
)
def test_current_claim_and_identity_changes_reject_old_capability(rig, change):
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        message = session.get(Message, rig.message_id)
        if change == "claim":
            record.claim_token = "synthetic-replacement-claim"
        elif change == "lease":
            record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "completed":
            HermesDispatchRecordStore(session).mark_success(record.id, claim_token=rig.claim)
        elif change == "account":
            # Only this isolated source snapshot changes; no real account is accessed.
            session.add(
                Conversation(
                    source=message.source,
                    source_account_id="different-account",
                    conversation_id=message.conversation_id,
                    conversation_type=message.conversation_type,
                )
            )
            session.flush()
            message.source_account_id = "different-account"
        elif change == "chat":
            session.add(
                Conversation(
                    source=message.source,
                    source_account_id=message.source_account_id,
                    conversation_id="different-chat",
                    conversation_type=message.conversation_type,
                )
            )
            session.flush()
            message.conversation_id = "different-chat"
        elif change == "mapping":
            mapping = session.scalar(select(SourceIdentityMapping))
            mapping.enabled = False
        else:
            identity = session.get(EnterpriseIdentity, record.enterprise_identity_id)
            identity.status = IdentityStatus.DISABLED
        session.commit()
    assert upload(rig).status_code == 403
    assert inspect_slot(rig).status_code == 403
    assert artifact_count(rig) == 0


@pytest.mark.parametrize(
    "updates", [{"profile_reference": "different-profile"}, {"profile_revision": 2}]
)
def test_profile_configuration_change_invalidates_capability(rig, updates):
    rig.app.state.settings = replace(
        rig.settings, artifact_return=replace(rig.settings.artifact_return, **updates)
    )
    assert upload(rig).status_code == 403
    assert artifact_count(rig) == 0


@pytest.mark.parametrize("rig", [True], indirect=True)
@pytest.mark.parametrize("change", ["disabled", "replacement_revision"])
def test_actual_v2_profile_change_rejects_old_capability(rig, change):
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        thread = session.get(AIThread, record.ai_thread_id)
        assert thread.agent_profile_id is not None
        profiles = AgentProfileStore(session)
        if change == "disabled":
            profiles.set_agent_profile_status(thread.agent_profile_id, AgentProfileStatus.DISABLED)
        else:
            profile, _ = profiles.create_agent_profile(
                profile_key="return-http",
                revision=2,
                provider="synthetic",
                external_profile_ref="synthetic-profile",
                model="synthetic-model",
            )
            message = session.get(Message, record.message_id)
            thread.agent_profile_id = profile.id
            thread.thread_key = build_v2_thread_key(
                platform=message.source,
                account_id=message.source_account_id,
                physical_conversation_id=message.conversation_id,
                conversation_type=message.conversation_type,
                sender_identity_id=record.enterprise_identity_id,
                agent_profile_id=profile.id,
                agent_profile_revision=profile.revision,
                thread_policy=thread.thread_policy,
            )
            session.commit()
    assert upload(rig).status_code == 403
    assert inspect_slot(rig).status_code == 403
    assert artifact_count(rig) == 0


def test_reissuing_context_after_restart_does_not_refresh_expiry_or_token(rig, monkeypatch):
    future = datetime.now(UTC) + timedelta(seconds=30)

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future if tz is not None else future.replace(tzinfo=None)

    monkeypatch.setattr("cf_agent_gateway.artifact.return_handoff.datetime", FutureDatetime)
    with (
        TestClient(create_app(rig.settings)) as restarted,
        restarted.app.state.database_session_factory() as session,
    ):
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        repeated = issue_context(
            session, record, rig.settings.artifact_return, session_id="synthetic-session"
        )
        session.commit()
    assert repeated == rig.context


def test_expired_capability_rejected_even_while_dispatch_lease_is_live(rig, monkeypatch):
    future = datetime.now(UTC) + timedelta(seconds=901)

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future if tz is not None else future.replace(tzinfo=None)

    monkeypatch.setattr("cf_agent_gateway.artifact.return_handoff.datetime", FutureDatetime)
    assert upload(rig).status_code == 403
    assert inspect_slot(rig).status_code == 403
    assert artifact_count(rig) == 0


def test_same_slot_replay_concurrency_and_restart_preserve_one_artifact(rig):
    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(lambda _: upload(rig), range(6)))
    assert [r.status_code for r in responses] == [200] * 6
    receipt = responses[0].json()
    assert all(r.json() == receipt for r in responses)
    assert receipt["filename"] == "资料.pdf"
    assert receipt["size"] == len(PDF)
    assert receipt["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert artifact_count(rig) == 1
    assert inspect_slot(rig).json() == receipt
    with TestClient(create_app(rig.settings)) as restarted:
        replay = restarted.put(path(rig), content=PDF, headers=headers(rig))
        assert replay.status_code == 200 and replay.json() == receipt
    with rig.factory() as session:
        artifact = session.get(Artifact, receipt["artifact_id"])
        assert artifact.status is ArtifactStatus.READY
        assert (
            ArtifactRepository(session, rig.settings.artifact.storage_root).read(
                artifact.artifact_id
            )
            == PDF
        )


@pytest.mark.parametrize("variant", ["filename", "bytes", "image"])
def test_slot_replay_rejects_changed_metadata_or_content(rig, variant):
    original = upload(rig)
    assert original.status_code == 200
    if variant == "filename":
        conflict = upload(rig, filename="renamed.pdf")
    elif variant == "bytes":
        conflict = upload(rig, content=PDF.replace(b"1.7", b"1.6"))
    else:
        conflict = upload(rig, content=PNG, filename="image.png", kind="image", mime="image/png")
    assert conflict.status_code == 409
    assert inspect_slot(rig).json() == original.json()
    assert artifact_count(rig) == 1


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.pdf",
        "C:\\secret.pdf",
        "https://host/file.pdf",
        "CON.pdf",
        "LPT1.pdf",
        "hidden\u202e.pdf",
        "文" * 90 + ".pdf",
    ],
)
def test_paths_and_urls_are_not_accepted_as_artifact_names(rig, filename):
    assert upload(rig, filename=filename).status_code == 422
    assert artifact_count(rig) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"X-CF-Return-Intent": "download-only"},
        {"X-CF-Content-SHA256": "0" * 64},
        {"Content-Length": str(len(PDF) + 1)},
        {"Content-Type": "image/png"},
        {"X-CF-Artifact-Kind": "image"},
        {"X-CF-Filename": "report.txt"},
    ],
)
def test_bad_digest_size_type_or_implicit_return_never_creates_artifact(rig, change):
    assert upload(rig, header_changes=change).status_code == 422
    assert artifact_count(rig) == 0


@pytest.mark.parametrize("content", [b"not a pdf", b"%PDF-1.7 truncated", b""])
def test_invalid_magic_or_incomplete_content_rejected(rig, content):
    assert upload(rig, content=content).status_code == 422
    assert artifact_count(rig) == 0


@pytest.mark.parametrize("slot", [-1, 4, "not-a-slot"])
def test_out_of_range_or_invalid_slot_rejected(rig, slot):
    assert upload(rig, slot=slot).status_code == 422
    assert artifact_count(rig) == 0


def test_over_limit_and_arbitrary_url_query_rejected(rig):
    content = b"%PDF-1.7\n" + b"x" * 1_048_576 + b"\n%%EOF"
    assert upload(rig, content=content).status_code == 413
    response = rig.client.put(
        path(rig) + "?url=https://untrusted.invalid/file.pdf", content=PDF, headers=headers(rig)
    )
    assert response.status_code == 422
    assert artifact_count(rig) == 0


def test_damaged_ready_artifact_is_not_retriable_or_reacknowledged(rig):
    response = upload(rig)
    assert response.status_code == 200
    with rig.factory() as session:
        artifact = session.get(Artifact, response.json()["artifact_id"])
        stored = rig.root / "private-artifacts" / artifact.storage_key
        stored.write_bytes(b"corrupt")
    for denied in (upload(rig), inspect_slot(rig)):
        assert denied.status_code == 409
        assert "retry-after" not in denied.headers
    assert artifact_count(rig) == 1


def test_get_expiring_during_read_is_denied(rig, monkeypatch):
    import cf_agent_gateway.artifact.return_handoff as handoff

    assert upload(rig).status_code == 200
    original_read = ArtifactRepository.read
    later = datetime.now(UTC) + timedelta(seconds=901)

    class LaterClock:
        @staticmethod
        def now(tz):
            return later

    def read_then_expire(self, artifact_id):
        result = original_read(self, artifact_id)
        monkeypatch.setattr(handoff, "datetime", LaterClock)
        return result

    monkeypatch.setattr(ArtifactRepository, "read", read_then_expire)
    assert inspect_slot(rig).status_code == 403


@pytest.mark.parametrize(
    "failure", ["before_create", "after_publish", "commit_before", "commit_after", "replace"]
)
def test_partial_failures_recover_same_slot_without_duplicate_artifacts(rig, monkeypatch, failure):
    original_create = ArtifactRepository.create
    original_commit = Session.commit
    injected = False

    def fail_create(self, **kwargs):
        nonlocal injected
        if injected:
            return original_create(self, **kwargs)
        injected = True
        if failure == "after_publish":
            original_create(self, **kwargs)
        raise OperationalError("synthetic failure", {}, RuntimeError("controlled"))

    def fail_commit(self):
        nonlocal injected
        if not injected and self.scalar(select(func.count()).select_from(Artifact)):
            injected = True
            if failure == "commit_after":
                original_commit(self)
            raise OperationalError("synthetic failure", {}, RuntimeError("controlled"))
        return original_commit(self)

    def fail_replace(source, destination):
        nonlocal injected
        injected = True
        raise OSError("synthetic publication failure")

    with monkeypatch.context() as faults:
        if failure in {"before_create", "after_publish"}:
            faults.setattr(ArtifactRepository, "create", fail_create)
        elif failure in {"commit_before", "commit_after"}:
            faults.setattr(Session, "commit", fail_commit)
        else:
            faults.setattr("cf_agent_gateway.artifact.repository.os.replace", fail_replace)
        failed = upload(rig)
    assert injected
    assert failed.status_code == 503
    assert failed.headers["retry-after"] == "1"
    assert artifact_count(rig) == (1 if failure == "commit_after" else 0)
    inspected = inspect_slot(rig)
    assert inspected.status_code == (200 if failure == "commit_after" else 404)
    recovered = upload(rig)
    assert recovered.status_code == 200
    assert artifact_count(rig) == 1
    assert inspect_slot(rig).json() == recovered.json()
    if failure == "commit_after":
        assert inspected.json() == recovered.json()


@pytest.mark.parametrize(
    "updates",
    [
        {"public_base_url": "http://untrusted.invalid"},
        {"public_base_url": "https://gateway.invalid/path"},
        {"max_bytes": 1_048_577},
        {"max_artifacts": 9},
        {"ttl_seconds": 3601},
        {"host_contract_confirmed": False},
    ],
)
def test_configuration_rejects_unbounded_or_unverified_handoff(updates):
    values = dict(
        enabled=True,
        host_contract_confirmed=True,
        public_base_url="https://gateway.invalid",
        profile_reference="test-profile",
    )
    values.update(updates)
    with pytest.raises(ValueError):
        ArtifactReturnSettings(**values)
