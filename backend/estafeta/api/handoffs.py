from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, model_validator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from estafeta.api.auth import AuthRoute, csrf_valid, reply, session_token
from estafeta.core.audit import append_event
from estafeta.core.config import get_settings
from estafeta.core.knowledge_access import account_transaction
from estafeta.core.permissions import Relation, can_manage_handoffs, can_read
from estafeta.indexing.extraction import Category
from estafeta.models import KnowledgeRecord, KnowledgeSpace, KnowledgeStatus, Material, Role
from estafeta.models.audit import Action, Target
from estafeta.models.handoff import Handoff

router = APIRouter(prefix="/handoffs", tags=["handoffs"], route_class=AuthRoute)
ROLES = frozenset({Role.EMPLOYEE, Role.DEPARTMENT_HEAD, Role.HR})
MANAGERS = frozenset(role for role in ROLES if can_manage_handoffs(role))
PAGE_SIZE = 30


class ScheduleBody(BaseModel):
    employee_id: UUID
    position_id: UUID
    effective_at: AwareDatetime
    successor_id: UUID | None = None
    target_position_id: UUID | None = None

    @model_validator(mode="after")
    def validate_participants(self) -> "ScheduleBody":
        if self.successor_id is not None and self.target_position_id is not None:
            raise ValueError("При переводе преемник не указывается")
        if self.successor_id == self.employee_id or self.target_position_id == self.position_id:
            raise ValueError("Сотрудник и преемник или должности должны различаться")
        return self


def token_for_post(request: Request) -> str | JSONResponse:
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    if not csrf_valid(request, get_settings(), token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    return token


@router.post("")
def schedule(request: Request, body: ScheduleBody) -> JSONResponse:
    token = token_for_post(request)
    if isinstance(token, JSONResponse):
        return token
    with account_transaction(token, MANAGERS) as db:
        if db is None:
            return reply(403, detail="Создать эстафету может HR или руководитель отдела")
        kind = "transfer" if body.target_position_id is not None else "departure"
        created = db.scalar(
            sa.select(
                sa.func.knowledge.schedule_handoff(
                    kind,
                    body.employee_id,
                    body.position_id,
                    body.successor_id,
                    body.target_position_id,
                    body.effective_at,
                    get_settings().handoff_retention,
                )
            )
        )
        if created is None:
            return reply(
                409,
                detail="Не удалось запланировать. Проверьте дату, назначения и права доступа",
            )
        return reply(201, id=str(created), status="scheduled")


@router.post("/{handoff_id}/cancel")
def cancel(request: Request, handoff_id: UUID) -> JSONResponse:
    token = token_for_post(request)
    if isinstance(token, JSONResponse):
        return token
    with account_transaction(token, MANAGERS) as db:
        if db is None or not db.scalar(sa.select(sa.func.knowledge.cancel_handoff(handoff_id))):
            return reply(409, detail="Отмена недоступна или срок уже наступил")
        return reply(status="cancelled")


def metadata(item: Handoff) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "employee_id": item.employee_id,
        "employee_name": item.employee_label,
        "position_id": item.position_id,
        "position_title": item.position_label,
        "successor_id": item.successor_id,
        "successor_name": item.successor_label,
        "target_position_id": item.target_position_id,
        "successor_assigned": item.successor_assigned,
        "effective_at": item.effective_at,
        "review_until": item.review_until,
        "issue": item.issue,
    }


@router.get("")
def list_handoffs(request: Request, before: UUID | None = None) -> JSONResponse:
    with account_transaction(session_token(request), ROLES) as db:
        if db is None:
            return reply(403, detail="Эстафеты недоступны")
        stmt = sa.select(Handoff)
        if before is not None:
            anchor = db.get(Handoff, before)
            if anchor is None:
                return reply(404, detail="Страница эстафет не найдена")
            stmt = stmt.where(
                sa.tuple_(Handoff.effective_at, Handoff.id) < (anchor.effective_at, anchor.id)
            )
        rows = list(
            db.scalars(
                stmt.order_by(Handoff.effective_at.desc(), Handoff.id.desc()).limit(PAGE_SIZE + 1)
            )
        )
        return reply(
            items=jsonable_encoder([metadata(item) for item in rows[:PAGE_SIZE]]),
            next_before=str(rows[PAGE_SIZE - 1].id) if len(rows) > PAGE_SIZE else None,
        )


