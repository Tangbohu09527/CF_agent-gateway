"""Followup policy over real HTTP, SQL claims, artifact bytes and response sealing."""

import base64
import hashlib
import json
from dataclasses import replace

import pytest
from test_artifact_return_http import (
    PDF,
    PNG,
    artifact_count,
    inspect_slot,
    upload,
)
from test_artifact_return_http import (
    rig as http_rig,
)

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.artifact import ArtifactRepository
from cf_agent_gateway.artifact.models import Artifact
from cf_agent_gateway.artifact.return_handoff import (
    ReturnDenied,
    _snapshot,
    authorize,
    issue_context,
    response_id_for,
    seal_returns,
    slot_id,
)
from cf_agent_gateway.hermes.errors import HermesResponseError
from cf_agent_gateway.hermes.models import ArtifactRefPart, HermesDispatchOutcome, ResponseEnvelope
from cf_agent_gateway.hermes.return_bridge.scope import _claims
from cf_agent_gateway.inbound import followup
from cf_agent_gateway.task.model import HermesDispatchRecord


@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    monkeypatch.setenv("CF_GATEWAY_HOST_GRANT_KEY", base64.b64encode(b"x" * 32).decode())
    yield from http_rig.__wrapped__(tmp_path, monkeypatch, request)


def constraint(content=PDF, *, kind="file", mime="application/pdf"):
    return {
        "action": "read-return",
        "attachment_id": 7,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "kind": kind,
        "mime_type": mime,
    }


def policy(monkeypatch, value):
    state = {"value": value}

    def current(session, record):
        value = state["value"]
        if isinstance(value, Exception):
            raise value
        return None if value is None else dict(value)

    monkeypatch.setattr(followup, "return_constraint", current)
    return state


def issue(rig):
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        result = issue_context(
            session,
            record,
            rig.settings.artifact_return,
            session_id=getattr(rig, "session_id", "synthetic-session"),
        )
        session.commit()
    rig.context = result
    return result


def outcome(rig, response=None):
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        return HermesDispatchOutcome(
            message_id=record.message_id,
            workspace_id=record.workspace_id,
            ai_thread_id=record.ai_thread_id,
            assistant_content="Synthetic result",
            response=response,
        )


def seal(rig, result, *, enabled=True):
    with rig.factory() as session:
        return seal_returns(
            session,
            rig.dispatch_id,
            rig.claim,
            result,
            settings=replace(rig.settings.artifact_return, enabled=enabled),
            storage_root=rig.settings.artifact.storage_root,
        )


def seed(rig, *, content=PDF, slot=0, kind="file", mime="application/pdf"):
    """A legacy/internal artifact must not bypass final sealing or receipt checks."""
    with rig.factory() as session:
        record = session.get(HermesDispatchRecord, rig.dispatch_id)
        snapshot = _snapshot(session, record, rig.settings.artifact_return)
        response_id = response_id_for(record, snapshot)
        artifact = ArtifactRepository(session, rig.settings.artifact.storage_root).create(
            artifact_id=slot_id(response_id, slot),
            response_id=response_id,
            filename="synthetic.pdf" if kind == "file" else "synthetic.png",
            kind=kind,
            mime_type=mime,
            content=content,
        )
        return response_id, artifact.artifact_id


def test_constraint_is_signed_and_windows_claim_reader_remains_compatible(rig, monkeypatch):
    expected = constraint()
    policy(monkeypatch, expected)
    issue(rig)
    claims = _claims(rig.context["authorization"], "synthetic-session")
    assert claims["original_attachment_constraint"] == expected
    with rig.factory() as session:
        record, response_id = authorize(
            session, rig.settings.artifact_return, rig.dispatch_id, rig.context["authorization"]
        )
        snapshot = {k: v for k, v in claims.items() if k not in {"schema", "session_id"}}
        assert response_id == response_id_for(record, snapshot)


def test_ordinary_snapshot_and_multiple_generated_files_are_unchanged(rig):
    token = rig.context["authorization"].split(" ", 1)[1].split(".")[0]
    claims = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert "original_attachment_constraint" not in claims
    assert upload(rig).status_code == 200
    assert (
        upload(
            rig, slot=1, content=PNG, filename="new.png", kind="image", mime="image/png"
        ).status_code
        == 200
    )
    assert len(seal(rig, outcome(rig)).response.artifact_ids) == 2


def test_read_cannot_issue_artifact_capability(rig, monkeypatch):
    policy(monkeypatch, {"action": "read"})
    with pytest.raises(ReturnDenied):
        issue(rig)
    assert upload(rig).status_code == 403
    assert inspect_slot(rig).status_code == 403
    assert artifact_count(rig) == 0


