from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models import Base

AUTH_SCHEMA = "auth"


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = {"schema": AUTH_SCHEMA}

    employee_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("knowledge.employees.id"), primary_key=True
    )
    password_hash: Mapped[str | None]
    blocked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(default=0, server_default="0")
    totp_secret: Mapped[bytes | None]
    totp_last_step: Mapped[int | None] = mapped_column(sa.BigInteger)


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = {"schema": AUTH_SCHEMA}

    token_hash: Mapped[bytes] = mapped_column(primary_key=True)
    employee_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("auth.credentials.employee_id", ondelete="CASCADE"), unique=True
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": AUTH_SCHEMA}

    token_hash: Mapped[bytes] = mapped_column(primary_key=True)
    employee_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("auth.credentials.employee_id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    enrollment_only: Mapped[bool]
    pending_totp: Mapped[bytes | None]


class RateLimit(Base):
    __tablename__ = "rate_limits"
    __table_args__ = {"schema": AUTH_SCHEMA}

    key: Mapped[bytes] = mapped_column(primary_key=True)
    window: Mapped[int] = mapped_column(sa.BigInteger, index=True)
    hits: Mapped[int]
