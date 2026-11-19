from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import FetchedValue

from estafeta.models.base import Base

EMBEDDING_DIMENSIONS = 1024


class BuildJob(Base):
    __tablename__ = "build_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')",
            name="ck_build_jobs_status",
        ),
        CheckConstraint(
            "stage IN ('queued', 'loading', 'chunking', 'embedding', 'extracting', "
            "'complete', 'failed')",
            name="ck_build_jobs_stage",
        ),
        CheckConstraint("total_count >= 0", name="ck_build_jobs_total_nonnegative"),
        CheckConstraint("processed_count >= 0", name="ck_build_jobs_processed_nonnegative"),
        CheckConstraint("failed_count >= 0", name="ck_build_jobs_failed_nonnegative"),
        Index("ix_build_jobs_owner_created", "requested_by", "created_at"),
        Index(
            "ix_build_jobs_pending_created",
            "created_at",
            postgresql_where=sql_text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    requested_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    stage: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("material_version_id", "ordinal", name="uq_chunks_version_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal_nonnegative"),
        CheckConstraint("length(text) > 0", name="ck_chunks_text_not_empty"),
        CheckConstraint(
            "char_start >= 0 AND char_end > char_start",
            name="ck_chunks_source_offsets",
        ),
        Index("ix_chunks_version_current", "material_version_id", "is_current"),
        Index("ix_chunks_current_material", "material_id", "is_current", "ordinal"),
        Index(
            "ix_chunks_search_vector_gin",
            "search_vector",
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=sql_text("is_current"),
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    material_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("materials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    material_version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("material_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite"),
        server_default=FetchedValue(),
        nullable=True,
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS).with_variant(JSON(), "sqlite"),
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RecordSource(Base):
    __tablename__ = "record_sources"
    __table_args__ = (
        UniqueConstraint("record_id", "chunk_id", name="uq_record_sources_record_chunk"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_record_sources_confidence",
        ),
        Index("ix_record_sources_chunk", "chunk_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    record_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