@pytest.mark.parametrize("enabled", [True, False])
def test_read_pure_text_allowed_but_structured_artifact_rejected(rig, monkeypatch, enabled):
    policy(monkeypatch, {"action": "read"})
    text = outcome(rig)
    assert seal(rig, text, enabled=enabled) is text
    response = ResponseEnvelope(
        response_id="legacy-response",
        parts=(ArtifactRefPart(artifact_id="unrelated-existing-artifact"),),
    )
    with pytest.raises(HermesResponseError):
        seal(rig, outcome(rig, response), enabled=enabled)


def test_read_legacy_current_claim_upload_is_not_silently_attached(rig, monkeypatch):
    policy(monkeypatch, {"action": "read"})
    seed(rig)
    with pytest.raises(HermesResponseError):
        seal(rig, outcome(rig))


@pytest.mark.parametrize(
    "content,kind,mime,filename",
    [
        (PDF, "file", "application/pdf", "original.pdf"),
        (PNG, "image", "image/png", "original.png"),
    ],
)
def test_exact_original_upload_retry_get_and_legacy_seal(
    rig, monkeypatch, content, kind, mime, filename
):
    policy(monkeypatch, constraint(content, kind=kind, mime=mime))
    issue(rig)
    arguments = dict(content=content, kind=kind, mime=mime, filename=filename)
    first = upload(rig, **arguments)
    assert first.status_code == 200
    assert upload(rig, **arguments).json() == first.json()
    assert inspect_slot(rig).json() == first.json()
    result = seal(rig, outcome(rig))
    assert result.response.artifact_ids == (first.json()["artifact_id"],)
    assert seal(rig, result) is result


@pytest.mark.parametrize("change", ["sha256", "size", "kind", "mime_type", "slot"])
def test_valid_file_cannot_exceed_original_constraint(rig, monkeypatch, change):
    expected = constraint()
    if change == "sha256":
        expected[change] = "a" * 64
    elif change == "size":
        expected[change] += 1
    elif change in {"kind", "mime_type"}:
        # A complete other supported format remains a valid policy, not malformed input.
        expected.update(kind="image", mime_type="image/png")
    policy(monkeypatch, expected)
    issue(rig)
    response = upload(rig, slot=1 if change == "slot" else 0)
    assert response.status_code == 403
    assert artifact_count(rig) == 0


@pytest.mark.parametrize("change", ["sha256", "size", "kind", "mime_type", "slot"])
def test_legacy_artifact_cannot_bypass_get_or_seal(rig, monkeypatch, change):
    policy(monkeypatch, constraint())
    issue(rig)
    response_id, aid = seed(rig, slot=1 if change == "slot" else 0)
    if change != "slot":
        with rig.factory() as session:
            artifact = session.get(Artifact, aid)
            setattr(
                artifact,
                change,
                {
                    "sha256": "a" * 64,
                    "size": len(PDF) + 1,
                    "kind": "image",
                    "mime_type": "image/png",
                }[change],
            )
            session.commit()
    assert inspect_slot(rig, slot=1 if change == "slot" else 0).status_code == 403
    for result in (
        outcome(rig),
        outcome(
            rig,
            ResponseEnvelope(response_id=response_id, parts=(ArtifactRefPart(artifact_id=aid),)),
        ),
    ):
        with pytest.raises(HermesResponseError):
            seal(rig, result)


@pytest.mark.parametrize("value", [None, {"action": "read"}, "revoked", "changed"])
def test_original_authorization_is_rechecked_after_issue_and_upload(rig, monkeypatch, value):
    state = policy(monkeypatch, constraint())
    issue(rig)
    assert upload(rig).status_code == 200
    state["value"] = (
        MediaFetchError("media_followup_unavailable")
        if value == "revoked"
        else {**constraint(), "attachment_id": 8}
        if value == "changed"
        else value
    )
    assert upload(rig).status_code == 403
    assert inspect_slot(rig).status_code == 403
    with pytest.raises(HermesResponseError):
        seal(rig, outcome(rig))


def test_revocation_during_disk_write_cannot_publish_ready(rig, monkeypatch):
    state = policy(monkeypatch, constraint())
    issue(rig)
    original = ArtifactRepository.create

    def create(self, **kwargs):
        artifact = original(self, **kwargs)
        state["value"] = MediaFetchError("media_followup_unavailable")
        return artifact

    monkeypatch.setattr(ArtifactRepository, "create", create)
    assert upload(rig).status_code == 403
    assert artifact_count(rig) == 0


