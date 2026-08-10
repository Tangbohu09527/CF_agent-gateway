"""Add versioned Context Snapshot persistence.

Revision ID: 20260810_01
Revises: 20260807_03
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_01"
down_revision: str | Sequence[str] | None = "20260807_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_snapshots",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("covered_until", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "snapshot_version > 0",
            name="ck_context_snapshot_positive_version",
        ),
        sa.CheckConstraint(
            "length(trim(summary)) > 0",
            name="ck_context_snapshot_nonempty_summary",
        ),
        sa.CheckConstraint(
            "covered_until > 0",
            name="ck_context_snapshot_positive_covered_until",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["ai_threads.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("thread_id", "snapshot_version"),
    )
    op.create_index(
        "ix_hermes_dispatch_context_timeline",
        "hermes_dispatch_records",
        ["ai_thread_id", "status", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hermes_dispatch_context_timeline",
        table_name="hermes_dispatch_records",
    )
    op.drop_table("context_snapshots")
