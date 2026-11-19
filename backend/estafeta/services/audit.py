from uuid import UUID

from sqlalchemy.orm import Session

from estafeta.models import AuditEvent
from estafeta.schemas.audit import AuditEventType, AuditMetadata

_METADATA_FIELDS: dict[AuditEventType, frozenset[str]] = {
    AuditEventType.USER_CREATED: frozenset({"target_role"}),
    AuditEventType.CSV_IMPORT_COMPLETED: frozenset({"imported_count"}),
    AuditEventType.MATERIAL_REDACTION_RECORDED: frozenset({"finding_type", "redacted_count"}),
    AuditEventType.RECORD_REDACTION_RECORDED: frozenset({"finding_type", "redacted_count"}),
}
_SUBJECT_TYPES = frozenset(
    {"user", "session", "material", "record", "chunk", "chat", "answer", "handoff", "job"}
)


def record_audit_event(
    session: Session,
    event_type: AuditEventType,
    *,
    actor_user_id: UUID | None,
    actor_role: str | None,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    metadata: AuditMetadata | None = None,
) -> AuditEvent:
    if subject_type is not None and subject_type not in _SUBJECT_TYPES:
        raise ValueError("Недопустимый тип объекта аудита")
    safe_metadata = metadata or AuditMetadata()
    allowed_fields = _METADATA_FIELDS.get(event_type, frozenset())
    supplied_fields = safe_metadata.model_dump(mode="json", exclude_none=True)
    unexpected_fields = supplied_fields.keys() - allowed_fields
    if unexpected_fields:
        raise ValueError(f"Недопустимые метаданные для события {event_type.value}")
    event = AuditEvent(
        event_type=event_type.value,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        subject_type=subject_type,
        subject_id=subject_id,
        metadata_json=supplied_fields,
    )
    session.add(event)
    return event
