"""Real Gateway SQL/HTTP/claim/Delivery flow; Hermes/WeChat remain explicit doubles."""

from dataclasses import replace

import test_inbound_execution_continuation as continuation_tests
from sqlalchemy import select
from test_hermes_dispatch_ingestion import normalized_message
from test_inbound_execution_continuation import PDF, first_managed_attachment

from cf_agent_gateway.inbound import followup
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.ingestion import MessageAdmissionService
from cf_agent_gateway.message.models import Attachment
from cf_agent_gateway.task.model import HermesDispatchRecord

rig = continuation_tests.rig
continuation = continuation_tests.continuation


def admit_operation(h, index, attachment_id, action="read-return"):
    incoming = normalized_message(
        event_id=f"operation:{index}",
        source_message_id=f"o{index}",
        source_local_id=f"o{index}",
        source_server_id=f"o{index}",
        content=f"/attachment {attachment_id} {action}\nUse only the registered original.",
    )
    with h.rig.sessions() as session:
        return MessageAdmissionService(session, inbound_media=h.settings.inbound_media).process(
            incoming
        )


def test_original_then_new_operation_then_gateway_restart_reuses_bytes_not_old_lease(
    continuation, monkeypatch
):
    h = continuation
    _, _, prior = first_managed_attachment(h)
    h.deliver_all()
    old_record = h.snapshot(prior.dispatch_record_id)
    with h.rig.sessions() as session:
        old = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == prior.message_id)
        )
        aid, old_jid = old.attachment_id, old.id
        frozen = {column.name: getattr(old, column.name) for column in old.__table__.columns}
        old_attachment = {
            column.name: getattr(session.get(Attachment, aid), column.name)
            for column in Attachment.__table__.columns
        }
    h.settings = replace(
        h.settings, inbound_media=replace(h.settings.inbound_media, followup_enabled=True)
    )
    h.client.app.state.settings = h.settings
    monkeypatch.setattr(followup, "InboundMediaStaging", lambda _root: h.rig.staging)
    grants = []
    for index in (4, 5):
        before_tip = h.tip(prior.ai_thread_id)
        outcome = admit_operation(h, index, aid)
        assert h.run().status.value == "success"
        h.deliver_all()
        grant = h.host.grants[-1]
        grants.append((grant["binding_id"], grant["attachments"][0]["authorization"]))
        assert grant["attachments"][0]["message_id"] == outcome.message_id
        assert grant["attachments"][0]["attachment_id"] == aid
        assert h.host.preparations[-1][1] == before_tip
        assert h.host.uploads[-1][2] == PDF
        assert h.sender.media[-1][2] == PDF
        assert h.snapshot(prior.dispatch_record_id) == old_record
        with h.rig.sessions() as session:
            old = session.get(InboundMediaJob, old_jid)
            assert {
                column.name: getattr(old, column.name) for column in old.__table__.columns
            } == frozen
            assert {
                column.name: getattr(session.get(Attachment, aid), column.name)
                for column in Attachment.__table__.columns
            } == old_attachment
            new = session.scalar(
                select(InboundMediaJob).where(InboundMediaJob.message_id == outcome.message_id)
            )
            assert new.source_job_id == old_jid and new.dispatch_id != prior.dispatch_record_id
            assert session.get(HermesDispatchRecord, outcome.dispatch_record_id).attempt_count == 1
        h.restart()
    assert grants[0][0] != grants[1][0] and grants[0][1] != grants[1][1]


def test_read_only_operation_has_no_return_context_and_delivers_text(continuation, monkeypatch):
    import asyncio

    from test_inbound_execution_continuation import DeterministicHermes

    from cf_agent_gateway.hermes import HermesChatResult

    h = continuation
    _, _, prior = first_managed_attachment(h)
    h.deliver_all()
    before_media = list(h.sender.media)
    with h.rig.sessions() as session:
        aid = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == prior.message_id)
        ).attachment_id
    h.settings = replace(
        h.settings, inbound_media=replace(h.settings.inbound_media, followup_enabled=True)
    )
    h.client.app.state.settings = h.settings
    monkeypatch.setattr(followup, "InboundMediaStaging", lambda _root: h.rig.staging)

    class ReadHost(DeterministicHermes):
        def chat(self, content, **kwargs):
            assert "artifact_return_context" not in kwargs
            self.calls.append((content, kwargs))
            session_id = kwargs["hermes_thread_id"]
            data, _ = asyncio.run(self._download(session_id))
            assert data == PDF
            self.sessions[session_id].append(content)
            return HermesChatResult(
                assistant_content="Read completed without artifact return.",
                hermes_thread_id=session_id,
            )

    h.host = ReadHost(h, sessions=h.host.sessions)
    outcome = admit_operation(h, 4, aid, action="read")
    assert h.run().status.value == "success"
    h.deliver_all()
    assert h.host.downloads == [PDF] and h.host.uploads == []
    assert h.sender.media == before_media
    assert h.sender.texts[-1][1] == "Read completed without artifact return."
    assert h.snapshot(outcome.dispatch_record_id)["attempt_count"] == 1


def test_missing_registered_bytes_fail_before_fork_or_model(continuation, monkeypatch):
    h = continuation
    _, _, prior = first_managed_attachment(h)
    with h.rig.sessions() as session:
        aid = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == prior.message_id)
        ).attachment_id
    old_calls, old_preparations = len(h.host.calls), len(h.host.preparations)
    h.settings = replace(
        h.settings, inbound_media=replace(h.settings.inbound_media, followup_enabled=True)
    )
    h.client.app.state.settings = h.settings
    monkeypatch.setattr(followup, "InboundMediaStaging", lambda _root: h.rig.staging)
    h.rig.staging.blobs.clear()
    new = admit_operation(h, 4, aid)
    assert h.run().status.value != "success"
    assert len(h.host.calls) == old_calls and len(h.host.preparations) == old_preparations
    with h.rig.sessions() as session:
        job = session.scalar(
            select(InboundMediaJob).where(InboundMediaJob.message_id == new.message_id)
        )
        assert job.read_token_hash is None
