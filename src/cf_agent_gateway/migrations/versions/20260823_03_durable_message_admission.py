"""Add one durable admission outcome per persisted message.

Revision ID: 20260823_03
Revises: 20260823_02
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260823_03"
down_revision: str | Sequence[str] | None = "20260823_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "message_admission_outcomes"


def upgrade() -> None:
    _require_schema_state(upgrading=True)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "completed",
                name="message_admission_outcome_state",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "allowed",
                "denied",
                "unresolved",
                name="message_admission_decision",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=True,
        ),
        sa.Column(
            "evidence_origin",
            sa.Enum(
                "runtime",
                "legacy_dispatch",
                "legacy_unresolved",
                name="message_admission_evidence_origin",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("admission_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "should_create_task",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("requested_scope", sa.JSON(), nullable=True),
        sa.Column("requested_skill_ids", sa.JSON(), nullable=True),
        sa.Column("risk_level", sa.String(length=32), nullable=True),
        sa.Column("authorization_reason_code", sa.String(length=64), nullable=True),
        sa.Column("authorization_snapshot", sa.JSON(), nullable=True),
        sa.Column("policy_snapshot", sa.JSON(), nullable=True),
        sa.Column("gateway_policy_id", sa.String(length=36), nullable=True),
        sa.Column("gateway_policy_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_policy_id", sa.String(length=36), nullable=True),
        sa.Column("user_policy_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enterprise_identity_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("ai_thread_id", sa.String(length=36), nullable=True),
        sa.Column(
            "routing_mode",
            sa.String(length=16),
            server_default="none",
            nullable=False,
        ),
        sa.Column("route_conversation_record_id", sa.Integer(), nullable=True),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=True),
        sa.Column("agent_profile_key", sa.String(length=64), nullable=True),
        sa.Column("agent_profile_reference", sa.String(length=255), nullable=True),
        sa.Column("agent_profile_revision", sa.Integer(), nullable=True),
        sa.Column("group_type_id", sa.String(length=36), nullable=True),
        sa.Column("group_type_key", sa.String(length=64), nullable=True),
        sa.Column("thread_policy", sa.String(length=32), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed')",
            name="ck_message_admission_outcome_state",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('allowed', 'denied', 'unresolved')",
            name="ck_message_admission_outcome_decision",
        ),
        sa.CheckConstraint(
            "evidence_origin IN ('runtime', 'legacy_dispatch', 'legacy_unresolved')",
            name="ck_message_admission_evidence_origin",
        ),
        sa.CheckConstraint(
            "(evidence_origin = 'runtime' AND "
            "(state = 'pending' OR decision IN ('allowed', 'denied'))) OR "
            "(evidence_origin = 'legacy_dispatch' AND "
            "state = 'completed' AND decision = 'allowed') OR "
            "(evidence_origin = 'legacy_unresolved' AND "
            "state = 'completed' AND decision = 'unresolved')",
            name="ck_message_admission_origin_decision",
        ),
        sa.CheckConstraint(
            "routing_mode IN ('none', 'v1', 'v2', 'legacy')",
            name="ck_message_admission_routing_mode",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_message_admission_nonnegative_attempt_count",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL AND lease_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_message_admission_claim_pair",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND decision IS NULL "
            "AND admission_reason IS NULL AND completed_at IS NULL "
            "AND should_create_task = false AND evidence_origin = 'runtime') OR "
            "(state = 'completed' AND decision IS NOT NULL "
            "AND admission_reason IS NOT NULL AND completed_at IS NOT NULL "
            "AND claim_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_message_admission_state_fields",
        ),
        sa.CheckConstraint(
            "evidence_origin != 'runtime' OR "
            "(requested_scope IS NOT NULL AND requested_skill_ids IS NOT NULL "
            "AND risk_level IS NOT NULL)",
            name="ck_message_admission_runtime_request",
        ),
        sa.CheckConstraint(
            "(decision = 'allowed' AND should_create_task = true "
            "AND enterprise_identity_id IS NOT NULL "
            "AND workspace_id IS NOT NULL AND ai_thread_id IS NOT NULL) OR "
            "(decision IN ('denied', 'unresolved') AND should_create_task = false "
            "AND workspace_id IS NULL AND ai_thread_id IS NULL) OR "
            "decision IS NULL",
            name="ck_message_admission_decision_targets",
        ),
        sa.CheckConstraint(
            "(decision = 'allowed' AND admission_reason = 'allowed') OR "
            "(decision = 'unresolved' AND admission_reason = 'legacy_unresolved') OR "
            "(decision = 'denied' AND admission_reason NOT IN "
            "('allowed', 'legacy_unresolved')) OR decision IS NULL",
            name="ck_message_admission_decision_reason",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_message_admission_nonempty_error",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gateway_policy_id"],
            ["gateway_access_policies.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_policy_id"],
            ["user_access_policies.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_identity_id"],
            ["enterprise_identities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["employee_workspaces.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ai_thread_id"],
            ["ai_threads.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["route_conversation_record_id"],
            ["conversations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["group_type_id"],
            ["group_types.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_message_admission_outcome_message"),
    )
    op.create_index(
        "ix_message_admission_outcomes_message_id",
        _TABLE,
        ["message_id"],
        unique=False,
    )
    op.create_index(
        "ix_message_admission_state_lease",
        _TABLE,
        ["state", "lease_expires_at", "message_id"],
        unique=False,
    )
    op.create_index(
        "ix_message_admission_decision_completed",
        _TABLE,
        ["decision", "completed_at", "message_id"],
        unique=False,
    )
    _backfill_legacy_outcomes()
    _require_backfill_complete()


def downgrade() -> None:
    _require_schema_state(upgrading=False)
    _require_downgrade_data_safe()
    op.drop_index("ix_message_admission_decision_completed", table_name=_TABLE)
    op.drop_index("ix_message_admission_state_lease", table_name=_TABLE)
    op.drop_index("ix_message_admission_outcomes_message_id", table_name=_TABLE)
    op.drop_table(_TABLE)


def _backfill_legacy_outcomes() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO message_admission_outcomes (
                message_id, state, decision, evidence_origin, admission_reason,
                should_create_task, enterprise_identity_id, workspace_id,
                ai_thread_id, routing_mode, agent_profile_id,
                agent_profile_key, agent_profile_reference,
                agent_profile_revision, thread_policy, attempt_count,
                completed_at, created_at, updated_at
            )
            SELECT
                messages.id,
                'completed',
                CASE WHEN dispatch.id IS NOT NULL THEN 'allowed' ELSE 'unresolved' END,
                CASE
                    WHEN dispatch.id IS NOT NULL THEN 'legacy_dispatch'
                    ELSE 'legacy_unresolved'
                END,
                CASE WHEN dispatch.id IS NOT NULL THEN 'allowed' ELSE 'legacy_unresolved' END,
                CASE WHEN dispatch.id IS NOT NULL THEN true ELSE false END,
                dispatch.enterprise_identity_id,
                dispatch.workspace_id,
                dispatch.ai_thread_id,
                'legacy',
                thread.agent_profile_id,
                profile.profile_key,
                profile.external_profile_ref,
                profile.revision,
                thread.thread_policy,
                0,
                COALESCE(dispatch.created_at, messages.created_at, CURRENT_TIMESTAMP),
                COALESCE(messages.created_at, CURRENT_TIMESTAMP),
                CURRENT_TIMESTAMP
            FROM messages
            LEFT JOIN hermes_dispatch_records AS dispatch
                ON dispatch.message_id = messages.id
            LEFT JOIN ai_threads AS thread
                ON thread.id = dispatch.ai_thread_id
            LEFT JOIN agent_profiles AS profile
                ON profile.id = thread.agent_profile_id
            """
        )
    )


