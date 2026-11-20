from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models import Base, Identified


class Handoff(Identified, Base):
    __tablename__ = "handoffs"
    __table_args__ = (
        sa.CheckConstraint("kind IN ('departure','transfer')", name="ck_handoff_kind"),
        sa.CheckConstraint(
            "status IN ('scheduled','review','completed','conflict','cancelled')",
            name="ck_handoff_status",
        ),
        sa.CheckConstraint(
            "(kind = 'departure' AND target_position_id IS NULL) OR "
            "(kind = 'transfer' AND target_position_id IS NOT NULL AND successor_id IS NULL)",
            name="ck_handoff_kind_fields",
        ),
        sa.CheckConstraint("retention IN ('archive','delete')", name="ck_handoff_retention"),
        sa.Index(
            "uq_handoff_scheduled_employee",
            "employee_id",
            unique=True,
            postgresql_where=sa.text("status = 'scheduled'"),
        ),
        sa.Index("ix_handoff_schedule", "status", "effective_at"),
        sa.Index("ix_handoff_review", "status", "review_until"),
        sa.Index("ix_handoff_department", "department_id", "effective_at"),
        sa.Index("ix_handoff_successor", "successor_id", "effective_at"),
    )

    kind: Mapped[str] = mapped_column(sa.String(12))
    status: Mapped[str] = mapped_column(sa.String(12), server_default="scheduled")
    employee_id: Mapped[UUID] = mapped_column(sa.ForeignKey("employees.id"))
    employee_label: Mapped[str] = mapped_column(sa.String(255))
    origin_assignment_id: Mapped[UUID] = mapped_column(sa.ForeignKey("position_assignments.id"))
    position_id: Mapped[UUID] = mapped_column(sa.ForeignKey("positions.id"))
    position_label: Mapped[str] = mapped_column(sa.String(200))
    department_id: Mapped[UUID] = mapped_column(sa.ForeignKey("departments.id"))
    successor_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("employees.id"))
    successor_label: Mapped[str | None] = mapped_column(sa.String(255))
    successor_assignment_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("position_assignments.id")
    )
    target_position_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("positions.id"))
    created_by: Mapped[UUID] = mapped_column(sa.ForeignKey("employees.id"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    effective_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    review_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    retention: Mapped[str] = mapped_column(sa.String(8), server_default="archive")
    successor_assigned: Mapped[bool] = mapped_column(server_default=sa.false())
    issue: Mapped[str | None] = mapped_column(sa.String(32))
    final_counts: Mapped[dict[str, int] | None] = mapped_column(JSONB)
