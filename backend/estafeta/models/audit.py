from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models.base import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_occurred_id", "occurred_at", "id"),
        Index("ix_audit_events_actor_id", "actor_user_id", "id"),
        Index("ix_audit_events_type_id", "event_type", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    actor_role: Mapped[str | None] = mapped_column(String(32))
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    metadata_json: Mapped[dict[str, str | int] | None] = mapped_column(
        "metadata",
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
