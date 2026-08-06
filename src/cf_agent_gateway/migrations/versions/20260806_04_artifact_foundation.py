"""Add the Hermes Artifact V2 foundation.

Revision ID: 20260806_04
Revises: 20260806_03
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_04"
down_revision: str | Sequence[str] | None = "20260806_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("response_id", sa.String(length=255), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "image",
                "file",
                name="artifact_kind",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "ready",
                "failed",
                "expired",
                name="artifact_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="created",
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('image', 'file')",
            name="artifact_kind",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'ready', 'failed', 'expired')",
            name="artifact_status",
        ),
        sa.CheckConstraint(
            "status != 'ready' OR (size IS NOT NULL AND sha256 IS NOT NULL)",
            name="ck_artifact_ready_metadata",
        ),
        sa.CheckConstraint(
            "size IS NULL OR size >= 0",
            name="ck_artifact_size_nonnegative",
        ),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint("storage_key", name="uq_artifact_storage_key"),
    )
    op.create_index(
        "ix_artifact_response_id",
        "artifacts",
        ["response_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_response_id", table_name="artifacts")
    op.drop_table("artifacts")
