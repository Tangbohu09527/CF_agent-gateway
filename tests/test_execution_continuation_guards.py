"""Targeted continuation guards; all peers, credentials and databases are synthetic."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from test_inbound_execution_continuation import (
    continuation as continuation_fixture,
)
from test_inbound_execution_continuation import (
    establish_text,
    first_managed_attachment,
)
from test_inbound_execution_continuation import (
    rig as queue_rig,
)
from test_inbound_media_queue import MEDIA
from test_migrations import migration_config

from cf_agent_gateway.hermes import HermesDispatchService
from cf_agent_gateway.hermes.errors import HermesDispatchError, HermesResponseError
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.message.models import Message
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import AIThread

continuation = continuation_fixture
rig = queue_rig


@pytest.mark.parametrize("change", ["disabled_host", "host", "identity", "mapping"])
def test_managed_text_denial_never_falls_back_to_unbound_chat(continuation, change):
    h = continuation
    first_managed_attachment(h)
    h.admit(4, content="Synthetic follow-up under the existing execution binding.")
    before = (len(h.host.calls), len(h.host.preparations))
    if change == "disabled_host":
        h.configure(enabled=False)
    elif change == "host":
        h.configure(host_id="different-synthetic-host")
    else:
        with h.rig.sessions() as session:
            if change == "identity":
                session.scalar(select(EnterpriseIdentity)).status = IdentityStatus.DISABLED
            else:
                session.scalar(select(SourceIdentityMapping)).enabled = False
            session.commit()
    assert h.run().status.value != "success"
    assert (len(h.host.calls), len(h.host.preparations)) == before


def test_fresh_instruction_never_skips_a_successful_attachment(continuation):
    h = continuation
    original = establish_text(h, 1, "Instruction for the earlier attachment only.")
    h.configure(legacy_runtime_confirmed=True)
    h.admit(2, kind="pdf")
    assert h.run().status.value == "success"
    h.configure(legacy_runtime_confirmed=False, fresh_session_thread_ids=(original.ai_thread_id,))
    later = h.admit(3, kind="png")
    before = (len(h.host.calls), len(h.host.preparations))
    assert h.run().status.value == "failed"
    assert (len(h.host.calls), len(h.host.preparations)) == before
    with h.rig.sessions() as session:
        record = session.get(HermesDispatchRecord, later.dispatch_record_id)
        assert record.last_error_code.endswith(":inbound_execution_instruction_missing")
        assert len(session.scalars(select(InboundHostBinding)).all()) == 1


@pytest.mark.parametrize("change", ["instruction_content", "previous_tip"])
def test_pending_intent_cannot_adopt_changed_inputs(continuation, change):
    h = continuation
    original = establish_text(h, 1, "Synthetic original text.")
    saved_instruction = establish_text(h, 2, "Synthetic frozen file instruction.")
    h.configure(fresh_session_thread_ids=(original.ai_thread_id,))
    h.admit(3, kind="pdf")
    h.host.lose_create_reply = True
    with h.rig.sessions() as session:
        record = HermesDispatchRecordStore(session).claim_next(claim_token="synthetic-frozen-claim")
        service = HermesDispatchService(
            session,
            h.host,
            inbound_media=MEDIA,
            host_binding=h.settings.host_binding,
            artifact_return=h.settings.artifact_return,
        )
        with pytest.raises(HermesResponseError):
            service.dispatch_record(record)
        session.rollback()
        binding = session.scalar(select(InboundHostBinding))
        frozen = (
            binding.id,
            binding.session_id,
            binding.parent_session_id,
            binding.previous_session_id,
            binding.instruction_message_id,
            binding.instruction_digest,
        )
        if change == "instruction_content":
            session.get(
                Message, saved_instruction.message_id
            ).content = "Changed synthetic instruction."
        else:
            session.get(AIThread, original.ai_thread_id).hermes_thread_id = "unknown-synthetic-tip"
        session.commit()
        before = (len(h.host.calls), len(h.host.preparations))
        with pytest.raises(HermesDispatchError):
            service.dispatch_record(record)
        session.rollback()
        session.refresh(binding)
        assert (
            binding.id,
            binding.session_id,
            binding.parent_session_id,
            binding.previous_session_id,
            binding.instruction_message_id,
            binding.instruction_digest,
        ) == frozen
        assert (len(h.host.calls), len(h.host.preparations)) == before


def test_managed_text_hidden_tip_rotation_stays_uncertain_without_call(continuation):
    h = continuation
    original, _, _ = first_managed_attachment(h)
    before_tip = h.tip(original.ai_thread_id)
    text = h.admit(4, content="Synthetic text after a hidden compression rotation.")
    before = len(h.host.calls)
    h.host.rotate_tip = True
    result = h.run()
    assert result.status.value == "uncertain"
    assert len(h.host.calls) == before
    assert h.tip(original.ai_thread_id) == before_tip
    with h.rig.sessions() as session:
        assert (
            session.get(HermesDispatchRecord, text.dispatch_record_id).status.value == "uncertain"
        )


def test_latest_instruction_uses_dispatch_fifo_order_not_numeric_id(continuation):
    h = continuation
    older_id = establish_text(h, 1, "This is the latest instruction in durable FIFO order.")
    newer_id = establish_text(h, 2, "This dispatch was earlier in durable FIFO order.")
    h.configure(fresh_session_thread_ids=(older_id.ai_thread_id,))
    pending = h.admit(3, kind="pdf")
    # PostgreSQL transaction timestamps and sequence IDs can differ in order;
    # mirror the store's (created_at, id) FIFO contract, not message ID order.
    now = datetime.now(UTC)
    with h.rig.sessions() as session:
        session.get(HermesDispatchRecord, newer_id.dispatch_record_id).created_at = now - timedelta(
            seconds=30
        )
        session.get(HermesDispatchRecord, older_id.dispatch_record_id).created_at = now - timedelta(
            seconds=20
        )
        session.get(HermesDispatchRecord, pending.dispatch_record_id).created_at = now - timedelta(
            seconds=10
        )
        session.commit()
    assert h.run().status.value == "success"
    payload = json.loads(h.host.calls[-1][0].split("\n", 1)[1])
    assert payload["previous_instruction"]["message_id"] == older_id.message_id


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_fallback_confirmation_rejects_non_boolean(value):
    with pytest.raises(ValueError, match="fallback_disabled_confirmed must be boolean"):
        HostBindingSettings(runtime_fallback_disabled_confirmed=value)


def test_fresh_allowlist_requires_fallback_confirmation():
    with pytest.raises(ValueError, match="verified runtime fallback disabled"):
        HostBindingSettings(fresh_session_thread_ids=("synthetic-private-thread",))


def test_removed_allowlist_cannot_hide_managed_fallback_drift(continuation):
    h = continuation
    first_managed_attachment(h)
    h.configure(fresh_session_thread_ids=(), runtime_fallback_disabled_confirmed=False)
    h.admit(4, content="Synthetic text must retain existing lineage guards.")
    before = (len(h.host.calls), len(h.host.preparations))
    assert h.run().status.value != "success"
    assert (len(h.host.calls), len(h.host.preparations)) == before


def test_prior_instruction_may_have_larger_message_id_than_current(continuation):
    h = continuation
    original = establish_text(h, 1, "Earlier instruction must not be reused.")
    h.configure(fresh_session_thread_ids=(original.ai_thread_id,))
    pending = h.admit(2, kind="pdf")
    instruction = h.admit(3, content="Latest successful instruction by durable FIFO order.")
    now = datetime.now(UTC)
    with h.rig.sessions() as session:
        session.get(HermesDispatchRecord, original.dispatch_record_id).created_at = now - timedelta(
            seconds=30
        )
        session.get(HermesDispatchRecord, instruction.dispatch_record_id).created_at = (
            now - timedelta(seconds=20)
        )
        session.get(HermesDispatchRecord, pending.dispatch_record_id).created_at = now - timedelta(
            seconds=10
        )
        session.commit()
    assert h.run().status.value == "success"
    assert h.host.calls[-1][0] == "Latest successful instruction by durable FIFO order."
    assert h.run().status.value == "success"
    payload = json.loads(h.host.calls[-1][0].split("\n", 1)[1])
    assert payload["previous_instruction"]["message_id"] == instruction.message_id


CONTINUATION_FIELDS = {
    "execution_policy",
    "previous_session_id",
    "instruction_message_id",
    "instruction_digest",
}


def test_populated_legacy_bindings_migrate_without_backfill_or_data_loss(continuation):
    h = continuation
    establish_text(h)
    h.configure(legacy_runtime_confirmed=True)
    h.admit(2, kind="pdf")
    assert h.run().status.value == "success"
    engine = h.rig.engine
    config = migration_config(str(engine.url))
    with engine.connect() as connection:
        original = dict(
            connection.execute(text("SELECT * FROM inbound_host_bindings")).mappings().one()
        )
        assert all(original.pop(field) is None for field in CONTINUATION_FIELDS)
        counts = {
            name: connection.scalar(text(f"SELECT count(*) FROM {name}"))
            for name in (
                "messages",
                "hermes_dispatch_records",
                "hermes_dispatch_responses",
                "inbound_media_jobs",
                "inbound_host_bindings",
                "delivery_outbox",
            )
        }
    command.downgrade(config, "20260928_01")
    with engine.connect() as connection:
        assert (
            dict(connection.execute(text("SELECT * FROM inbound_host_bindings")).mappings().one())
            == original
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260928_01"
    command.upgrade(config, "head")
    assert {
        field["name"] for field in inspect(engine).get_columns("inbound_host_bindings")
    } >= CONTINUATION_FIELDS
    with engine.connect() as connection:
        restored = dict(
            connection.execute(text("SELECT * FROM inbound_host_bindings")).mappings().one()
        )
        assert all(restored.pop(field) is None for field in CONTINUATION_FIELDS)
        assert restored == original
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert {
            name: connection.scalar(text(f"SELECT count(*) FROM {name}")) for name in counts
        } == counts


def test_migration_cannot_discard_committed_execution_lineage(continuation):
    h = continuation
    first_managed_attachment(h)
    engine = h.rig.engine
    with engine.connect() as connection:
        original = list(connection.execute(text("SELECT * FROM inbound_host_bindings")))
    with pytest.raises(RuntimeError, match="cannot discard persisted execution lineage"):
        command.downgrade(migration_config(str(engine.url)), "20260928_01")
    with engine.connect() as connection:
        assert list(connection.execute(text("SELECT * FROM inbound_host_bindings"))) == original
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261016_01"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
