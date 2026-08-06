from __future__ import annotations

from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, Enum, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class ArtifactKind(StrEnum):
    IMAGE = "image"
    FILE = "file"


class ArtifactStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_artifact_storage_key"),
        CheckConstraint("kind IN ('image', 'file')", name="artifact_kind"),
        CheckConstraint(
            "status IN ('created', 'ready', 'failed', 'expired')",
            name="artifact_status",
        ),
        CheckConstraint("size IS NULL OR size >= 0", name="ck_artifact_size_nonnegative"),
        CheckConstraint(
            "status != 'ready' OR (size IS NOT NULL AND sha256 IS NOT NULL)",
            name="ck_artifact_ready_metadata",
        ),
        Index("ix_artifact_response_id", "response_id"),
    )

    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(255))
    kind: Mapped[ArtifactKind] = mapped_column(
        Enum(
            ArtifactKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="artifact_kind",
        )
    )
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(
            ArtifactStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            name="artifact_status",
        ),
        default=ArtifactStatus.CREATED,
        server_default=ArtifactStatus.CREATED.value,
    )
