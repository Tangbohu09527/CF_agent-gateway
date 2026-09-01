"""Add WeChat checkpoint generation and continuity anchor.

Revision ID: 20260823_01
Revises: 20260810_01
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260823_01"
down_revision: str | Sequence[str] | None = "20260810_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "wechat_sync_checkpoints"
_NEW_COLUMNS = frozenset(
    {
        "regression_generation",
        "last_message_fingerprint",
    }
)


def upgrade() -> None:
    _require_column_state(expected_present=False)
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "regression_generation",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_message_fingerprint",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_wechat_sync_checkpoint_nonnegative_generation",
            "regression_generation >= 0",
        )
        batch_op.create_check_constraint(
            "ck_wechat_sync_checkpoint_fingerprint_length",
            "last_message_fingerprint IS NULL OR length(last_message_fingerprint) = 64",
        )


def downgrade() -> None:
    _require_column_state(expected_present=True)
    _require_downgrade_data_safe()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_wechat_sync_checkpoint_fingerprint_length",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_wechat_sync_checkpoint_nonnegative_generation",
            type_="check",
        )
        batch_op.drop_column("last_message_fingerprint")
        batch_op.drop_column("regression_generation")


def _require_column_state(*, expected_present: bool) -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        raise RuntimeError("WeChat checkpoint table is missing")
    existing = {column["name"] for column in inspector.get_columns(_TABLE)}
    present = existing & _NEW_COLUMNS
    if expected_present and present != _NEW_COLUMNS:
        raise RuntimeError("partial WeChat checkpoint generation schema detected")
    if not expected_present and present:
        raise RuntimeError("partial WeChat checkpoint generation schema detected")


def _require_downgrade_data_safe() -> None:
    if context.is_offline_mode():
        return
    evidence_count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM wechat_sync_checkpoints "
            "WHERE regression_generation <> 0 "
            "OR last_message_fingerprint IS NOT NULL"
        )
    )
    if evidence_count:
        raise RuntimeError(
            "checkpoint regression evidence exists; restore the pre-upgrade backup "
            "to roll back; never delete generation or anchor evidence"
        )
