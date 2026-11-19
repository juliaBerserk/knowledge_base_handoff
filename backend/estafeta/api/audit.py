from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from estafeta.api.auth import AuthRoute, reply, session_token
from estafeta.auth.service import load_session
from estafeta.core.config import get_settings
from estafeta.core.db import get_session_factory
from estafeta.models import Role
from estafeta.models.audit import Action, AuditEvent

router = APIRouter(prefix="/audit", tags=["audit"], route_class=AuthRoute)


@router.get("/events")
def list_events(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    action: Action | None = None,
) -> JSONResponse:
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    with get_session_factory().begin() as db:
        active = load_session(db, get_settings(), token, datetime.now(UTC))
        if active is None:
            return reply(401, detail="Сессия завершена. Войдите снова")
        if active.session.enrollment_only or active.principal.role is not Role.SECURITY_AUDITOR:
            return reply(403, detail="Журнал доступен только аудитору информационной безопасности")
        db.execute(
            sa.text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": str(active.principal.employee_id)},
        )
        table = AuditEvent.__table__
        statement = table.select().order_by(table.c.id.desc()).limit(limit + 1)
        if before_id is not None:
            statement = statement.where(table.c.id < before_id)
        if action is not None:
            statement = statement.where(table.c.action == action)
        rows = db.execute(statement).mappings().all()
        page = [dict(row) for row in rows[:limit]]
        next_before_id = page[-1]["id"] if len(rows) > limit else None
        return reply(events=jsonable_encoder(page), next_before_id=next_before_id)
