from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models import Base, enum_type


class Action(StrEnum):
    LOGIN = "login"
    LOGOUT = "logout"
    PASSWORD_SET = "password_set"
    PASSWORD_CHANGED = "password_changed"
    TOTP_ENABLED = "totp_enabled"
    INVITATION_ISSUED = "invitation_issued"
    ADMIN_BOOTSTRAPPED = "admin_bootstrapped"
    KNOWLEDGE_VIEWED = "knowledge_viewed"
    MATERIAL_UPLOADED = "material_uploaded"
    KNOWLEDGE_PUBLISHED = "knowledge_published"
    KNOWLEDGE_COPIED = "knowledge_copied"
    KNOWLEDGE_EXPORTED = "knowledge_exported"
    AI_QUESTION = "ai_question"


class Outcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    THROTTLED = "throttled"
    CHALLENGE = "challenge"


class Target(StrEnum):
    EMPLOYEE = "employee"
    MATERIAL = "material"
    RECORD = "record"
    SPACE = "space"
    CHAT = "chat"


class AuditEvent(Base):
    __tablename__ = "events"
    __table_args__ = (
        sa.CheckConstraint(
            "(target_kind IS NULL) = (target_id IS NULL)", name="ck_event_target_pair"
        ),
        sa.Index("ix_audit_events_action_id", "action", "id"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp()
    )
    action: Mapped[Action] = mapped_column(enum_type(Action, "ck_event_action"))
    outcome: Mapped[Outcome] = mapped_column(enum_type(Outcome, "ck_event_outcome"))
    actor_id: Mapped[UUID | None]
    target_kind: Mapped[Target | None] = mapped_column(enum_type(Target, "ck_event_target"))
    target_id: Mapped[UUID | None]
