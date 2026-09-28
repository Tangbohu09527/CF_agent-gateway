"""Real Gateway service/claims/SQL, with an explicitly synthetic execution host."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_inbound_media_queue import HOST, MEDIA, Hermes, admit, worker
from test_inbound_media_queue import rig as queue_rig

from cf_agent_gateway.hermes import HermesDispatchService
from cf_agent_gateway.hermes.errors import HermesDispatchError, HermesResponseError
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import AIThread

rig = queue_rig


def test_default_disabled_has_no_model_call_or_grant(rig):
    admit(rig)
    assert worker(rig).run_once() == "ready"
    hermes = Hermes()
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="fixture-claim")
        with pytest.raises(HermesDispatchError, match="inbound_host_binding_required"):
            HermesDispatchService(session, hermes, inbound_media=MEDIA).dispatch_record(record)
        assert session.scalar(select(InboundHostBinding)) is None
        assert session.get(InboundMediaJob, 1).read_token_hash is None
    assert not hermes.contents


def test_claim_prebinding_precedes_chat_and_model_input_contains_no_grant(rig, caplog):
    outcome = admit(rig)
    assert worker(rig).run_once() == "ready"

    class InspectHost(Hermes):
        def chat(self, content, **kwargs):
            with rig.sessions() as inspector:
                binding = inspector.scalar(select(InboundHostBinding))
                assert binding.state == "running"
                assert binding.session_id == kwargs["hermes_thread_id"]
                assert binding.history_digest and binding.runtime_config_digest
                assert binding.parent_session_id is None
                assert (
                    inspector.get(AIThread, outcome.ai_thread_id).hermes_thread_id
                    != binding.session_id
                )
                assert binding.grant_ciphertext
            assert kwargs["runtime_model"] == HOST.runtime_model
            assert kwargs["runtime_provider"] == HOST.runtime_provider
            return super().chat(content, **kwargs)

    host = InspectHost()
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="fixture-claim")
        result = HermesDispatchService(
            session, host, inbound_media=MEDIA, host_binding=HOST
        ).dispatch_record(record)
        assert result.next_hermes_thread_id != result.requested_hermes_thread_id
    content = json.loads(host.contents[0].split("\n", 1)[1])
    assert set(content) == {"text", "attachment_id"}
    assert content["text"] == "" and content["attachment_id"]
    assert "Bearer " not in caplog.text + repr(host.contents) + repr(host.invocations)


@pytest.mark.parametrize("legacy_confirmed", [False, True])
def test_preparation_timeout_recovery_keeps_child_history_and_encrypted_grant(
    rig, legacy_confirmed
):
    admit(rig)
    assert worker(rig).run_once() == "ready"

    class LostReplyHost(Hermes):
        attempts = []

        def prepare_inbound_session(self, session_id, **kwargs):
            self.attempts.append((session_id, kwargs["allow_create"], kwargs["parent_session_id"]))
            if kwargs["allow_create"]:
                kwargs["record_history"]("0" * 64)
                raise HermesResponseError(operation="synthetic-lost-create-response")
            assert kwargs["expected_history_digest"] == "0" * 64

    host = LostReplyHost()
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="same-claim")
        service = HermesDispatchService(
            session,
            host,
            inbound_media=MEDIA,
            host_binding=replace(HOST, legacy_runtime_confirmed=legacy_confirmed),
        )
        with pytest.raises(HermesResponseError):
            service.dispatch_record(record)
        session.rollback()
        binding = session.scalar(select(InboundHostBinding))
        before = (
            binding.session_id,
            binding.grant_ciphertext,
            session.get(InboundMediaJob, 1).read_token_hash,
        )
        service.dispatch_record(record)
        session.refresh(binding)
        assert (
            binding.session_id,
            binding.grant_ciphertext,
            session.get(InboundMediaJob, 1).read_token_hash,
        ) == before
    assert host.attempts == [(before[0], True, None), (before[0], False, None)]
    assert len(host.contents) == 1


def test_unapproved_legacy_runtime_blocks_existing_parent(rig):
    outcome = admit(rig)
    assert worker(rig).run_once() == "ready"
    host = Hermes()
    with rig.sessions() as session:
        session.get(AIThread, outcome.ai_thread_id).hermes_thread_id = "legacy-parent"
        session.commit()
        record = HermesDispatchRecordStore(session).claim_next(claim_token="fixture-claim")
        with pytest.raises(HermesDispatchError, match="inbound_parent_runtime_unverified"):
            HermesDispatchService(
                session,
                host,
                inbound_media=MEDIA,
                host_binding=replace(HOST, legacy_runtime_confirmed=False),
            ).dispatch_record(record)
        assert session.scalar(select(InboundHostBinding)) is None
    assert not host.contents


def test_rotated_execution_session_is_uncertain_and_never_advances_thread(rig):
    outcome = admit(rig)
    assert worker(rig).run_once() == "ready"

    class RotatingHost(Hermes):
        def verify_inbound_session_tip(self, session_id):
            raise HermesResponseError(operation="inbound_session_tip")

    host = RotatingHost()
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="fixture-claim")
        with pytest.raises(HermesResponseError):
            HermesDispatchService(
                session, host, inbound_media=MEDIA, host_binding=HOST
            ).dispatch_record(record)
        binding = session.scalar(select(InboundHostBinding))
        assert session.get(AIThread, outcome.ai_thread_id).hermes_thread_id != binding.session_id


def test_stale_worker_cannot_prepare_on_behalf_of_replacement_claim(rig):
    admit(rig)
    assert worker(rig).run_once() == "ready"

    class PausedService(HermesDispatchService):
        def _resolve_dispatch_profile(self, thread, message, admission):
            profile = super()._resolve_dispatch_profile(thread, message, admission)
            # The original worker has captured its fence, then pauses while a
            # different recovery claim is installed. It must not adopt that fence.
            with rig.sessions() as replacement:
                replacement.scalar(select(HermesDispatchRecord)).claim_token = "replacement-claim"
                replacement.commit()
            return profile

    host = Hermes()
    with rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="stale-claim")
        with pytest.raises(HermesResponseError):
            PausedService(session, host, inbound_media=MEDIA, host_binding=HOST).dispatch_record(
                record
            )
        session.rollback()
        assert session.scalar(select(InboundHostBinding)) is None
        assert session.get(InboundMediaJob, 1).read_token_hash is None
    assert not host.contents