def counts(db: Session, item: Handoff) -> dict[str, int]:
    function = sa.func.knowledge.handoff_completeness(item.id).table_valued("category", "total")
    rows = dict(db.execute(sa.select(function.c.category, function.c.total)).all())
    return {category.value: rows.get(category.value, 0) for category in Category}


@router.get("/options")
def options(request: Request) -> JSONResponse:
    with account_transaction(session_token(request), MANAGERS) as db:
        if db is None:
            return reply(403, detail="Справочник недоступен")
        data = db.scalar(sa.select(sa.func.knowledge.handoff_options(type_=JSONB())))
        return reply(**data) if data is not None else reply(403, detail="Справочник недоступен")


@router.get("/{handoff_id}")
def details(request: Request, handoff_id: UUID) -> JSONResponse:
    with account_transaction(session_token(request), ROLES) as db:
        if db is None or (item := db.get(Handoff, handoff_id)) is None:
            return reply(404, detail="Эстафета не найдена")
        actor_view = sa.func.knowledge.handoff_actor().table_valued("id", "role", "department_id")
        actor = db.execute(
            sa.select(actor_view.c.id, actor_view.c.role, actor_view.c.department_id)
        ).one_or_none()
        if actor is None:
            return reply(404, detail="Эстафета не найдена")
        manages = can_manage_handoffs(Role(actor.role)) and (
            actor.role == Role.HR or actor.department_id == item.department_id
        )
        completeness = (
            counts(db, item)
            if item.kind == "departure" and (manages or actor.id == item.employee_id)
            else None
        )
        return reply(
            **jsonable_encoder(
                {
                    **metadata(item),
                    "completeness": completeness,
                    "can_cancel": manages,
                    "can_start": item.successor_assigned and actor.id == item.successor_id,
                }
            )
        )


PRIORITY = {
    "risks": 0,
    "unfinished": 1,
    "responsibilities": 2,
    "processes": 3,
    "people": 4,
}


def starting_items(db: Session, item: Handoff, actor: UUID) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    relation = sa.func.knowledge.access_relation(KnowledgeSpace.id)
    for model in (KnowledgeRecord, Material):
        category = KnowledgeRecord.category if model is KnowledgeRecord else sa.null()
        query = (
            sa.select(
                model.id,
                model.author_id,
                model.space_kind,
                model.status,
                category,
                relation,
                sa.func.left(model.cleaned_text, 240),
            )
            .join(KnowledgeSpace, KnowledgeSpace.id == model.space_id)
            .where(
                KnowledgeSpace.position_id == item.position_id,
                model.status == KnowledgeStatus.PUBLISHED,
                relation == Relation.POSITION,
            )
            .limit(8 - len(result))
        )
        if model is KnowledgeRecord:
            query = query.order_by(sa.case(PRIORITY, value=KnowledgeRecord.category, else_=5))
        rows = db.execute(query.order_by(model.id))
        target = Target.RECORD if model is KnowledgeRecord else Target.MATERIAL
        for row_id, author, space_kind, status, group, role, excerpt in rows:
            if not can_read(Relation(role), space_kind, status):
                continue
            if author != actor:
                append_event(db, Action.KNOWLEDGE_VIEWED, actor_id=actor, target=(target, row_id))
            result.append(
                {
                    "id": row_id,
                    "category": group,
                    "kind": model.__tablename__,
                    "author_id": author,
                    "excerpt": excerpt,
                }
            )
    return result


@router.get("/{handoff_id}/start")
def where_to_start(request: Request, handoff_id: UUID) -> JSONResponse:
    with account_transaction(session_token(request), ROLES) as db:
        if db is None or (item := db.get(Handoff, handoff_id)) is None:
            return reply(404, detail="Эстафета не найдена")
        actor = db.scalar(sa.select(sa.func.knowledge.current_uploader(type_=sa.Uuid())))
        if actor is None or actor != item.successor_id or not item.successor_assigned:
            return reply(403, detail="Сводка доступна назначенному преемнику")
        return reply(items=jsonable_encoder(starting_items(db, item, actor)))
