from asyncio import timeout
from datetime import UTC, datetime
from logging import getLogger
from threading import BoundedSemaphore
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from estafeta.api.auth import AuthRoute, csrf_valid, rate_exceeded, reply, session_token
from estafeta.auth.service import consume_limit
from estafeta.core.config import Settings, get_settings
from estafeta.core.knowledge_access import knowledge_transaction, read_knowledge
from estafeta.ingest.formats import MAX_UPLOAD_BYTES, SourceKind, UploadError
from estafeta.ingest.pipeline import (
    Choices,
    Submission,
    clean,
    extract_input,
    matches_preview,
    preview_proof,
)
from estafeta.ingest.policy import PLACEHOLDERS, Directory, PolicyError, load_policy
from estafeta.ingest.repository import current_uploader, load_directory, save_draft
from estafeta.models import Material

MAX_CHOICE_HEADER = 2500
UPLOAD_SLOTS = BoundedSemaphore(3)
LOGGER = getLogger(__name__)


class MaterialRoute(AuthRoute):
    post_media_type = "application/octet-stream"


router = APIRouter(prefix="/materials", tags=["materials"], route_class=MaterialRoute)


async def read_payload(request: Request) -> bytes:
    result = bytearray()
    async for chunk in request.stream():
        if len(result) + len(chunk) > MAX_UPLOAD_BYTES:
            raise UploadError("Размер файла превышает 20 МБ")
        result.extend(chunk)
    return bytes(result)


def upload_context(token: str) -> tuple[UUID, Directory] | JSONResponse:
    with knowledge_transaction(token) as db:
        if db is None or (actor_id := current_uploader(db)) is None:
            return reply(403, detail="Загрузка материалов недоступна")
        if not consume_limit(db, "material-upload", str(actor_id), datetime.now(UTC), 30):
            return rate_exceeded()
        return actor_id, load_directory(db)


def preview_input(
    token: str,
    settings: Settings,
    context: tuple[UUID, Directory],
    kind: SourceKind,
    raw: bytes,
    choices: Choices,
    proof: str | None,
) -> JSONResponse:
    actor_id, directory = context
    parsed = extract_input(raw, kind)
    submission = Submission.for_input(settings.app_secret_key, actor_id, kind, raw)
    preview = clean(
        parsed, load_policy(settings.redaction_policy_path), directory, submission, choices
    )
    if proof is None:
        return reply(
            text=preview.text,
            proof=preview_proof(submission, preview, choices),
            markers=[
                {
                    "start": marker.start,
                    "end": marker.end,
                    "type": marker.kind,
                    "label": PLACEHOLDERS[marker.kind],
                    "contact_id": marker.contact_id,
                    "base_start": marker.base_start,
                    "base_end": marker.base_end,
                }
                for marker in preview.markers
            ],
            counts=preview.counts,
            warning="Автоматическая очистка не даёт полной гарантии. Проверьте текст.",
        )

    if not matches_preview(proof, submission, preview, choices):
        return reply(409, detail="Предпросмотр устарел или был изменён. Проверьте материал снова")
    material_id = UUID(hex=submission.sign(b"estafeta:material-id:v1", proof)[:32])
    with knowledge_transaction(token) as db:
        if db is None or current_uploader(db) != actor_id:
            return reply(403, detail="Сохранение материала недоступно")
        save_draft(db, actor_id, material_id, kind, preview)
    return reply(201, id=str(material_id), status="draft")


async def process_upload(request: Request, *, save: bool) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    if not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    header = request.headers.get("x-material-choices", "{}")
    proof = request.headers.get("x-material-proof") if save else None
    if save and (not proof or len(proof) > 80):
        return reply(400, detail="Сначала подтвердите предпросмотр")
    if len(header) > MAX_CHOICE_HEADER:
        return reply(400, detail="Слишком много отметок в предпросмотре")
    try:
        kind = SourceKind(request.headers.get("x-material-format"))
        choices = Choices.model_validate_json(header)
    except (ValueError, ValidationError):
        return reply(400, detail="Неверный формат материала или параметры очистки")
    if not UPLOAD_SLOTS.acquire(blocking=False):
        return reply(429, detail="Обработка материалов занята. Повторите запрос позже")
    try:
        context = await run_in_threadpool(upload_context, token)
        if isinstance(context, JSONResponse):
            return context
        try:
            async with timeout(30):
                raw = await read_payload(request)
        except TimeoutError:
            return reply(408, detail="Загрузка заняла слишком много времени")
        except UploadError:
            return reply(413, detail="Размер файла превышает 20 МБ")
        except ClientDisconnect:
            return reply(400, detail="Загрузка была прервана")
        try:
            return await run_in_threadpool(
                preview_input, token, settings, context, kind, raw, choices, proof
            )
        except UploadError as error:
            return reply(422, detail=str(error))
        except PolicyError:
            return reply(503, detail="Очистка сейчас недоступна. Материал не сохранён")
    except SQLAlchemyError:
        LOGGER.error("Ошибка БД при обработке материала")
        return reply(503, detail="Сохранение недоступно. Материал не сохранён")
    finally:
        UPLOAD_SLOTS.release()


@router.post("/preview")
async def make_preview(request: Request) -> JSONResponse:
    return await process_upload(request, save=False)


@router.post("")
async def create_material(request: Request) -> JSONResponse:
    return await process_upload(request, save=True)


@router.get("/{material_id}")
def show_material(request: Request, material_id: UUID) -> JSONResponse:
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    with knowledge_transaction(token) as db:
        if db is None:
            return reply(403, detail="Чтение материалов недоступно")
        material = read_knowledge(db, Material, material_id)
        if material is None:
            return reply(404, detail="Материал не найден")
        return reply(
            id=str(material.id),
            text=material.cleaned_text,
            format=material.source_kind,
            status=material.status,
        )