def test_revocation_during_seal_read_cannot_complete(rig, monkeypatch):
    state = policy(monkeypatch, constraint())
    issue(rig)
    assert upload(rig).status_code == 200
    original = ArtifactRepository.read

    def read(self, artifact_id):
        result = original(self, artifact_id)
        state["value"] = MediaFetchError("media_followup_unavailable")
        return result

    monkeypatch.setattr(ArtifactRepository, "read", read)
    with pytest.raises(HermesResponseError):
        seal(rig, outcome(rig))


@pytest.mark.parametrize("value", [{"action": "read"}, constraint()])
def test_disabled_return_cannot_admit_legacy_followup_artifacts(rig, monkeypatch, value):
    policy(monkeypatch, value)
    seed(rig)
    with pytest.raises(HermesResponseError):
        seal(rig, outcome(rig), enabled=False)


@pytest.mark.parametrize(
    "value",
    [
        {"action": "return"},
        {"action": "read", "attachment_id": 7},
        {**constraint(), "attachment_id": True},
        {**constraint(), "size": 0},
        {**constraint(), "sha256": "not-a-digest"},
        {**constraint(), "kind": "file", "mime_type": "image/png"},
        {**constraint(), "extra": "not-authorized"},
    ],
)
def test_invalid_server_constraint_never_creates_capability(rig, monkeypatch, value):
    policy(monkeypatch, value)
    with pytest.raises(ReturnDenied):
        issue(rig)
    assert upload(rig).status_code == 403


def registered_followup(rig, action):
    """Real admission/worker/registry/new job; no mocked followup policy."""
    from types import SimpleNamespace

    from sqlalchemy import select
    from test_hermes_dispatch_ingestion import normalized_message
    from test_inbound_media_queue import HOST, MEDIA, MemoryStaging

    from cf_agent_gateway.adapters.wechat.inbound_media import parse_inbound_media
    from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult
    from cf_agent_gateway.inbound.host_binding import prepare_binding
    from cf_agent_gateway.inbound.models import InboundMediaJob
    from cf_agent_gateway.inbound.worker import InboundMediaWorker
    from cf_agent_gateway.ingestion import MessageAdmissionService
    from cf_agent_gateway.task.model import HermesDispatchRecordStore

    with rig.factory() as session:
        HermesDispatchRecordStore(session).mark_success(rig.dispatch_id, claim_token=rig.claim)
        media = MessageAdmissionService(
            session, inbound_media=MEDIA, v2_routing_enabled=True
        ).process(
            normalized_message(
                event_id="original-pdf",
                source_message_id="original-pdf",
                source_local_id="2",
                source_server_id="pdf-server",
                message_type="file",
                raw_type=49,
                content="",
            )
        )

    def fetch(source):
        return BoundMediaResult(
            source.fingerprint,
            parse_inbound_media(
                {"type": "file", "data": base64.b64encode(PDF).decode(), "filename": "original.pdf"}
            ),
        )

    assert (
        InboundMediaWorker(rig.factory, SimpleNamespace(fetch=fetch), MemoryStaging()).run_once()
        == "ready"
    )
    with rig.factory() as session:
        store = HermesDispatchRecordStore(session)
        original_record = store.claim_next(claim_token="original-synthetic-claim")
        assert original_record.id == media.dispatch_record_id
        store.mark_success(original_record.id, claim_token=original_record.claim_token)
        original_job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.dispatch_id == original_record.id)
        )
        attachment_id = original_job.attachment_id
        new = MessageAdmissionService(
            session, inbound_media=replace(MEDIA, followup_enabled=True), v2_routing_enabled=True
        ).process(
            normalized_message(
                event_id="new-operation",
                source_message_id="new-operation",
                source_local_id="new-local",
                source_server_id="new-server",
                content=f"/attachment {attachment_id} {action}\nSynthetic explicit instruction.",
            )
        )
        record = store.claim_next(claim_token="new-synthetic-claim", lease_seconds=3600)
        assert record.id == new.dispatch_record_id
        rig.message_id, rig.dispatch_id, rig.claim = (
            record.message_id,
            record.id,
            record.claim_token,
        )
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.dispatch_id == record.id)
        )
        binding = prepare_binding(
            session,
            job,
            settings=replace(
                HOST, profile_reference=rig.settings.artifact_return.profile_reference
            ),
            public_base_url=MEDIA.public_base_url,
            parent_session_id=None,
            profile_reference=rig.settings.artifact_return.profile_reference,
            profile_revision=rig.settings.artifact_return.profile_revision,
            expected_claim_token=record.claim_token,
        )
        rig.session_id = binding.session_id
        assert followup.return_constraint(session, record)["action"] == action
        return attachment_id, original_record.id


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_real_followup_registry_constrains_http_and_completion(rig):
    from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore
    from cf_agent_gateway.task.model.models import HermesDispatchStatus

    attachment_id, original_dispatch = registered_followup(rig, "read-return")
    issue(rig)
    claims = _claims(rig.context["authorization"], rig.session_id)
    assert claims["original_attachment_constraint"] == {
        **constraint(),
        "attachment_id": attachment_id,
    }
    assert upload(rig, content=PDF.replace(b"Catalog", b"Changed")).status_code == 403
    response = upload(rig)
    assert response.status_code == 200
    with rig.factory() as session:
        completed = HermesDispatchResponseStore(session).complete_success(
            rig.dispatch_id,
            claim_token=rig.claim,
            outcome=outcome(rig),
            artifact_return=rig.settings.artifact_return,
            artifact_storage_root=rig.settings.artifact.storage_root,
        )
        assert (
            completed.response_payload["parts"][-1]["artifact_id"] == response.json()["artifact_id"]
        )
        assert (
            session.get(HermesDispatchRecord, original_dispatch).status
            is HermesDispatchStatus.SUCCESS
        )
    assert inspect_slot(rig).status_code == 403


