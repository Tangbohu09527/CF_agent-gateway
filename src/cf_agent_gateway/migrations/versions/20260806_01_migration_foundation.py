"""Establish the migration schema version.

Revision ID: 20260806_01
Revises:
Create Date: 2026-08-06
"""

from collections.abc import Sequence

revision: str = "20260806_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record the baseline revision without changing business tables."""


def downgrade() -> None:
    """Remove the baseline revision without changing business tables."""
