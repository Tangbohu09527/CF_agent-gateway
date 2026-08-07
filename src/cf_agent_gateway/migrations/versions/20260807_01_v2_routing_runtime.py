"""Enable persisted V2 routing runtime facts.

Revision ID: 20260807_01
Revises: 20260806_04
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_01"
down_revision: str | Sequence[str] | None = "20260806_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_agent_profile_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            name="uq_conversation_agent_profile_binding_conversation",
        ),
    )
    op.create_index(
        "ix_conversation_agent_profile_bindings_agent_profile_id",
        "conversation_agent_profile_bindings",
        ["agent_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_agent_profile_bindings_conversation_id",
        "conversation_agent_profile_bindings",
        ["conversation_id"],
        unique=False,
    )

    with op.batch_alter_table("ai_threads", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_profile_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column(
                "thread_policy",
                sa.Enum(
                    "private_sender",
                    "group_shared",
                    "group_sender",
                    name="thread_policy",
                    native_enum=False,
                    create_constraint=False,
                ),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_ai_thread_agent_profile",
            "agent_profiles",
            ["agent_profile_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_ai_thread_v2_route_snapshot",
            "(agent_profile_id IS NULL AND thread_policy IS NULL) OR "
            "(agent_profile_id IS NOT NULL AND thread_policy IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_ai_thread_v2_thread_policy",
            "thread_policy IS NULL OR "
            "thread_policy IN ('private_sender', 'group_shared', 'group_sender')",
        )
        batch_op.create_check_constraint(
            "ck_ai_thread_v2_policy_matches_type",
            "agent_profile_id IS NULL OR "
            "(thread_type = 'private' AND thread_policy = 'private_sender') OR "
            "(thread_type = 'group' AND "
            "thread_policy IN ('group_shared', 'group_sender'))",
        )
        batch_op.create_index(
            "ix_ai_threads_agent_profile_id",
            ["agent_profile_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_threads", schema=None) as batch_op:
        batch_op.drop_index("ix_ai_threads_agent_profile_id")
        batch_op.drop_constraint("ck_ai_thread_v2_policy_matches_type", type_="check")
        batch_op.drop_constraint("ck_ai_thread_v2_thread_policy", type_="check")
        batch_op.drop_constraint("ck_ai_thread_v2_route_snapshot", type_="check")
        batch_op.drop_constraint("fk_ai_thread_agent_profile", type_="foreignkey")
        batch_op.drop_column("thread_policy")
        batch_op.drop_column("agent_profile_id")

    op.drop_index(
        "ix_conversation_agent_profile_bindings_conversation_id",
        table_name="conversation_agent_profile_bindings",
    )
    op.drop_index(
        "ix_conversation_agent_profile_bindings_agent_profile_id",
        table_name="conversation_agent_profile_bindings",
    )
    op.drop_table("conversation_agent_profile_bindings")
