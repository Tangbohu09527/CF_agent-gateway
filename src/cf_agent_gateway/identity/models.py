from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint, func, true
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class IdentityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


class EnterpriseIdentity(Base):
    __tablename__ = "enterprise_identities"
    __table_args__ = (UniqueConstraint("employee_id", name="uq_enterprise_identity_employee_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    employee_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[IdentityStatus] = mapped_column(
        Enum(
            IdentityStatus,
            values_callable=_enum_values,
            native_enum=False,
            validate_strings=True,
            name="identity_status",
        ),
        default=IdentityStatus.ACTIVE,
        server_default=IdentityStatus.ACTIVE.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SourceIdentityMapping(Base):
    __tablename__ = "source_identity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "account_id",
            "sender_id",
            name="uq_source_identity_platform_account_sender",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(255))
    sender_id: Mapped[str] = mapped_column(String(255))
    enterprise_identity_id: Mapped[str] = mapped_column(
        ForeignKey("enterprise_identities.id", ondelete="RESTRICT"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
