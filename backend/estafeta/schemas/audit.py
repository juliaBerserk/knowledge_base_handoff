from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from estafeta.domain.access import Role


class AuditEventType(StrEnum):
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    INVITATION_ACCEPTED = "auth.invitation_accepted"
    TOTP_SETUP_STARTED = "auth.totp_setup_started"
    TOTP_VERIFICATION_FAILED = "auth.totp_verification_failed"
    TOTP_VERIFIED = "auth.totp_verified"
    TOTP_ENABLED = "auth.totp_enabled"
    USER_CREATED = "admin.user_created"
    CSV_IMPORT_COMPLETED = "admin.csv_import_completed"
    RECOVERY_INVITATION_CREATED = "admin.recovery_invitation_created"
    ACCOUNT_UNLOCKED = "admin.account_unlocked"
    BOOTSTRAP_ADMIN_CREATED = "admin.bootstrap_created"
    SANITIZATION_POLICY_UPDATED = "admin.sanitization_policy_updated"
    MATERIAL_CREATED = "material.created"
    MATERIAL_REDACTION_RECORDED = "material.redaction_recorded"
    MATERIAL_SUBMITTED = "material.submitted"
    MATERIAL_PUBLISHED = "material.published"
    MATERIAL_ARCHIVED = "material.archived"
    KNOWLEDGE_SPACE_VIEWED = "knowledge.space_viewed"
    KNOWLEDGE_ITEM_VIEWED = "knowledge.item_viewed"
    RECORD_CREATED = "record.created"
    RECORD_UPDATED = "record.updated"
    RECORD_SUBMITTED = "record.submitted"
    RECORD_PUBLISHED = "record.published"
    RECORD_ARCHIVED = "record.archived"
    RECORD_COPIED = "record.copied"
    RECORD_REDACTION_RECORDED = "record.redaction_recorded"
    BUILD_JOB_CREATED = "build_job.created"
    BUILD_JOB_COMPLETED = "build_job.completed"


class AuditMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    imported_count: int | None = Field(default=None, ge=1, le=500)
    target_role: Role | None = None
    finding_type: (
        Literal[
            "password",
            "token",
            "api_key",
            "private_key",
            "db_connection",
            "credit_card",
            "passport",
            "snils",
            "inn",
            "phone",
            "external_email",
            "person",
            "address",
            "custom",
        ]
        | None
    ) = None
    redacted_count: int | None = Field(default=None, ge=1, le=100_000)


class AuditEventResponse(BaseModel):
    id: int
    occurred_at: datetime
    event_type: AuditEventType
    actor_user_id: UUID | None
    actor_role: Role | None
    subject_type: str | None
    subject_id: UUID | None
    metadata: AuditMetadata


class AuditEventPage(BaseModel):
    items: list[AuditEventResponse]
    next_before_id: int | None
