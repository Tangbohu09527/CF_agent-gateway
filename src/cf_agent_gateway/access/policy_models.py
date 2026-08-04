from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from cf_agent_gateway.database import Base

DEFAULT_GATEWAY_POLICY_KEY = "default"


class SortedStringSet(TypeDecorator[frozenset[str]]):
    """Persist an immutable string set as a deterministic JSON array."""

    impl = JSON
    cache_ok = True

    def process_bind_param(
        self, value: Iterable[str | Enum] | None, dialect: Dialect
    ) -> list[str] | None:
        del dialect
        if value is None:
            return None
        normalized = {str(item.value) if isinstance(item, Enum) else str(item) for item in value}
        return sorted(normalized)

    def process_result_value(
        self, value: list[str] | None, dialect: Dialect
    ) -> frozenset[str] | None:
        del dialect
        if value is None:
            return None
        return frozenset(value)

    @property
    def python_type(self) -> type[frozenset[str]]:
        return frozenset


class UserAccessPolicy(Base):
    __tablename__ = "user_access_policies"
    __table_args__ = (
        UniqueConstraint(
            "enterprise_identity_id",
            name="uq_user_access_policy_enterprise_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    enterprise_identity_id: Mapped[str] = mapped_column(
        ForeignKey("enterprise_identities.id", ondelete="RESTRICT"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    permission_scope: Mapped[frozenset[str]] = mapped_column(
        SortedStringSet(), default=frozenset, nullable=False
    )
    allowed_skills: Mapped[frozenset[str]] = mapped_column(
        SortedStringSet(), default=frozenset, nullable=False
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GatewayAccessPolicy(Base):
    __tablename__ = "gateway_access_policies"
    __table_args__ = (
        UniqueConstraint("policy_key", name="uq_gateway_access_policy_key"),
        CheckConstraint(
            "policy_key = 'default'",
            name="ck_gateway_access_policy_default_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_key: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_GATEWAY_POLICY_KEY, server_default=DEFAULT_GATEWAY_POLICY_KEY
    )
    permission_scope: Mapped[frozenset[str]] = mapped_column(
        SortedStringSet(), default=frozenset, nullable=False
    )
    allowed_skills: Mapped[frozenset[str]] = mapped_column(
        SortedStringSet(), default=frozenset, nullable=False
    )
    allowed_risk_levels: Mapped[frozenset[str]] = mapped_column(
        SortedStringSet(), default=frozenset, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
