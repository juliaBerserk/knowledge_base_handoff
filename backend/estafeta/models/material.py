from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from estafeta.models.base import Base


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        CheckConstraint("length(trim(title)) > 0", name="ck_materials_title_not_empty"),
        CheckConstraint(
            "(scope = 'personal' AND position_id IS NULL AND status = 'draft') OR "
            "(scope = 'position' AND position_id IS NOT NULL AND "
            "status IN ('in_review', 'published', 'archived'))",
            name="ck_materials_scope_status",
        ),
        Index("ix_materials_owner_updated", "owner_id", "updated_at"),
        Index("ix_materials_position_status", "position_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="RESTRICT"),
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="personal")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MaterialVersion(Base):
    __tablename__ = "material_versions"
    __table_args__ = (
        UniqueConstraint("material_id", "version_number", name="uq_material_versions_number"),
        CheckConstraint("version_number >= 1", name="ck_material_versions_number_positive"),
        CheckConstraint(
            "source_kind IN ('note', 'txt', 'md', 'pdf', 'docx')",
            name="ck_material_versions_source_kind",
        ),
        CheckConstraint("length(cleaned_text) > 0", name="ck_material_versions_text_not_empty"),
        Index("ix_material_versions_material_created", "material_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    material_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("materials.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    cleaned_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    sanitizer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RedactionEvent(Base):
    __tablename__ = "redaction_events"
    __table_args__ = (
        UniqueConstraint("version_id", "finding_type", name="uq_redaction_events_type"),
        CheckConstraint("redacted_count > 0", name="ck_redaction_events_count_positive"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    version_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("material_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    finding_type: Mapped[str] = mapped_column(String(32), nullable=False)
    redacted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SanitizationPolicy(Base):
    __tablename__ = "sanitization_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    custom_patterns: Mapped[list[dict[str, str]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'[]'"),
    )
    custom_terms: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'[]'"),
    )
    corporate_domains: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        server_default=text("'[]'"),
    )
    updated_by: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class KnowledgeRecord(Base):
    __tablename__ = "knowledge_records"
    __table_args__ = (
        CheckConstraint(
            "scope = 'personal' AND position_id IS NULL AND status = 'draft' "
            "OR scope = 'position' AND position_id IS NOT NULL "
            "AND status IN ('in_review', 'published', 'archived')",
            name="ck_knowledge_records_scope_status",
        ),
        CheckConstraint(
            "category IN ('responsibilities', 'processes', 'cases', 'people', "
            "'systems', 'decisions', 'unfinished', 'faqs', 'glossary', 'risks')",
            name="ck_knowledge_records_category",
        ),
        CheckConstraint("length(trim(title)) > 0", name="ck_knowledge_records_title"),
        CheckConstraint("length(content) > 0", name="ck_knowledge_records_content"),
        CheckConstraint(
            "record_origin IN ('manual', 'extracted', 'copied')",
            name="ck_knowledge_records_origin",
        ),
        Index("ix_knowledge_records_owner_updated", "owner_id", "updated_at"),
        Index("ix_knowledge_records_position_status", "position_id", "status"),
        Index("ix_knowledge_records_current_scope", "scope", "status", "is_current"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="RESTRICT"),
    )
    source_material_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("materials.id", ondelete="SET NULL"),
    )
    copied_from_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("knowledge_records.id", ondelete="SET NULL"),
    )
    original_author_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
    )
    source_material_version_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("material_versions.id", ondelete="SET NULL"),
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="personal")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    record_origin: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="manual"
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_manually_edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    confidence: Mapped[float | None] = mapped_column()
    extraction_model: Mapped[str | None] = mapped_column(String(200))
    extraction_prompt_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
