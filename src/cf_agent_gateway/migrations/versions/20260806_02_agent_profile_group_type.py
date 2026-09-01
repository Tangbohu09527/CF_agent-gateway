"""Add Agent Profile and Group Type configuration.

Revision ID: 20260806_02
Revises: 20260806_0002
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_02"
down_revision: str | Sequence[str] | None = "20260806_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V2_TABLES = frozenset(
    {
        "agent_profiles",
        "conversation_group_type_bindings",
        "group_types",
    }
)


def upgrade() -> None:
    if not op.get_context().as_sql:
        table_names = set(sa.inspect(op.get_bind()).get_table_names())
        v2_tables = table_names & _V2_TABLES
        if v2_tables and v2_tables != _V2_TABLES:
            raise RuntimeError("partial Agent Profile and Group Type schema detected")
        if v2_tables == _V2_TABLES:
            _create_immutable_revision_guard()
            return
        if "conversations" not in table_names:
            return

    _create_tables()
    _create_immutable_revision_guard()


def _create_tables() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_profile_ref", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "disabled",
                "archived",
                name="agent_profile_status",
                native_enum=False,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(external_profile_ref)) > 0",
            name="ck_agent_profile_nonempty_external_ref",
        ),
        sa.CheckConstraint(
            "length(trim(profile_key)) > 0",
            name="ck_agent_profile_nonempty_key",
        ),
        sa.CheckConstraint(
            "length(trim(model)) > 0",
            name="ck_agent_profile_nonempty_model",
        ),
        sa.CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_agent_profile_nonempty_provider",
        ),
        sa.CheckConstraint("revision > 0", name="ck_agent_profile_positive_revision"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_key",
            "revision",
            name="uq_agent_profile_key_revision",
        ),
    )
    op.create_table(
        "group_types",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("type_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=False),
        sa.Column(
            "thread_policy",
            sa.Enum(
                "private_sender",
                "group_shared",
                "group_sender",
                name="thread_policy",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "disabled",
                "archived",
                name="group_type_status",
                native_enum=False,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "thread_policy IN ('group_shared', 'group_sender')",
            name="ck_group_type_group_thread_policy",
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0",
            name="ck_group_type_nonempty_display_name",
        ),
        sa.CheckConstraint(
            "length(trim(type_key)) > 0",
            name="ck_group_type_nonempty_key",
        ),
        sa.CheckConstraint(
            "length(trim(thread_policy)) > 0",
            name="ck_group_type_nonempty_thread_policy",
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("type_key", name="uq_group_type_key"),
    )
    op.create_index(
        "ix_group_types_agent_profile_id",
        "group_types",
        ["agent_profile_id"],
        unique=False,
    )
    op.create_table(
        "conversation_group_type_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("group_type_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_type_id"],
            ["group_types.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            name="uq_conversation_group_type_binding_conversation",
        ),
    )
    op.create_index(
        "ix_conversation_group_type_bindings_conversation_id",
        "conversation_group_type_bindings",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_group_type_bindings_group_type_id",
        "conversation_group_type_bindings",
        ["group_type_id"],
        unique=False,
    )


def downgrade() -> None:
    if not op.get_context().as_sql:
        table_names = set(sa.inspect(op.get_bind()).get_table_names())
        v2_tables = table_names & _V2_TABLES
        if v2_tables and v2_tables != _V2_TABLES:
            raise RuntimeError("partial Agent Profile and Group Type schema detected")
        if not v2_tables:
            return

    _drop_immutable_revision_guard()
    op.drop_index(
        "ix_conversation_group_type_bindings_group_type_id",
        table_name="conversation_group_type_bindings",
    )
    op.drop_index(
        "ix_conversation_group_type_bindings_conversation_id",
        table_name="conversation_group_type_bindings",
    )
    op.drop_table("conversation_group_type_bindings")
    op.drop_index("ix_group_types_agent_profile_id", table_name="group_types")
    op.drop_table("group_types")
    op.drop_table("agent_profiles")


def _create_immutable_revision_guard() -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_profiles_immutable_revision
            BEFORE UPDATE OF id, profile_key, revision, provider,
                external_profile_ref, model, created_at
            ON agent_profiles
            FOR EACH ROW
            WHEN NEW.id IS NOT OLD.id
              OR NEW.profile_key IS NOT OLD.profile_key
              OR NEW.revision IS NOT OLD.revision
              OR NEW.provider IS NOT OLD.provider
              OR NEW.external_profile_ref IS NOT OLD.external_profile_ref
              OR NEW.model IS NOT OLD.model
              OR NEW.created_at IS NOT OLD.created_at
            BEGIN
                SELECT RAISE(ABORT, 'agent profile revision is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_agent_profiles_prevent_delete
            BEFORE DELETE ON agent_profiles
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'agent profile revision is immutable');
            END
            """
        )
    elif dialect_name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_agent_profile_revision_immutable()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'agent profile revision is immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.profile_key IS DISTINCT FROM OLD.profile_key
                   OR NEW.revision IS DISTINCT FROM OLD.revision
                   OR NEW.provider IS DISTINCT FROM OLD.provider
                   OR NEW.external_profile_ref IS DISTINCT FROM OLD.external_profile_ref
                   OR NEW.model IS DISTINCT FROM OLD.model
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'agent profile revision is immutable'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_agent_profiles_immutable_revision ON agent_profiles")
        op.execute(
            """
            CREATE TRIGGER trg_agent_profiles_immutable_revision
            BEFORE UPDATE OR DELETE ON agent_profiles
            FOR EACH ROW
            EXECUTE FUNCTION guard_agent_profile_revision_immutable()
            """
        )


def _drop_immutable_revision_guard() -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_agent_profiles_immutable_revision")
        op.execute("DROP TRIGGER IF EXISTS trg_agent_profiles_prevent_delete")
    elif dialect_name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_agent_profiles_immutable_revision ON agent_profiles")
        op.execute("DROP FUNCTION IF EXISTS guard_agent_profile_revision_immutable()")
