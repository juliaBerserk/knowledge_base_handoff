from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models import Base, Identified


class BuildJob(Identified, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')", name="ck_job_status"
        ),
        sa.Index(
            "uq_build_job_active_owner",
            "owner_id",
            unique=True,
            postgresql_where=sa.text("status IN ('queued','running')"),
        ),
        sa.Index(
            "ix_build_job_queue",
            "created_at",
            "id",
            postgresql_where=sa.text("status IN ('queued','running')"),
        ),
        sa.Index("ix_build_job_owner_history", "owner_id", "created_at"),
    )

    owner_id: Mapped[UUID] = mapped_column(sa.ForeignKey("employees.id"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    status: Mapped[str] = mapped_column(sa.String(12), server_default="queued")
    stage: Mapped[str] = mapped_column(sa.String(16), server_default="queued")
    lease: Mapped[UUID | None]
    leased_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(server_default="0")
    progress: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=sa.text("'{}'"))


class Chunk(Identified, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        sa.Index("ix_chunks_material_active", "material_id", "active"),
        sa.Index(
            "ix_chunks_fts",
            "search_vector",
            postgresql_using="gin",
            postgresql_where=sa.text("active"),
        ),
        sa.Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=sa.text("active"),
        ),
    )

    material_id: Mapped[UUID] = mapped_column(sa.ForeignKey("materials.id", ondelete="CASCADE"))
    ordinal: Mapped[int]
    text: Mapped[str] = mapped_column(sa.Text)
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    source_hash: Mapped[str] = mapped_column(sa.String(64))
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    active: Mapped[bool] = mapped_column(server_default=sa.true())
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    embedding_key: Mapped[str | None] = mapped_column(sa.String(64))
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, sa.Computed("to_tsvector('russian'::regconfig, text)", persisted=True)
    )
