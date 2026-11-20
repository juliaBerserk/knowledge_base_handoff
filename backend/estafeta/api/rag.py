import json
from collections.abc import AsyncIterator, Generator
from dataclasses import asdict
from datetime import UTC, datetime
from functools import lru_cache
from threading import BoundedSemaphore
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from anyio import CancelScope
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from estafeta.api.auth import AuthRoute, csrf_valid, rate_exceeded, reply, session_token
from estafeta.auth.service import consume_limit
from estafeta.core.audit import append_event
from estafeta.core.config import Settings, get_settings
from estafeta.core.knowledge_access import knowledge_transaction
from estafeta.indexing.providers import BgeM3Embedder, OpenAICompatibleClient, ProviderError
from estafeta.ingest.formats import SourceKind, UploadError
from estafeta.ingest.pipeline import Choices, Submission, clean
from estafeta.ingest.policy import Directory, PolicyError, load_policy
from estafeta.ingest.repository import current_uploader, load_directory
from estafeta.models import Material
from estafeta.models.audit import Action, Target
from estafeta.models.chat import Chat, ChatTurn
from estafeta.rag.answers import NO_DATA, PROMPT_VERSION, Citation, certified_quotes
from estafeta.rag.search import Hit, Scope, authorized_hits, query_vector, search

router = APIRouter(tags=["rag"], route_class=AuthRoute)
VECTOR_SLOTS = BoundedSemaphore(1)
ANSWER_SLOTS = BoundedSemaphore(4)
PAGE_SIZE = 30


@lru_cache(maxsize=1)
def embedder() -> BgeM3Embedder:
    settings = get_settings()
    return BgeM3Embedder(settings.embedding_model, settings.embedding_revision)


@lru_cache(maxsize=1)
def llm() -> OpenAICompatibleClient:
    settings = get_settings()
    return OpenAICompatibleClient(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key,
        settings.llm_timeout_seconds,
    )


class SearchBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    scope: Scope = Scope.PERSONAL


class AskBody(SearchBody):
    chat_id: UUID | None = None


class FeedbackBody(BaseModel):
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=1000)


