from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import test_inbound_media_queue as queue_tests
from sqlalchemy import func, select
from test_hermes_dispatch_ingestion import normalized_message
from test_inbound_media_queue import HOST, MEDIA, admit, worker

from cf_agent_gateway.access.policy_models import GatewayAccessPolicy, UserAccessPolicy
from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.identity.models import SourceIdentityMapping
from cf_agent_gateway.inbound.access import grant_read, read_authorized
from cf_agent_gateway.inbound.followup import verify_available
from cf_agent_gateway.inbound.host_binding import prepare_binding
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.message.models import Attachment, Message
from cf_agent_gateway.task.model import HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import AIThread, ThreadStatus

rig = queue_tests.rig


def followup(rig, content, *, index=2, enabled=True, **overrides):
    message = normalized_message(
        event_id=f"followup:{index}",
        source_message_id=f"f{index}",
        source_local_id=f"f{index}",
        source_server_id=f"f{index}",
        content=content,
        **overrides,
    )
    with rig.sessions() as session:
        return MessageAdmissionService(
            session, inbound_media=replace(MEDIA, followup_enabled=enabled)
        ).process(message)


def original(rig):
    result = admit(rig)
    assert worker(rig).run_once() == "ready"
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        return result, job.attachment_id, job.id


def test_explicit_new_message_gets_new_job_preserving_original(rig):
    prior, aid, jid = original(rig)
    new = followup(rig, f"/attachment {aid} read\nDescribe this file.")
    with rig.sessions() as session:
        old = session.get(InboundMediaJob, jid)
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == new.message_id)
        )
        assert job is not None and job.state == "ready"
        assert job.id != old.id and job.dispatch_id != old.dispatch_id
        assert job.source_job_id == old.id and job.followup_action == "read"
        assert job.attachment_id == old.attachment_id == aid
        assert job.read_token_hash is None and old.read_token_hash is None
        assert session.get(Attachment, aid).message_id == prior.message_id
        assert job.manifest == old.manifest


def test_default_disabled_and_natural_reference_issue_no_media_job(rig):
    _, aid, _ = original(rig)
    results = [
        followup(rig, f"/attachment {aid} read", enabled=False),
        followup(rig, "Read the previous one", index=3),
    ]
    with rig.sessions() as session:
        for result in results:
            assert (
                session.scalar(
                    select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
                )
                is None
            )


@pytest.mark.parametrize(
    "text",
    [
        "/attachment latest read",
        "/attachment 1,2 read",
        "/attachment quoted read",
        "/attachment 999 read",
        "/attachment 1 execute",
    ],
)
def test_unresolved_explicit_reference_is_durable_clarification_without_grant(rig, text):
    original(rig)
    result = followup(rig, text)
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported"
        assert job.last_error_code == "media_followup_reference_required"
        assert job.attachment_id is None and job.read_token_hash is None


def test_duplicate_operation_message_creates_only_one_new_job(rig):
    _, aid, _ = original(rig)
    first = followup(rig, f"/attachment {aid} read")
    second = followup(rig, f"/attachment {aid} read")
    assert first.message_id == second.message_id
    assert first.dispatch_record_id == second.dispatch_record_id
    with rig.sessions() as session:
        assert session.scalar(select(func.count()).select_from(InboundMediaJob)) == 2


def claim_followup(rig, *, old_dead=False):
    prior, aid, jid = original(rig)
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        old_record = store.claim_next(claim_token="old-original-claim")
        old = session.get(InboundMediaJob, jid)
        binding = prepare_binding(
            session,
            old,
            settings=HOST,
            public_base_url=MEDIA.public_base_url,
            parent_session_id=None,
            profile_reference=HOST.profile_reference,
            profile_revision=HOST.profile_revision,
            expected_claim_token=old_record.claim_token,
        )
        old_cipher = binding.grant_ciphertext
        old_token = old.read_token_hash
        if old_dead:
            store.mark_dead(
                old_record.id,
                claim_token=old_record.claim_token,
                error_code="synthetic_old_failure",
            )
        else:
            store.mark_success(old_record.id, claim_token=old_record.claim_token)
    result = followup(rig, f"/attachment {aid} read\nRead the registered original.")
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="new-operation-claim")
        assert record.id == result.dispatch_record_id
        new = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.dispatch_id == record.id)
        )
        return new.id, jid, old_cipher, old_token


