from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

BuildJobStatus = Literal[
    "pending",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
]
BuildJobStage = Literal[
    "queued",
    "loading",
    "chunking",
    "embedding",
    "extracting",
    "complete",
    "failed",
]


class BuildJobResponse(BaseModel):
    id: UUID
    status: BuildJobStatus
    stage: BuildJobStage
    total_count: int
    processed_count: int
    failed_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class BuildJobListResponse(BaseModel):
    items: list[BuildJobResponse]