@pytest.mark.parametrize("rig", [True], indirect=True)
@pytest.mark.parametrize("return_enabled", [None, False])
def test_real_read_has_no_capability_and_completion_accepts_text_only(rig, return_enabled):
    from cf_agent_gateway.hermes.result_store import HermesDispatchResponseStore

    registered_followup(rig, "read")
    settings = (
        None if return_enabled is None else replace(rig.settings.artifact_return, enabled=False)
    )
    with pytest.raises(ReturnDenied):
        issue(rig)
    forged = ResponseEnvelope(
        response_id="legacy-output",
        parts=(ArtifactRefPart(artifact_id="not-an-authorized-original"),),
    )
    with rig.factory() as session, pytest.raises(HermesResponseError):
        HermesDispatchResponseStore(session).complete_success(
            rig.dispatch_id,
            claim_token=rig.claim,
            outcome=outcome(rig, forged),
            artifact_return=settings,
        )
    with rig.factory() as session:
        completed = HermesDispatchResponseStore(session).complete_success(
            rig.dispatch_id, claim_token=rig.claim, outcome=outcome(rig), artifact_return=settings
        )
        assert completed.response_payload is None


@pytest.mark.parametrize("rig", [True], indirect=True)
@pytest.mark.parametrize("change", ["profile", "session", "missing", "claim", "binding-session"])
def test_real_followup_capability_requires_current_host_intent(rig, change):
    from sqlalchemy import select

    from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding

    registered_followup(rig, "read-return")
    issue(rig)
    assert upload(rig).status_code == 200
    if change == "profile":
        rig.settings = replace(
            rig.settings,
            artifact_return=replace(
                rig.settings.artifact_return, profile_reference="other-profile"
            ),
        )
        rig.app.state.settings = rig.settings
    elif change == "session":
        rig.session_id = "another-synthetic-session"
    else:
        with rig.factory() as session:
            binding = session.scalar(
                select(InboundHostBinding).where(InboundHostBinding.dispatch_id == rig.dispatch_id)
            )
            if change == "missing":
                session.delete(binding)
            elif change == "claim":
                binding.claim_token_hash = "f" * 64
            else:
                binding.session_id = "changed-binding-session"
            session.commit()
    with pytest.raises(ReturnDenied):
        issue(rig)
    if change != "session":  # Caller typo does not mutate the original live capability.
        assert inspect_slot(rig).status_code == 403
        with pytest.raises(HermesResponseError):
            seal(rig, outcome(rig))


@pytest.mark.parametrize("rig", [True], indirect=True)
def test_completed_host_state_does_not_invalidate_original_artifact_sealing(rig):
    from sqlalchemy import select

    from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding

    registered_followup(rig, "read-return")
    issue(rig)
    assert upload(rig).status_code == 200
    with rig.factory() as session:
        binding = session.scalar(
            select(InboundHostBinding).where(InboundHostBinding.dispatch_id == rig.dispatch_id)
        )
        binding.state = "revoked"
        binding.revoke_reason = "completed"
        session.commit()
    assert len(seal(rig, outcome(rig)).response.artifact_ids) == 1