def _require_schema_state(*, upgrading: bool) -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    required = {
        "messages",
        "hermes_dispatch_records",
        "gateway_access_policies",
        "user_access_policies",
        "enterprise_identities",
        "employee_workspaces",
        "ai_threads",
        "conversations",
        "agent_profiles",
        "group_types",
    }
    if not required.issubset(tables):
        raise RuntimeError("durable admission source schema is incomplete")
    if upgrading and _TABLE in tables:
        raise RuntimeError("partial durable admission schema detected")
    if not upgrading and _TABLE not in tables:
        raise RuntimeError("partial durable admission schema detected")


def _require_backfill_complete() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    message_count = bind.scalar(sa.text("SELECT count(*) FROM messages"))
    outcome_count = bind.scalar(sa.text(f"SELECT count(*) FROM {_TABLE}"))
    dispatch_count = bind.scalar(sa.text("SELECT count(*) FROM hermes_dispatch_records"))
    allowed_count = bind.scalar(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} "
            "WHERE evidence_origin = 'legacy_dispatch' AND decision = 'allowed'"
        )
    )
    invalid_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM message_admission_outcomes AS outcome
            LEFT JOIN hermes_dispatch_records AS dispatch
                ON dispatch.message_id = outcome.message_id
            WHERE
                (outcome.evidence_origin = 'legacy_dispatch' AND (
                    dispatch.id IS NULL
                    OR outcome.enterprise_identity_id != dispatch.enterprise_identity_id
                    OR outcome.workspace_id != dispatch.workspace_id
                    OR outcome.ai_thread_id != dispatch.ai_thread_id
                ))
                OR (outcome.evidence_origin = 'legacy_unresolved' AND dispatch.id IS NOT NULL)
            """
        )
    )
    if outcome_count != message_count or allowed_count != dispatch_count or invalid_count:
        raise RuntimeError("durable admission backfill validation failed")


def _require_downgrade_data_safe() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline durable admission downgrade is disabled because runtime "
            "evidence cannot be inspected safely"
        )
    bind = op.get_bind()
    runtime_count = bind.scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE evidence_origin = 'runtime'")
    )
    if runtime_count:
        raise RuntimeError(
            "runtime admission evidence exists; restore the pre-upgrade backup "
            "to roll back; never delete authoritative admission outcomes"
        )
    _require_backfill_complete()
