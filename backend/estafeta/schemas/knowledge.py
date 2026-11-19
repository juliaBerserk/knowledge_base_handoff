from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from estafeta.domain.access import KnowledgeCategory


class KnowledgeRecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: KnowledgeCategory
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=1_000_000)
    source_material_id: UUID | None = None


class KnowledgeRecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: KnowledgeCategory
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=1_000_000)
    source_material_id: UUID | None = None


class KnowledgeRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    original_author_id: UUID | None
    copied_from_id: UUID | None
    source_material_id: UUID | None
    category: KnowledgeCategory
    title: str
    content: str
    scope: str
    status: str
    position_id: UUID | None
    created_at: datetime
    updated_at: datetime


class KnowledgeRecordListResponse(BaseModel):
    items: list[KnowledgeRecordResponse]
