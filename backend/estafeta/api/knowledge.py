from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from estafeta.api.dependencies import (
    Principal,
    get_db_session,
    require_knowledge_user,
    verify_csrf,
)
from estafeta.schemas.knowledge import (
    KnowledgeRecordCreate,
    KnowledgeRecordListResponse,
    KnowledgeRecordResponse,
    KnowledgeRecordUpdate,
)
from estafeta.services.document_input import DocumentInputError, extract_note
from estafeta.services.knowledge import (
    KnowledgeError,
    archive_item,
    copy_record,
    create_record,
    get_record,
    list_records,
    publish_item,
    submit_record,
    update_record,
)
from estafeta.services.materials import prepare_material
from estafeta.services.sanitization import SanitizationError

router = APIRouter(prefix="/records", tags=["knowledge-records"])


@router.post("", response_model=KnowledgeRecordResponse, status_code=201)
def create_knowledge_record(
    payload: KnowledgeRecordCreate,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    verify_csrf(request, principal)
    title, content, redaction_counts = _sanitize(
        session,
        principal,
        payload.title,
        payload.content,
    )
    try:
        record = create_record(
            session,
            user=principal.user,
            category=payload.category.value,
            title=title,
            content=content,
            source_material_id=payload.source_material_id,
            redaction_counts=redaction_counts,
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordResponse.model_validate(record)


@router.get("", response_model=KnowledgeRecordListResponse)
def get_records(
    space: Literal["mine", "position", "department", "review"] = "mine",
    author_id: UUID | None = None,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordListResponse:
    try:
        records = list_records(
            session,
            user=principal.user,
            space=space,
            author_id=author_id,
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordListResponse(
        items=[KnowledgeRecordResponse.model_validate(record) for record in records]
    )


@router.get("/{record_id}", response_model=KnowledgeRecordResponse)
def get_knowledge_record(
    record_id: UUID,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    record = get_record(session, user=principal.user, record_id=record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return KnowledgeRecordResponse.model_validate(record)


@router.put("/{record_id}", response_model=KnowledgeRecordResponse)
def edit_knowledge_record(
    record_id: UUID,
    payload: KnowledgeRecordUpdate,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    verify_csrf(request, principal)
    title, content, redaction_counts = _sanitize(
        session,
        principal,
        payload.title,
        payload.content,
    )
    try:
        record = update_record(
            session,
            user=principal.user,
            record_id=record_id,
            category=payload.category.value,
            title=title,
            content=content,
            source_material_id=payload.source_material_id,
            redaction_counts=redaction_counts,
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordResponse.model_validate(record)


@router.post("/{record_id}/copy", response_model=KnowledgeRecordResponse, status_code=201)
def copy_knowledge_record(
    record_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    verify_csrf(request, principal)
    try:
        record = copy_record(session, user=principal.user, record_id=record_id)
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordResponse.model_validate(record)


@router.post("/{record_id}/submit", response_model=KnowledgeRecordResponse)
def submit_record_for_publication(
    record_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    verify_csrf(request, principal)
    try:
        record = submit_record(session, user=principal.user, record_id=record_id)
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordResponse.model_validate(record)


@router.post("/{record_id}/publish", response_model=KnowledgeRecordResponse)
def approve_record_publication(
    record_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    verify_csrf(request, principal)
    try:
        record = publish_item(
            session,
            user=principal.user,
            item_id=record_id,
            subject="record",
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordResponse.model_validate(record)


@router.post("/{record_id}/archive", response_model=KnowledgeRecordResponse)
def archive_knowledge_record(
    record_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> KnowledgeRecordResponse:
    verify_csrf(request, principal)
    try:
        record = archive_item(
            session,
            user=principal.user,
            item_id=record_id,
            subject="record",
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return KnowledgeRecordResponse.model_validate(record)


def _sanitize(
    session: Session,
    principal: Principal,
    title: str,
    content: str,
) -> tuple[str, str, dict[str, int]]:
    try:
        document = extract_note(content)
        prepared = prepare_material(
            session,
            title=title,
            document=document,
        )
    except DocumentInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except SanitizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return (
        prepared.title.cleaned_text,
        prepared.content.cleaned_text,
        prepared.redaction_counts(),
    )
