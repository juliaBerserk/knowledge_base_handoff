from collections import Counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from estafeta.api.dependencies import (
    Principal,
    get_db_session,
    require_admin,
    require_knowledge_user,
    verify_csrf,
)
from estafeta.models import SanitizationPolicy
from estafeta.schemas.materials import (
    MaterialCreateResponse,
    MaterialListResponse,
    MaterialNotePreviewInput,
    MaterialPreviewResponse,
    MaterialSummary,
    MaterialTextInput,
    RedactionFindingResponse,
    RedactionRange,
    SanitizationPolicyResponse,
    SanitizationPolicyUpdate,
)
from estafeta.services.document_input import (
    DocumentInputError,
    ExtractedDocument,
    extract_document,
    extract_note,
)
from estafeta.services.knowledge import (
    KnowledgeError,
    archive_item,
    get_material_view,
    list_materials,
    publish_item,
    submit_material,
)
from estafeta.services.materials import (
    PreparedMaterial,
    create_material,
    load_sanitization_policy,
    prepare_material,
    save_sanitization_policy,
)
from estafeta.services.sanitization import SanitizationError

router = APIRouter(tags=["materials"])
admin_router = APIRouter(prefix="/admin/sanitization-policy", tags=["sanitization-policy"])
_PREVIEW_WARNING = (
    "Автоматическая очистка не гарантирует обнаружение всех секретов и персональных данных. "
    "Проверьте очищенный текст перед сохранением."
)


