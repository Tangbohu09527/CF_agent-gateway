"""Private per-claim host bindings. No historical message or dispatch backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20260928_01"
down_revision = "20260927_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "inbound_host_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "dispatch_id", sa.Integer(), sa.ForeignKey("hermes_dispatch_records.id"), nullable=False
        ),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("inbound_media_jobs.id"), nullable=False),
        sa.Column("ai_thread_id", sa.String(), sa.ForeignKey("ai_threads.id"), nullable=False),
        sa.Column("claim_token_hash", sa.String(64), nullable=False),
        sa.Column("claim_epoch", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("parent_session_id", sa.String(255)),
        sa.Column("profile_reference", sa.String(255), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("host_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_ciphertext", sa.LargeBinary()),
        sa.Column("host_instance_id", sa.String(128)),
        sa.Column("host_nonce_hash", sa.String(64)),
        sa.Column("task_id", sa.String(128)),
        sa.Column("host_lease_until", sa.DateTime(timezone=True)),
        sa.Column("events_connected", sa.Boolean(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("preparation_started_at", sa.DateTime(timezone=True)),
        sa.Column("history_digest", sa.String(64)),
        sa.Column("runtime_config_digest", sa.String(64)),
        sa.UniqueConstraint("session_id", name="uq_inbound_host_session"),
        sa.UniqueConstraint("dispatch_id", "claim_token_hash", name="uq_inbound_host_claim"),
    )
    op.create_index(
        "ix_inbound_host_bindings_dispatch_id", "inbound_host_bindings", ["dispatch_id"]
    )
    op.create_index(
        "ix_inbound_host_bindings_ai_thread_id", "inbound_host_bindings", ["ai_thread_id"]
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM inbound_host_bindings")):
        raise RuntimeError("cannot discard persisted host bindings")
    op.drop_table("inbound_host_bindings")