def test_quote_resolves_exact_upstream_reference_and_not_quoted_text(rig):
    _, aid, jid = original(rig)
    result = followup(
        rig,
        "/attachment quoted read",
        raw_type=49,
        message_type="reply",
        reply={
            "server_id": "1",
            "local_id": "1",
            "sender_id": "wxid-alice",
            "content": "Untrusted quoted text cannot select a different attachment.",
        },
    )
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.source_job_id == jid and job.attachment_id == aid


@pytest.mark.parametrize(
    "reply",
    [
        {"server_id": "1", "local_id": "different"},
        {"server_id": "1", "sender_id": "other"},
        {"content": "attachment 1"},
    ],
)
def test_ambiguous_or_conflicting_quote_has_no_grant(rig, reply):
    original(rig)
    result = followup(rig, "/attachment quoted read", reply=reply)
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported" and job.attachment_id is None


def test_explicit_id_in_another_chat_does_not_authorize_registered_bytes(rig):
    _, aid, _ = original(rig)
    result = followup(rig, f"/attachment {aid} read", conversation_id="other-private-chat")
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported" and job.source_job_id is None


def test_new_claim_grant_survives_new_session_but_never_reuses_original_capability(rig):
    jid, old_id, old_cipher, old_token = claim_followup(rig)
    with rig.sessions() as session:
        job = session.get(InboundMediaJob, jid)
        verify_available(session, job, staging_root=MEDIA.staging_root, staging=rig.staging)
        descriptor = grant_read(session, job, public_base_url=MEDIA.public_base_url)
        assert descriptor["message_id"] == job.message_id
        assert descriptor["attachment_id"] == session.get(InboundMediaJob, old_id).attachment_id
        session.commit()
    with rig.sessions() as restarted:
        data, _ = read_authorized(restarted, jid, descriptor["authorization"], rig.staging)
        assert data
        old = restarted.get(InboundMediaJob, old_id)
        assert old.read_token_hash == old_token
        assert (
            restarted.scalar(
                select(InboundHostBinding).where(InboundHostBinding.job_id == old_id)
            ).grant_ciphertext
            == old_cipher
        )
        with pytest.raises(MediaFetchError):
            read_authorized(restarted, old_id, descriptor["authorization"], rig.staging)


@pytest.mark.parametrize(
    "change",
    [
        "mapping",
        "user_policy",
        "gateway_policy",
        "thread",
        "deadline",
        "source_content",
        "operation_content",
        "registry",
        "missing",
        "corrupt",
    ],
)
def test_each_access_rechecks_revocation_expiry_and_original_bytes(rig, change):
    jid, old_id, _, _ = claim_followup(rig)
    with rig.sessions() as session:
        descriptor = grant_read(
            session, session.get(InboundMediaJob, jid), public_base_url=MEDIA.public_base_url
        )
        session.commit()
    with rig.sessions() as session:
        job = session.get(InboundMediaJob, jid)
        old = session.get(InboundMediaJob, old_id)
        if change == "mapping":
            session.scalar(select(SourceIdentityMapping)).enabled = False
        elif change == "user_policy":
            session.scalar(select(UserAccessPolicy)).enabled = False
        elif change == "gateway_policy":
            session.scalar(select(GatewayAccessPolicy)).enabled = False
        elif change == "thread":
            session.scalar(select(AIThread)).status = ThreadStatus.ARCHIVED
        elif change == "deadline":
            job.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "source_content":
            session.get(Message, old.message_id).content = "changed source"
        elif change == "operation_content":
            session.get(Message, job.message_id).content = "/attachment 1 read-return"
        elif change == "registry":
            session.get(Attachment, old.attachment_id).hash = "0" * 64
        elif change == "missing":
            rig.staging.blobs.clear()
        elif change == "corrupt":
            rig.staging.blobs[old.manifest["reference"]] = b"changed"
        session.commit()
    with rig.sessions() as session, pytest.raises(MediaFetchError):
        read_authorized(session, jid, descriptor["authorization"], rig.staging)