@router.post("/materials/preview", response_model=MaterialPreviewResponse)
async def preview_upload(
    request: Request,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    file: Annotated[UploadFile, File()],
    manual_redactions_json: str = Form(default="[]"),
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialPreviewResponse:
    verify_csrf(request, principal)
    document = await _extract_upload(file)
    manual_redactions = _parse_manual_redactions(manual_redactions_json)
    prepared = _prepare(session, title, document, manual_redactions=manual_redactions)
    return _preview_response(prepared, document.source_kind)


@router.post("/materials/preview-note", response_model=MaterialPreviewResponse)
def preview_note(
    payload: MaterialNotePreviewInput,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialPreviewResponse:
    verify_csrf(request, principal)
    try:
        document = extract_note(payload.text)
        prepared = _prepare(
            session,
            payload.title,
            document,
            manual_redactions=payload.manual_redactions,
        )
    except DocumentInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except SanitizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _preview_response(prepared, document.source_kind)


@router.post("/materials", response_model=MaterialCreateResponse, status_code=201)
async def create_from_upload(
    request: Request,
    title: Annotated[str, Form(min_length=1, max_length=300)],
    file: Annotated[UploadFile, File()],
    manual_redactions_json: str = Form(default="[]"),
    approved_external_contact_ids: list[str] = Form(default_factory=list),
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialCreateResponse:
    verify_csrf(request, principal)
    document = await _extract_upload(file)
    manual_redactions = _parse_manual_redactions(manual_redactions_json)
    prepared = _prepare(
        session,
        title,
        document,
        approved_external_contact_ids=set(approved_external_contact_ids),
        manual_redactions=manual_redactions,
    )
    return _save(session, principal, document, prepared)


@router.post("/materials/note", response_model=MaterialCreateResponse, status_code=201)
def create_note(
    payload: MaterialTextInput,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialCreateResponse:
    verify_csrf(request, principal)
    try:
        document = extract_note(payload.text)
        prepared = _prepare(
            session,
            payload.title,
            document,
            approved_external_contact_ids=set(payload.approved_external_contact_ids),
            manual_redactions=payload.manual_redactions,
        )
    except DocumentInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except SanitizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _save(session, principal, document, prepared)


@router.get("/materials", response_model=MaterialListResponse)
def list_own_materials(
    space: Literal["mine", "position", "department", "review"] = "mine",
    author_id: UUID | None = None,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialListResponse:
    try:
        rows = list_materials(
            session,
            user=principal.user,
            space=space,
            author_id=author_id,
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MaterialListResponse(
        items=[MaterialSummary.model_validate(row, from_attributes=True) for row in rows]
    )


@router.get("/materials/{material_id}", response_model=MaterialCreateResponse)
def read_material(
    material_id: UUID,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialCreateResponse:
    result = get_material_view(session, user=principal.user, material_id=material_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Материал не найден")
    material, version, counts = result
    return MaterialCreateResponse(
        id=material.id,
        title=material.title,
        source_kind=version.source_kind,
        version_number=version.version_number,
        cleaned_text=version.cleaned_text,
        redaction_counts=counts,
        created_at=version.created_at,
        owner_id=material.owner_id,
        scope=material.scope,
        status=material.status,
        position_id=material.position_id,
    )


@router.post("/materials/{material_id}/submit", response_model=MaterialSummary)
def submit_material_for_publication(
    material_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialSummary:
    verify_csrf(request, principal)
    try:
        item = submit_material(session, user=principal.user, material_id=material_id)
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MaterialSummary.model_validate(item, from_attributes=True)


@router.post("/materials/{material_id}/publish", response_model=MaterialSummary)
def approve_material_publication(
    material_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialSummary:
    verify_csrf(request, principal)
    try:
        item = publish_item(
            session,
            user=principal.user,
            item_id=material_id,
            subject="material",
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MaterialSummary.model_validate(item, from_attributes=True)


@router.post("/materials/{material_id}/archive", response_model=MaterialSummary)
def archive_material(
    material_id: UUID,
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> MaterialSummary:
    verify_csrf(request, principal)
    try:
        item = archive_item(
            session,
            user=principal.user,
            item_id=material_id,
            subject="material",
        )
    except KnowledgeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MaterialSummary.model_validate(item, from_attributes=True)


@admin_router.get("", response_model=SanitizationPolicyResponse)
def read_sanitization_policy(
    _: Principal = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> SanitizationPolicyResponse:
    row = session.get(SanitizationPolicy, 1)
    policy = load_sanitization_policy(session)
    return SanitizationPolicyResponse(
        **policy.model_dump(),
        updated_at=row.updated_at if row is not None else None,
    )


@admin_router.put("", response_model=SanitizationPolicyResponse)
def update_sanitization_policy(
    payload: SanitizationPolicyUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> SanitizationPolicyResponse:
    verify_csrf(request, principal)
    row = save_sanitization_policy(
        session,
        updated_by=principal.user.id,
        policy=payload,
    )
    return SanitizationPolicyResponse(
        **payload.model_dump(),
        updated_at=row.updated_at,
    )


async def _extract_upload(file: UploadFile) -> ExtractedDocument:
    try:
        content = await file.read(20 * 1024 * 1024 + 1)
        return extract_document(file.filename or "", content)
    except DocumentInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        await file.close()


def _prepare(
    session: Session,
    title: str,
    document: ExtractedDocument,
    approved_external_contact_ids: set[str] | None = None,
    manual_redactions: list[RedactionRange] | None = None,
) -> PreparedMaterial:
    try:
        return prepare_material(
            session,
            title=title,
            document=document,
            approved_external_contact_ids=approved_external_contact_ids,
            manual_redactions=manual_redactions,
        )
    except SanitizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _parse_manual_redactions(value: str) -> list[RedactionRange]:
    if len(value) > 16_384:
        raise HTTPException(status_code=422, detail="Слишком много диапазонов очистки")
    try:
        ranges = TypeAdapter(list[RedactionRange]).validate_json(value)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Некорректные диапазоны очистки") from exc
    if len(ranges) > 100:
        raise HTTPException(status_code=422, detail="Можно добавить не более 100 диапазонов")
    return ranges


def _preview_response(
    prepared: PreparedMaterial,
    source_kind: str,
) -> MaterialPreviewResponse:
    findings: list[RedactionFindingResponse] = []
    for field_name, result in (("title", prepared.title), ("text", prepared.content)):
        per_type = Counter(item.finding_type for item in result.findings)
        for finding in result.findings:
            findings.append(
                RedactionFindingResponse(
                    field=field_name,
                    finding_type=finding.finding_type,
                    count=per_type[finding.finding_type],
                    source_start=finding.source_start,
                    source_end=finding.source_end,
                    contact_id=finding.contact_id,
                    redacted=finding.redacted,
                )
            )
    return MaterialPreviewResponse(
        cleaned_title=prepared.title.cleaned_text,
        cleaned_text=prepared.content.cleaned_text,
        source_kind=source_kind,
        findings=findings,
        warning=_PREVIEW_WARNING,
    )


def _save(
    session: Session,
    principal: Principal,
    document: ExtractedDocument,
    prepared: PreparedMaterial,
) -> MaterialCreateResponse:
    try:
        material, version, counts = create_material(
            session,
            owner=principal.user,
            title=prepared.title.cleaned_text,
            document=document,
            prepared=prepared,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return MaterialCreateResponse(
        id=material.id,
        title=material.title,
        source_kind=version.source_kind,
        version_number=version.version_number,
        cleaned_text=version.cleaned_text,
        redaction_counts=counts,
        created_at=version.created_at,
        owner_id=material.owner_id,
        scope=material.scope,
        status=material.status,
        position_id=material.position_id,
    )