def checked_token(request: Request) -> str | JSONResponse:
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    if request.method == "POST" and not csrf_valid(request, get_settings(), token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    return token


def clean_input(text: str, actor: UUID, directory: Directory, settings: Settings) -> str:
    text = text.strip()
    if not text or "\x00" in text:
        raise UploadError("Введите текст")
    submission = Submission.for_input(
        settings.app_secret_key, actor, SourceKind.NOTE, text.encode()
    )
    cleaned = clean(
        text,
        load_policy(settings.redaction_policy_path),
        directory,
        submission,
        Choices(),
    ).text.strip()
    if len(cleaned) > 1000:
        raise UploadError("Очищенный текст превышает 1000 символов")
    return cleaned


def prepare(token: str, text: str, purpose: str, limit: int) -> str | JSONResponse:
    with knowledge_transaction(token) as db:
        if db is None or (actor := current_uploader(db)) is None:
            return reply(403, detail="Операция недоступна")
        if not consume_limit(db, purpose, str(actor), datetime.now(UTC), limit):
            return rate_exceeded()
        directory = load_directory(db)
    try:
        return clean_input(text, actor, directory, get_settings())
    except UploadError as error:
        return reply(422, detail=str(error))
    except PolicyError:
        return reply(503, detail="Очистка текста недоступна")


def find_hits(
    token: str, question: str, scope: Scope, *, audit_views: bool = False
) -> tuple[list[Hit], bool] | None:
    vector = None
    if VECTOR_SLOTS.acquire(blocking=False):
        try:
            vector = query_vector(embedder(), question)
        finally:
            VECTOR_SLOTS.release()
    with knowledge_transaction(token, touch=False) as db:
        if db is None:
            return None
        hits = search(db, question, scope, vector, get_settings())
        if audit_views and hits:
            actor = current_uploader(db)
            viewed = db.scalars(
                sa.select(Material.id).where(
                    Material.id.in_({hit.material_id for hit in hits}),
                    Material.author_id != actor,
                )
            )
            for material_id in viewed:
                append_event(
                    db,
                    Action.KNOWLEDGE_VIEWED,
                    actor_id=actor,
                    target=(Target.MATERIAL, material_id),
                )
        return hits, vector is not None


@router.post("/search")
def search_materials(request: Request, body: SearchBody) -> JSONResponse:
    token = checked_token(request)
    if isinstance(token, JSONResponse):
        return token
    question = prepare(token, body.question, "rag-search", 30)
    if isinstance(question, JSONResponse):
        return question
    result = find_hits(token, question, body.scope, audit_views=True)
    if result is None:
        return reply(403, detail="Поиск недоступен")
    hits, semantic = result
    return reply(
        results=jsonable_encoder([asdict(hit) for hit in hits]), semantic_available=semantic
    )


def sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def visible_citations(db: Session, entries: list[Citation], scope: Scope) -> bool:
    resolved = authorized_hits(db, [UUID(entry.chunk_id) for entry in entries], scope)
    return all(
        (hit := resolved.get(UUID(entry.chunk_id))) is not None
        and str(hit.material_id) == entry.material_id
        and entry.text in hit.text
        for entry in entries
    )


def open_turn(token: str, existing: UUID | None) -> UUID | JSONResponse:
    with knowledge_transaction(token, touch=False) as db:
        if db is None or (actor := current_uploader(db)) is None:
            return reply(403, detail="Чат недоступен")
        chat_id = existing or uuid4()
        if existing is None:
            db.add(Chat(id=chat_id, owner_id=actor))
            db.flush()
        elif db.get(Chat, chat_id) is None:
            return reply(404, detail="Чат не найден")
        append_event(db, Action.AI_QUESTION, actor_id=actor, target=(Target.CHAT, chat_id))
        return chat_id


def finish(
    token: str,
    chat_id: UUID,
    scope: Scope,
    question: str,
    entries: list[Citation],
    model: str,
) -> UUID | None:
    with knowledge_transaction(token, touch=False) as db:
        if db is None or db.get(Chat, chat_id) is None or not visible_citations(db, entries, scope):
            return None
        turn_id = uuid4()
        db.execute(
            sa.insert(ChatTurn.__table__)
            .inline()
            .values(
                id=turn_id,
                chat_id=chat_id,
                question=question,
                scope=scope,
                segments=[asdict(entry) for entry in entries],
                model_version=model,
                prompt_version=PROMPT_VERSION,
            )
        )
        return turn_id


def answer_events(
    token: str, chat_id: UUID, body: AskBody, question: str
) -> Generator[str, None, None]:
    if not ANSWER_SLOTS.acquire(blocking=False):
        yield sse("error", {"detail": "Модель занята. Повторите попытку позже"})
        return
    try:
        yield sse("started", {"chat_id": str(chat_id), "question": question})
        found = find_hits(token, question, body.scope)
        if found is None:
            yield sse("error", {"detail": "Доступ к чату завершён"})
            return
        hits, _ = found
        entries: list[Citation] = []
        model = "not_called"
        if hits:
            model = get_settings().llm_model
            with knowledge_transaction(token, touch=False) as db:
                current = (
                    authorized_hits(db, [hit.id for hit in hits], body.scope)
                    if db is not None and db.get(Chat, chat_id) is not None
                    else None
                )
            if current is None:
                yield sse("error", {"detail": "Доступ к чату завершён"})
                return
            sources = [current[hit.id] for hit in hits if hit.id in current]
            if sources:
                with_quotes = certified_quotes(llm(), question, sources)
                try:
                    for entry, reported in with_quotes:
                        model = reported
                        with knowledge_transaction(token, touch=False) as db:
                            allowed = (
                                db is not None
                                and db.get(Chat, chat_id) is not None
                                and visible_citations(db, [entry], body.scope)
                            )
                        if not allowed:
                            yield sse("error", {"detail": "Доступ к источнику изменился"})
                            return
                        entries.append(entry)
                        yield sse("citation", asdict(entry))
                finally:
                    with_quotes.close()
            else:
                model = "not_called"
        turn_id = finish(token, chat_id, body.scope, question, entries, model)
        if turn_id is None:
            yield sse("error", {"detail": "Доступ к чату или источнику изменился"})
            return
        yield sse("done", {"turn_id": str(turn_id), "message": "" if entries else NO_DATA})
    except ProviderError:
        yield sse("error", {"detail": "Модель сейчас недоступна. Повторите попытку позже"})
    except sa.exc.SQLAlchemyError:
        yield sse("error", {"detail": "Не удалось завершить ответ"})
    finally:
        ANSWER_SLOTS.release()


async def closeable_stream(events: Generator[str, None, None]) -> AsyncIterator[str]:
    try:
        async for event in iterate_in_threadpool(events):
            yield event
    finally:
        with CancelScope(shield=True):
            await run_in_threadpool(events.close)


@router.post("/chat/ask", response_model=None)
def ask(request: Request, body: AskBody) -> JSONResponse | StreamingResponse:
    token = checked_token(request)
    if isinstance(token, JSONResponse):
        return token
    question = prepare(token, body.question, "rag-ask", 8)
    if isinstance(question, JSONResponse):
        return question
    chat_id = open_turn(token, body.chat_id)
    if isinstance(chat_id, JSONResponse):
        return chat_id
    return StreamingResponse(
        closeable_stream(answer_events(token, chat_id, body, question)),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@router.get("/chat")
def chats(request: Request, before: UUID | None = None) -> JSONResponse:
    token = checked_token(request)
    if isinstance(token, JSONResponse):
        return token
    with knowledge_transaction(token) as db:
        if db is None:
            return reply(403, detail="Чат недоступен")
        stmt = sa.select(Chat.id, Chat.created_at)
        if before is not None:
            cursor = db.get(Chat, before)
            if cursor is None:
                return reply(404, detail="Чат не найден")
            stmt = stmt.where(sa.tuple_(Chat.created_at, Chat.id) < (cursor.created_at, cursor.id))
        rows = (
            db.execute(stmt.order_by(Chat.created_at.desc(), Chat.id.desc()).limit(PAGE_SIZE + 1))
            .mappings()
            .all()
        )
        return reply(
            chats=jsonable_encoder([dict(row) for row in rows[:PAGE_SIZE]]),
            next_before=str(rows[PAGE_SIZE - 1].id) if len(rows) > PAGE_SIZE else None,
        )


@router.get("/chat/{chat_id}")
def history(request: Request, chat_id: UUID, before: UUID | None = None) -> JSONResponse:
    token = checked_token(request)
    if isinstance(token, JSONResponse):
        return token
    with knowledge_transaction(token) as db:
        if db is None or db.get(Chat, chat_id) is None:
            return reply(404, detail="Чат не найден")
        stmt = sa.select(ChatTurn).where(ChatTurn.chat_id == chat_id)
        if before is not None:
            cursor = db.get(ChatTurn, before)
            if cursor is None or cursor.chat_id != chat_id:
                return reply(404, detail="Страница истории не найдена")
            stmt = stmt.where(
                sa.tuple_(ChatTurn.created_at, ChatTurn.id) < (cursor.created_at, cursor.id)
            )
        rows = list(
            db.scalars(
                stmt.order_by(ChatTurn.created_at.desc(), ChatTurn.id.desc()).limit(PAGE_SIZE + 1)
            )
        )
        turns = [view_turn(db, row) for row in reversed(rows[:PAGE_SIZE])]
        return reply(
            turns=jsonable_encoder(turns),
            next_before=str(rows[PAGE_SIZE - 1].id) if len(rows) > PAGE_SIZE else None,
        )


def view_turn(db: Session, turn: ChatTurn) -> dict[str, object]:
    entries = [Citation(**entry) for entry in turn.segments]
    allowed = visible_citations(db, entries, Scope(turn.scope))
    return {
        "id": turn.id,
        "question": turn.question,
        "scope": turn.scope,
        "segments": turn.segments if allowed else [],
        "message": ("" if entries else NO_DATA) if allowed else "Ответ скрыт. Доступ изменился.",
        "model_version": turn.model_version,
        "prompt_version": turn.prompt_version,
        "rating": turn.rating,
        "comment": turn.comment,
    }


@router.post("/chat/{chat_id}/delete")
def delete_chat(request: Request, chat_id: UUID) -> JSONResponse:
    token = checked_token(request)
    if isinstance(token, JSONResponse):
        return token
    with knowledge_transaction(token) as db:
        if (
            db is None
            or db.scalar(sa.delete(Chat).where(Chat.id == chat_id).returning(Chat.id)) is None
        ):
            return reply(404, detail="Чат не найден")
        return reply()


@router.post("/chat/turns/{turn_id}/feedback")
def feedback(request: Request, turn_id: UUID, body: FeedbackBody) -> JSONResponse:
    token = checked_token(request)
    if isinstance(token, JSONResponse):
        return token
    comment = None
    if body.comment and body.comment.strip():
        comment = prepare(token, body.comment, "rag-feedback", 15)
        if isinstance(comment, JSONResponse):
            return comment
    with knowledge_transaction(token) as db:
        if db is None:
            return reply(403, detail="Оценка недоступна")
        updated = db.scalar(
            sa.update(ChatTurn)
            .where(ChatTurn.id == turn_id)
            .values(
                rating=body.rating,
                comment=comment,
            )
            .returning(ChatTurn.id)
        )
        if updated is None:
            return reply(404, detail="Ответ не найден")
        return reply(rating=body.rating, comment=comment)
