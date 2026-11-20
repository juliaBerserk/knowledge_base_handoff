from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from estafeta.api.auth import AuthRoute, csrf_valid, reply, session_token
from estafeta.core.audit import append_event
from estafeta.core.config import get_settings
from estafeta.core.knowledge_access import knowledge_transaction
from estafeta.core.permissions import Relation, can_read, can_write
from estafeta.indexing.queue import enqueue
from estafeta.ingest.formats import SourceKind, UploadError
from estafeta.ingest.pipeline import Choices, Submission, clean
from estafeta.ingest.policy import PolicyError, load_policy
from estafeta.ingest.repository import current_uploader, load_directory
from estafeta.models import KnowledgeRecord, Material
from estafeta.models.audit import Action, Target
from estafeta.models.indexing import BuildJob, Chunk

router = APIRouter(prefix="/build", tags=["build"], route_class=AuthRoute)


@router.post("")
def start_build(request: Request) -> JSONResponse:
    token = session_token(request)
    if not csrf_valid(request, get_settings(), token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    with knowledge_transaction(token) as db:
        if db is None or (actor := current_uploader(db)) is None:
            return reply(403, detail="Сборка базы недоступна")
        return reply(202, id=str(enqueue(db, actor)))


@router.get("")
def build_status(request: Request, job_id: UUID | None = None) -> JSONResponse:
    with knowledge_transaction(session_token(request), touch=False) as db:
        if db is None or (actor := current_uploader(db)) is None:
            return reply(403, detail="Сборка базы недоступна")
        stmt = sa.select(BuildJob).where(BuildJob.owner_id == actor)
        if job_id is not None:
            stmt = stmt.where(BuildJob.id == job_id)
        job = db.scalar(stmt.order_by(BuildJob.created_at.desc(), BuildJob.id.desc()).limit(1))
        if job is None:
            if job_id is not None:
                return reply(404, detail="Задача не найдена")
            return reply(job=None)
        return reply(
            job=jsonable_encoder(
                {
                    "id": job.id,
                    "status": job.status,
                    "stage": job.stage,
                    "created_at": job.created_at,
                    "progress": job.progress,
                }
            )
        )


@router.get("/sources/{chunk_id}")
def show_source(request: Request, chunk_id: UUID) -> JSONResponse:
    with knowledge_transaction(session_token(request)) as db:
        if db is None:
            return reply(403, detail="Источники недоступны")
        row = db.execute(
            sa.select(
                Chunk.material_id,
                Chunk.text,
                Chunk.start_offset,
                Chunk.end_offset,
                Material.space_kind,
                Material.status,
                Material.author_id,
                sa.func.knowledge.access_relation(Material.space_id).label("relation"),
            )
            .join(Material, Chunk.material_id == Material.id)
            .where(Chunk.id == chunk_id, Material.status != "archived")
        ).one_or_none()
        if row is None or not can_read(Relation(row.relation), row.space_kind, row.status):
            return reply(404, detail="Источник недоступен или заменён новой версией")
        actor = current_uploader(db)
        if actor != row.author_id:
            append_event(
                db,
                Action.KNOWLEDGE_VIEWED,
                actor_id=actor,
                target=(Target.MATERIAL, row.material_id),
            )
        return reply(
            material_id=str(row.material_id),
            text=row.text,
            start_offset=row.start_offset,
            end_offset=row.end_offset,
        )


class EditBody(BaseModel):
    text: str = Field(min_length=1, max_length=12_000)


@router.post("/records/{record_id}/edit")
def edit_record(request: Request, record_id: UUID, body: EditBody) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    if not body.text.strip() or "\x00" in body.text:
        return reply(422, detail="Введите текст записи")
    with knowledge_transaction(token) as db:
        if db is None or (actor := current_uploader(db)) is None:
            return reply(403, detail="Правка недоступна")
        row = db.execute(
            sa.select(
                KnowledgeRecord.status,
                KnowledgeRecord.author_id,
                sa.func.knowledge.access_relation(KnowledgeRecord.space_id).label("relation"),
            ).where(KnowledgeRecord.id == record_id)
        ).one_or_none()
        if (
            row is None
            or row.author_id != actor
            or not can_write(Relation(row.relation), row.status)
        ):
            return reply(404, detail="Доступный черновик не найден")
        submission = Submission.for_input(
            settings.app_secret_key, actor, SourceKind.NOTE, body.text.encode()
        )
        try:
            preview = clean(
                body.text,
                load_policy(settings.redaction_policy_path),
                load_directory(db),
                submission,
                Choices(),
            )
        except (PolicyError, UploadError):
            return reply(422, detail="Не удалось очистить текст записи")
        if len(preview.text) > 16_000:
            return reply(422, detail="Очищенный текст слишком длинный")
        updated = db.scalar(
            sa.update(KnowledgeRecord)
            .where(
                KnowledgeRecord.id == record_id,
                KnowledgeRecord.status == row.status,
            )
            .values(cleaned_text=preview.text, manually_edited=True)
            .returning(KnowledgeRecord.id)
        )
        if updated is None:
            return reply(409, detail="Запись изменилась. Откройте её заново")
        return reply(text=preview.text, manually_edited=True)