def test_actual_original_profile_evidence_rejects_new_profile_before_new_grant(rig):
    jid, _, _, _ = claim_followup(rig)
    with rig.sessions() as session:
        job = session.get(InboundMediaJob, jid)
        changed = replace(HOST, profile_reference="profiles/different/1")
        with pytest.raises(MediaFetchError):
            prepare_binding(
                session,
                job,
                settings=changed,
                public_base_url=MEDIA.public_base_url,
                parent_session_id="old-managed-tip",
                profile_reference=changed.profile_reference,
                profile_revision=changed.profile_revision,
                expected_claim_token="new-operation-claim",
            )
        assert job.read_token_hash is None
        assert (
            session.scalar(select(InboundHostBinding).where(InboundHostBinding.job_id == jid))
            is None
        )


def test_repeated_and_competing_prepare_reuses_only_same_new_intent(rig):
    jid, _, _, _ = claim_followup(rig)

    def prepare(parent):
        with rig.sessions() as session:
            binding = prepare_binding(
                session,
                session.get(InboundMediaJob, jid),
                settings=HOST,
                public_base_url=MEDIA.public_base_url,
                parent_session_id=parent,
                profile_reference=HOST.profile_reference,
                profile_revision=HOST.profile_revision,
                expected_claim_token="new-operation-claim",
            )
            return binding.id, binding.grant_ciphertext

    first = prepare("current-managed-tip")
    assert prepare("current-managed-tip") == first
    from cf_agent_gateway.inbound.host_binding import HostBindingError

    with pytest.raises(HostBindingError):
        prepare("other-session")
    with rig.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(InboundHostBinding)
                .where(InboundHostBinding.job_id == jid)
            )
            == 1
        )


def test_concurrent_duplicate_ingestion_is_one_operation(rig):
    _, aid, _ = original(rig)
    from cf_agent_gateway.admission.errors import AdmissionPendingError

    def submit(_):
        try:
            return followup(rig, f"/attachment {aid} read")
        except AdmissionPendingError:
            # Existing admission CAS may report the other consumer's live claim.
            # It must not create a second operation; normal redelivery resolves it.
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [result for result in pool.map(submit, range(2)) if result is not None]
    assert results
    repeated = followup(rig, f"/attachment {aid} read")
    assert {result.dispatch_record_id for result in results} == {repeated.dispatch_record_id}
    assert len({result.message_id for result in results}) == 1
    with rig.sessions() as session:
        assert session.scalar(select(func.count()).select_from(InboundMediaJob)) == 2


def test_followup_settings_are_explicit_boolean_and_require_intake():
    from cf_agent_gateway.config import InboundMediaSettings

    assert InboundMediaSettings().followup_enabled is False
    with pytest.raises(ValueError):
        InboundMediaSettings(followup_enabled=True)
    with pytest.raises(ValueError):
        replace(MEDIA, followup_enabled=1)


def test_migration_preserves_registered_originals_and_refuses_followup_loss(rig):
    from alembic import command
    from sqlalchemy import inspect, text
    from test_migrations import migration_config

    prior, aid, jid = original(rig)
    with rig.sessions() as session:
        store = HermesDispatchRecordStore(session)
        record = store.claim_next(claim_token="migration-original")
        prepare_binding(
            session,
            session.get(InboundMediaJob, jid),
            settings=HOST,
            public_base_url=MEDIA.public_base_url,
            parent_session_id=None,
            profile_reference=HOST.profile_reference,
            profile_revision=HOST.profile_revision,
            expected_claim_token=record.claim_token,
        )
        store.mark_success(record.id, claim_token=record.claim_token)
    with rig.engine.connect() as connection:
        original_binding = dict(
            connection.execute(text("SELECT * FROM inbound_host_bindings")).mappings().one()
        )
        original_attachment = dict(
            connection.execute(text("SELECT * FROM attachments")).mappings().one()
        )
    command.downgrade(migration_config(str(rig.engine.url)), "20261016_01")
    assert "source_job_id" not in {
        column["name"] for column in inspect(rig.engine).get_columns("inbound_media_jobs")
    }
    command.upgrade(migration_config(str(rig.engine.url)), "head")
    with rig.engine.connect() as connection:
        assert (
            dict(connection.execute(text("SELECT * FROM attachments")).mappings().one())
            == original_attachment
        )
        assert connection.scalar(text("SELECT source_job_id FROM inbound_media_jobs")) is None
        assert (
            dict(connection.execute(text("SELECT * FROM inbound_host_bindings")).mappings().one())
            == original_binding
        )
    followup(rig, f"/attachment {aid} read")
    with pytest.raises(RuntimeError, match="cannot discard persisted attachment follow-up"):
        command.downgrade(migration_config(str(rig.engine.url)), "20261016_01")
    with rig.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261018_01"
        assert connection.scalar(text("SELECT COUNT(*) FROM inbound_media_jobs")) == 2
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []


def test_single_followup_cannot_point_to_another_followup_job(rig):
    jid, _, _, _ = claim_followup(rig)
    with rig.sessions() as session:
        operation = session.get(InboundMediaJob, jid)
        # A quote to the operation text is not a quote to an original attachment.
        source = session.get(Message, operation.message_id).source_server_id
    result = followup(rig, "/attachment quoted read", index=3, reply={"server_id": source})
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported" and job.source_job_id is None


def test_deadline_revalidated_after_storage_read(rig):
    jid, _, _, _ = claim_followup(rig)
    with rig.sessions() as session:
        descriptor = grant_read(
            session, session.get(InboundMediaJob, jid), public_base_url=MEDIA.public_base_url
        )
        session.commit()

    class ExpiringStorage:
        def read(self, *args, **kwargs):
            data = rig.staging.read(*args, **kwargs)
            with rig.sessions() as session:
                session.get(InboundMediaJob, jid).deadline_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )
                session.commit()
            return data

    with rig.sessions() as session, pytest.raises(MediaFetchError):
        read_authorized(session, jid, descriptor["authorization"], ExpiringStorage())


def test_group_command_never_creates_attachment_authority(rig):
    _, aid, _ = original(rig)
    result = followup(
        rig,
        f"/attachment {aid} read",
        conversation_id="synthetic-group",
        conversation_type="group",
        is_mentioned=True,
    )
    assert result.dispatch_record_id is not None
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported" and job.attachment_id is None


def test_return_only_is_not_claimed_without_a_no_parse_filebridge_contract(rig):
    _, aid, _ = original(rig)
    result = followup(rig, f"/attachment {aid} return")
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported" and job.read_token_hash is None


def test_duplicate_local_quote_id_does_not_select_latest(rig):
    original(rig)
    incoming = normalized_message(
        event_id="second-original",
        source_message_id="second-server",
        source_local_id="1",
        source_server_id="second-server",
        message_type="image",
        raw_type=3,
        content="",
    )
    with rig.sessions() as session:
        MessageAdmissionService(session, inbound_media=MEDIA).process(incoming)
    rig.now[0] = datetime.now(UTC)
    assert worker(rig).run_once() == "ready"
    result = followup(rig, "/attachment quoted read", index=4, reply={"local_id": "1"})
    with rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == result.message_id)
        )
        assert job.state == "unsupported" and job.attachment_id is None


def test_old_dead_dispatch_is_immutable_when_new_operation_gets_new_capability(rig):
    from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchStatus

    jid, old_id, old_cipher, old_token = claim_followup(rig, old_dead=True)
    with rig.sessions() as session:
        old = session.get(InboundMediaJob, old_id)
        record = session.get(HermesDispatchRecord, old.dispatch_id)
        assert record.status is HermesDispatchStatus.DEAD
        before = {column.name: getattr(record, column.name) for column in record.__table__.columns}
        new = session.get(InboundMediaJob, jid)
        descriptor = grant_read(session, new, public_base_url=MEDIA.public_base_url)
        assert new.dispatch_id != old.dispatch_id and new.read_token_hash != old_token
        session.commit()
    with rig.sessions() as session:
        assert read_authorized(session, jid, descriptor["authorization"], rig.staging)[0]
        old_record = session.get(HermesDispatchRecord, before["id"])
        assert {
            column.name: getattr(old_record, column.name) for column in old_record.__table__.columns
        } == before
        assert session.get(InboundMediaJob, old_id).read_token_hash == old_token
        assert (
            session.scalar(
                select(InboundHostBinding).where(InboundHostBinding.job_id == old_id)
            ).grant_ciphertext
            == old_cipher
        )
