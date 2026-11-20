from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from estafeta.api.auth import AuthRoute, csrf_valid, reply, session_token
from estafeta.core.administration import admin_transaction
from estafeta.core.audit import append_event
from estafeta.core.config import get_settings
from estafeta.core.knowledge_access import knowledge_transaction, read_knowledge
from estafeta.core.permissions import (
    Relation,
    can_archive,
    can_copy,
    can_publish,
    can_read,
    can_return,
    can_submit,
    can_write,
)
from estafeta.models import KnowledgeRecord, Material, PublicationSettings
from estafeta.models.audit import Action

router = APIRouter(prefix="/publication", tags=["publication"], route_class=AuthRoute)


class ItemKind(StrEnum):
    MATERIAL = "materials"
    RECORD = "records"

    @property
    def model(self) -> type[Material] | type[KnowledgeRecord]:
        return Material if self is ItemKind.MATERIAL else KnowledgeRecord


class Operation(StrEnum):
    SUBMIT = "submit"
    RETURN = "return"
    PUBLISH = "publish"
    ARCHIVE = "archive"
    COPY = "copy"


class Scope(StrEnum):
    PERSONAL = "personal"
    REVIEW = "review"
    POSITION = "position"
    DEPARTMENT = "department"


SCOPE_RELATIONS = {
    Scope.PERSONAL: (Relation.OWNER,),
    Scope.REVIEW: (Relation.SUPERVISOR, Relation.HANDOFF),
    Scope.POSITION: (Relation.POSITION,),
    Scope.DEPARTMENT: (Relation.POSITION, Relation.DEPARTMENT),
}


def actions_for(
    item: Material | KnowledgeRecord,
    relation: Relation,
    destination: UUID | None,
    manages: bool,
    review_required: bool,
) -> list[Operation]:
    status = item.status
    checks = {
        Operation.SUBMIT: can_submit(relation, status, destination is not None),
        Operation.RETURN: can_return(relation, status),
        Operation.PUBLISH: can_publish(
            relation,
            status,
            review_required=review_required,
            target_allowed=destination is not None,
        ),
        Operation.ARCHIVE: can_archive(relation, status, manages_position=manages),
        Operation.COPY: isinstance(item, KnowledgeRecord)
        and can_copy(relation, item.space_kind, status),
    }
    return [operation for operation, allowed in checks.items() if allowed]


def context_columns(space: sa.ColumnElement):
    return (
        sa.func.knowledge.publication_context(space)
        .table_valued("relation", "destination", "manages_position", "review_required")
        .lateral()
    )


@router.get("/{kind}")
def listing(
    request: Request,
    kind: ItemKind,
    scope: Scope = Scope.PERSONAL,
    after_id: UUID | None = None,
    author_id: UUID | None = None,
) -> JSONResponse:
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    if author_id is not None and scope not in {Scope.POSITION, Scope.DEPARTMENT}:
        return reply(400, detail="Фильтр автора доступен только для публикаций")
    with knowledge_transaction(token) as db:
        if db is None:
            return reply(403, detail="Материалы недоступны")
        model = kind.model
        ctx = context_columns(model.space_id)
        stmt = (
            sa.select(model, ctx)
            .options(sa.orm.defer(model.cleaned_text))
            .join(ctx, sa.true())
            .where(ctx.c.relation.in_(SCOPE_RELATIONS[scope]))
            .order_by(model.id)
            .limit(51)
        )
        if after_id is not None:
            stmt = stmt.where(model.id > after_id)
        if author_id is not None:
            stmt = stmt.where(model.author_id == author_id)
        rows = db.execute(stmt).all()
        items = [
            {
                "id": item.id,
                "author_id": item.author_id,
                "status": item.status,
                "actions": actions_for(item, Relation(relation), dest, manages, required),
            }
            for item, relation, dest, manages, required in rows[:50]
            if can_read(Relation(relation), item.space_kind, item.status)
        ]
        return reply(
            items=jsonable_encoder(items),
            next_after_id=str(rows[49][0].id) if len(rows) > 50 else None,
        )


@router.get("/{kind}/{item_id}")
def detail(request: Request, kind: ItemKind, item_id: UUID) -> JSONResponse:
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    with knowledge_transaction(token) as db:
        if db is None:
            return reply(403, detail="Материалы недоступны")
        item = read_knowledge(db, kind.model, item_id)
        if item is None:
            return reply(404, detail="Объект не найден")
        result: dict[str, object] = {
            "id": item.id,
            "text": item.cleaned_text,
            "status": item.status,
            "author_id": item.author_id,
        }
        if isinstance(item, KnowledgeRecord):
            result.update(
                original_id=item.original_id,
                original_author_id=item.original_author_id,
                source_chunk_id=item.source_chunk_id,
                category=item.category,
                confidence=item.confidence,
                manually_edited=item.manually_edited,
                editable=can_write(
                    Relation(
                        db.scalar(sa.select(sa.func.knowledge.access_relation(item.space_id)))
                    ),
                    item.status,
                ),
            )
        return reply(**jsonable_encoder(result))


@router.post("/{kind}/{item_id}/{operation}")
def transition(
    request: Request, kind: ItemKind, item_id: UUID, operation: Operation
) -> JSONResponse:
    token = session_token(request)
    if not csrf_valid(request, get_settings(), token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    with knowledge_transaction(token) as db:
        if db is None:
            return reply(403, detail="Операция недоступна")
        model = kind.model
        ctx = context_columns(model.space_id)
        row = db.execute(
            sa.select(model, ctx)
            .options(sa.orm.defer(model.cleaned_text))
            .join(ctx, sa.true())
            .where(model.id == item_id)
        ).one_or_none()
        if row is None:
            return reply(404, detail="Объект не найден")
        item, relation, dest, manages, required = row
        if operation not in actions_for(item, Relation(relation), dest, manages, required):
            return reply(409, detail="Действие недоступно для этого объекта")
        if operation is Operation.COPY:
            copied = db.scalar(sa.select(sa.func.knowledge.copy_record(item_id)))
            if copied is not None:
                return reply(201, id=str(copied), status="draft")
        else:
            updated = db.scalar(
                sa.select(sa.func.knowledge.transition_item(kind.value, item_id, operation.value))
            )
            if updated:
                return reply(id=str(item_id))
        return reply(409, detail="Объект или права изменились. Обновите список")


class ReviewSetting(BaseModel):
    review_required: StrictBool


settings_router = APIRouter(
    prefix="/settings/publication", tags=["settings"], route_class=AuthRoute
)


@settings_router.get("")
@settings_router.post("")
def publication_settings(request: Request, body: ReviewSetting | None = None) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    if request.method == "POST" and (body is None or not csrf_valid(request, settings, token)):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    with admin_transaction(token) as (db, actor):
        value = db.scalar(sa.select(PublicationSettings.review_required))
        if request.method == "POST" and body is not None:
            changed = db.execute(
                sa.update(PublicationSettings)
                .where(PublicationSettings.review_required != body.review_required)
                .values(review_required=body.review_required)
            )
            if changed.rowcount:
                append_event(db, Action.PUBLICATION_SETTINGS_CHANGED, actor_id=actor)
            value = body.review_required
        return reply(review_required=value)
