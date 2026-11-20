from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models import Base, Identified


class Chat(Identified, Base):
    __tablename__ = "chats"
    __table_args__ = (sa.Index("ix_chats_owner_created", "owner_id", "created_at", "id"),)

    owner_id: Mapped[UUID] = mapped_column(sa.ForeignKey("employees.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ChatTurn(Identified, Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_chat_turn_rating"),
        sa.CheckConstraint("scope IN ('personal','position','department')", name="ck_chat_scope"),
        sa.Index("ix_chat_turns_chat_created", "chat_id", "created_at", "id"),
    )

    chat_id: Mapped[UUID] = mapped_column(sa.ForeignKey("chats.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    question: Mapped[str] = mapped_column(sa.String(1000))
    scope: Mapped[str] = mapped_column(sa.String(12))
    segments: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(sa.Text)
    prompt_version: Mapped[str] = mapped_column(sa.String(40))
    rating: Mapped[int | None] = mapped_column(sa.SmallInteger)
    comment: Mapped[str | None] = mapped_column(sa.String(1000))
