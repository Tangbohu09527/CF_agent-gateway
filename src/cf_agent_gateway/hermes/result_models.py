from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from cf_agent_gateway.database import Base


class HermesDispatchResponse(Base):
    __tablename__ = "hermes_dispatch_responses"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_record_id",
            name="uq_hermes_dispatch_response_dispatch_record",
        ),
        UniqueConstraint(
            "hermes_response_id",
            name="uq_hermes_dispatch_response_hermes_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_record_id: Mapped[int] = mapped_column(
        ForeignKey("hermes_dispatch_records.id", ondelete="RESTRICT"),
        index=True,
    )
    hermes_response_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assistant_content: Mapped[str] = mapped_column(Text)
    response_payload: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
