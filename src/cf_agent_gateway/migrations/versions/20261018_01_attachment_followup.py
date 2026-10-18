"""New operation jobs reference registered originals; no backfill or replay."""

import sqlalchemy as sa
from alembic import op

revision = "20261018_01"
down_revision = "20261016_01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("inbound_media_jobs") as batch:
        batch.add_column(sa.Column("source_job_id", sa.Integer()))
        batch.add_column(sa.Column("followup_action", sa.String(16)))
        batch.create_foreign_key(
            "fk_inbound_followup_source",
            "inbound_media_jobs",
            ["source_job_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_inbound_followup_scope",
            "(source_job_id IS NULL AND followup_action IS NULL) OR "
            "(source_job_id IS NOT NULL AND source_job_id != id "
            "AND followup_action IS NOT NULL AND "
            "followup_action IN ('read', 'read-return'))",
        )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM inbound_media_jobs WHERE source_job_id IS NOT NULL")
    ):
        raise RuntimeError("cannot discard persisted attachment follow-up")
    with op.batch_alter_table("inbound_media_jobs") as batch:
        batch.drop_constraint("ck_inbound_followup_scope", type_="check")
        batch.drop_constraint("fk_inbound_followup_source", type_="foreignkey")
        batch.drop_column("followup_action")
        batch.drop_column("source_job_id")
