"""Opt-in attachment execution lineage; no legacy backfill or task replay."""

import sqlalchemy as sa
from alembic import op

revision = "20261016_01"
down_revision = "20260928_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("inbound_host_bindings", sa.Column("execution_policy", sa.String(32)))
    op.add_column("inbound_host_bindings", sa.Column("previous_session_id", sa.String(255)))
    with op.batch_alter_table("inbound_host_bindings") as batch:
        batch.add_column(sa.Column("instruction_message_id", sa.Integer()))
        batch.create_foreign_key(
            "fk_inbound_execution_instruction", "messages", ["instruction_message_id"], ["id"]
        )
        batch.add_column(sa.Column("instruction_digest", sa.String(64)))


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM inbound_host_bindings WHERE execution_policy IS NOT NULL")
    ):
        raise RuntimeError("cannot discard persisted execution lineage")
    with op.batch_alter_table("inbound_host_bindings") as batch:
        batch.drop_constraint("fk_inbound_execution_instruction", type_="foreignkey")
        batch.drop_column("instruction_digest")
        batch.drop_column("instruction_message_id")
        batch.drop_column("previous_session_id")
        batch.drop_column("execution_policy")
