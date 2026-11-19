from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from estafeta.api.dependencies import Principal, get_db_session, require_security_auditor
from estafeta.models import AuditEvent
from estafeta.schemas.audit import (
    AuditEventPage,
    AuditEventResponse,
    AuditEventType,
    AuditMetadata,
)

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events", response_model=AuditEventPage)
def list_audit_events(
    event_type: AuditEventType | None = None,
    actor_user_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    _: Principal = Depends(require_security_auditor),
    session: Session = Depends(get_db_session),
) -> AuditEventPage:
    if (since is not None and since.tzinfo is None) or (until is not None and until.tzinfo is None):
        raise HTTPException(status_code=422, detail="Укажите часовой пояс для даты")
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=422, detail="Начало периода позже его окончания")

    statement = select(AuditEvent)
    if event_type is not None:
        statement = statement.where(AuditEvent.event_type == event_type.value)
    if actor_user_id is not None:
        statement = statement.where(AuditEvent.actor_user_id == actor_user_id)
    if since is not None:
        statement = statement.where(AuditEvent.occurred_at >= since)
    if until is not None:
        statement = statement.where(AuditEvent.occurred_at <= until)
    if before_id is not None:
        statement = statement.where(AuditEvent.id < before_id)

    rows = list(session.scalars(statement.order_by(AuditEvent.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    items = [
        AuditEventResponse(
            id=row.id,
            occurred_at=row.occurred_at,
            event_type=row.event_type,
            actor_user_id=row.actor_user_id,
            actor_role=row.actor_role,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            metadata=AuditMetadata.model_validate(row.metadata_json or {}, strict=False),
        )
        for row in rows[:limit]
    ]
    return AuditEventPage(
        items=items,
        next_before_id=items[-1].id if has_more and items else None,
    )
