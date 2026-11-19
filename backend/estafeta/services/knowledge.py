from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from estafeta.core.db import set_local_user_id
from estafeta.domain.access import (
    AccessContext,
    KnowledgeResource,
    KnowledgeScope,
    KnowledgeStatus,
    Role,
    can_archive,
    can_publish,
    can_read,
    can_submit_for_review,
    can_write,
)
from estafeta.models import (
    Department,
    EmploymentAssignment,
    KnowledgeRecord,
    Material,
    MaterialVersion,
    Position,
    RedactionEvent,
    User,
)
from estafeta.schemas.audit import AuditEventType, AuditMetadata
from estafeta.services.audit import record_audit_event


class KnowledgeError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class _CurrentAssignment:
    context: AccessContext
    review_required: bool


KnowledgeItem = TypeVar("KnowledgeItem", Material, KnowledgeRecord)


def current_access(session: Session, user: User) -> _CurrentAssignment:
    assignment = session.execute(
        select(EmploymentAssignment, Position, Department)
        .join(Position, Position.id == EmploymentAssignment.position_id)
        .join(Department, Department.id == Position.department_id)
        .where(
            EmploymentAssignment.user_id == user.id,
            EmploymentAssignment.ends_at.is_(None),
        )
    ).one_or_none()
    if assignment is None:
        return _CurrentAssignment(
            AccessContext(user.id, user.role, None, None, user.is_active),
            True,
        )
    employment, position, department = assignment
    return _CurrentAssignment(
        AccessContext(
            user.id,
            user.role,
            department.id,
            position.id,
            user.is_active,
        ),
        department.publication_review_required,
    )


def _resource(
    session: Session,
    *,
    owner_id: UUID,
    scope: str,
    status: str,
    position_id: UUID | None,
) -> KnowledgeResource:
    department_id = None
    if position_id is not None:
        department_id = session.scalar(
            select(Position.department_id).where(Position.id == position_id)
        )
    return KnowledgeResource(
        owner_id=owner_id,
        scope=KnowledgeScope(scope),
        status=KnowledgeStatus(status),
        department_id=department_id,
        position_id=position_id,
    )


def _audit(
    session: Session,
    user: User,
    event_type: AuditEventType,
    subject_type: str | None,
    subject_id: UUID | None,
) -> None:
    record_audit_event(
        session,
        event_type,
        actor_user_id=user.id,
        actor_role=user.role.value,
        subject_type=subject_type,
        subject_id=subject_id,
    )


def _audit_redactions(
    session: Session,
    user: User,
    record_id: UUID,
    redaction_counts: dict[str, int],
) -> None:
    for finding_type, redacted_count in redaction_counts.items():
        record_audit_event(
            session,
            AuditEventType.RECORD_REDACTION_RECORDED,
            actor_user_id=user.id,
            actor_role=user.role.value,
            subject_type="record",
            subject_id=record_id,
            metadata=AuditMetadata(
                finding_type=finding_type,
                redacted_count=redacted_count,
            ),
        )


def submit_material(session: Session, *, user: User, material_id: UUID) -> Material:
    return _submit(session, user=user, item_id=material_id, model=Material, subject="material")


def submit_record(session: Session, *, user: User, record_id: UUID) -> KnowledgeRecord:
    return _submit(
        session,
        user=user,
        item_id=record_id,
        model=KnowledgeRecord,
        subject="record",
    )


def _submit(
    session: Session,
    *,
    user: User,
    item_id: UUID,
    model: type[KnowledgeItem],
    subject: Literal["material", "record"],
) -> KnowledgeItem:
    set_local_user_id(session, user.id)
    item = session.get(model, item_id)
    if item is None:
        raise KnowledgeError("Материал или запись не найдены", 404)
    assignment = current_access(session, user)
    current_resource = _resource(
        session,
        owner_id=item.owner_id,
        scope=item.scope,
        status=item.status,
        position_id=item.position_id,
    )
    target_resource = KnowledgeResource(
        owner_id=item.owner_id,
        scope=KnowledgeScope.POSITION,
        status=KnowledgeStatus.DRAFT,
        department_id=assignment.context.department_id,
        position_id=assignment.context.position_id,
    )
    if (
        not can_write(assignment.context, current_resource)
        or assignment.context.position_id is None
    ):
        raise KnowledgeError("Нет доступа к отправке на публикацию", 403)

    if assignment.review_required:
        if not can_submit_for_review(assignment.context, target_resource, True):
            raise KnowledgeError("Нет доступа к отправке на проверку", 403)
        item.status = KnowledgeStatus.IN_REVIEW.value
        event = (
            AuditEventType.MATERIAL_SUBMITTED
            if subject == "material"
            else AuditEventType.RECORD_SUBMITTED
        )
    else:
        if not can_publish(assignment.context, target_resource, False):
            raise KnowledgeError("Нет доступа к публикации", 403)
        item.status = KnowledgeStatus.PUBLISHED.value
        event = (
            AuditEventType.MATERIAL_PUBLISHED
            if subject == "material"
            else AuditEventType.RECORD_PUBLISHED
        )
    item.scope = KnowledgeScope.POSITION.value
    item.position_id = assignment.context.position_id
    session.flush()
    _audit(session, user, event, subject, item.id)
    return item


def publish_item(
    session: Session,
    *,
    user: User,
    item_id: UUID,
    subject: Literal["material", "record"],
) -> Material | KnowledgeRecord:
    model = Material if subject == "material" else KnowledgeRecord
    set_local_user_id(session, user.id)
    item = session.get(model, item_id)
    if item is None:
        raise KnowledgeError("Материал или запись не найдены", 404)
    assignment = current_access(session, user)
    resource = _resource(
        session,
        owner_id=item.owner_id,
        scope=item.scope,
        status=item.status,
        position_id=item.position_id,
    )
    review_required = resource.status is KnowledgeStatus.IN_REVIEW
    if not can_publish(assignment.context, resource, review_required):
        raise KnowledgeError("Нет прав на публикацию", 403)
    item.status = KnowledgeStatus.PUBLISHED.value
    session.flush()
    event = (
        AuditEventType.MATERIAL_PUBLISHED
        if subject == "material"
        else AuditEventType.RECORD_PUBLISHED
    )
    _audit(session, user, event, subject, item.id)
    return item


def archive_item(
    session: Session,
    *,
    user: User,
    item_id: UUID,
    subject: Literal["material", "record"],
) -> Material | KnowledgeRecord:
    model = Material if subject == "material" else KnowledgeRecord
    set_local_user_id(session, user.id)
    item = session.get(model, item_id)
    if item is None:
        raise KnowledgeError("Материал или запись не найдены", 404)
    assignment = current_access(session, user)
    resource = _resource(
        session,
        owner_id=item.owner_id,
        scope=item.scope,
        status=item.status,
        position_id=item.position_id,
    )
    if not can_archive(assignment.context, resource):
        raise KnowledgeError("Нет прав на архивацию", 403)
    item.status = KnowledgeStatus.ARCHIVED.value
    session.flush()
    event = (
        AuditEventType.MATERIAL_ARCHIVED
        if subject == "material"
        else AuditEventType.RECORD_ARCHIVED
    )
    _audit(session, user, event, subject, item.id)
    return item


def create_record(
    session: Session,
    *,
    user: User,
    category: str,
    title: str,
    content: str,
    source_material_id: UUID | None,
    redaction_counts: dict[str, int],
) -> KnowledgeRecord:
    set_local_user_id(session, user.id)
    assignment = current_access(session, user)
    if not user.is_active or user.role in {Role.ADMIN, Role.SECURITY_AUDITOR}:
        raise KnowledgeError("Нет доступа к содержимому знаний", 403)
    if source_material_id is not None:
        source = session.get(Material, source_material_id)
        if source is None or not can_read(
            assignment.context,
            _resource(
                session,
                owner_id=source.owner_id,
                scope=source.scope,
                status=source.status,
                position_id=source.position_id,
            ),
        ):
            raise KnowledgeError("Материал-источник не найден", 404)
    record = KnowledgeRecord(
        id=uuid4(),
        owner_id=user.id,
        source_material_id=source_material_id,
        category=category,
        title=title,
        content=content,
        scope=KnowledgeScope.PERSONAL.value,
        status=KnowledgeStatus.DRAFT.value,
    )
    session.add(record)
    session.flush()
    _audit(session, user, AuditEventType.RECORD_CREATED, "record", record.id)
    _audit_redactions(session, user, record.id, redaction_counts)
    return record


def update_record(
    session: Session,
    *,
    user: User,
    record_id: UUID,
    category: str,
    title: str,
    content: str,
    source_material_id: UUID | None,
    redaction_counts: dict[str, int],
) -> KnowledgeRecord:
    set_local_user_id(session, user.id)
    record = session.get(KnowledgeRecord, record_id)
    if record is None:
        raise KnowledgeError("Запись не найдена", 404)
    assignment = current_access(session, user)
    resource = _resource(
        session,
        owner_id=record.owner_id,
        scope=record.scope,
        status=record.status,
        position_id=record.position_id,
    )
    if not can_write(assignment.context, resource):
        raise KnowledgeError("Редактировать можно только свой черновик", 403)
    if source_material_id is not None:
        source = session.get(Material, source_material_id)
        if source is None or not can_read(
            assignment.context,
            _resource(
                session,
                owner_id=source.owner_id,
                scope=source.scope,
                status=source.status,
                position_id=source.position_id,
            ),
        ):
            raise KnowledgeError("Материал-источник не найден", 404)
    record.category = category
    record.title = title
    record.content = content
    record.source_material_id = source_material_id
    if record.record_origin == "extracted":
        record.is_manually_edited = True
    session.flush()
    _audit(session, user, AuditEventType.RECORD_UPDATED, "record", record.id)
    _audit_redactions(session, user, record.id, redaction_counts)
    return record


def copy_record(session: Session, *, user: User, record_id: UUID) -> KnowledgeRecord:
    original = get_record(session, user=user, record_id=record_id)
    if original is None:
        raise KnowledgeError("Опубликованная запись не найдена", 404)
    if original.scope != KnowledgeScope.POSITION.value or (
        original.status != KnowledgeStatus.PUBLISHED.value
    ):
        raise KnowledgeError("Копировать можно только опубликованные записи", 403)
    copy = KnowledgeRecord(
        id=uuid4(),
        owner_id=user.id,
        source_material_id=original.source_material_id,
        copied_from_id=original.id,
        original_author_id=original.original_author_id or original.owner_id,
        category=original.category,
        title=original.title,
        content=original.content,
        scope=KnowledgeScope.PERSONAL.value,
        status=KnowledgeStatus.DRAFT.value,
        record_origin="copied",
    )
    session.add(copy)
    session.flush()
    _audit(session, user, AuditEventType.RECORD_COPIED, "record", copy.id)
    return copy


def get_record(session: Session, *, user: User, record_id: UUID) -> KnowledgeRecord | None:
    set_local_user_id(session, user.id)
    record = session.get(KnowledgeRecord, record_id)
    if record is None or not record.is_current:
        return None
    assignment = current_access(session, user)
    if not can_read(
        assignment.context,
        _resource(
            session,
            owner_id=record.owner_id,
            scope=record.scope,
            status=record.status,
            position_id=record.position_id,
        ),
    ):
        return None
    if record.owner_id != user.id:
        _audit(session, user, AuditEventType.KNOWLEDGE_ITEM_VIEWED, "record", record.id)
    return record


def list_records(
    session: Session,
    *,
    user: User,
    space: str,
    author_id: UUID | None = None,
) -> Sequence[KnowledgeRecord]:
    set_local_user_id(session, user.id)
    assignment = current_access(session, user)
    query = select(KnowledgeRecord).order_by(
        KnowledgeRecord.updated_at.desc(),
        KnowledgeRecord.id,
    ).where(KnowledgeRecord.is_current.is_(True))
    if space == "mine":
        query = query.where(KnowledgeRecord.owner_id == user.id)
    elif space == "position":
        query = query.where(
            KnowledgeRecord.position_id == assignment.context.position_id,
            KnowledgeRecord.status == KnowledgeStatus.PUBLISHED.value,
        )
    elif space == "department":
        query = (
            query.join(Position, Position.id == KnowledgeRecord.position_id)
            .where(
                Position.department_id == assignment.context.department_id,
                KnowledgeRecord.status == KnowledgeStatus.PUBLISHED.value,
            )
        )
    elif space == "review":
        if user.role is not Role.DEPARTMENT_MANAGER:
            raise KnowledgeError("Очередь проверки доступна только руководителю отдела", 403)
        query = (
            query.join(Position, Position.id == KnowledgeRecord.position_id)
            .where(
                Position.department_id == assignment.context.department_id,
                KnowledgeRecord.status == KnowledgeStatus.IN_REVIEW.value,
            )
        )
    else:
        raise KnowledgeError("Неизвестное пространство", 422)
    if author_id is not None:
        if space not in {"position", "department"}:
            raise KnowledgeError("Фильтр автора доступен только в общих пространствах", 422)
        query = query.where(KnowledgeRecord.owner_id == author_id)
    records = list(session.scalars(query.limit(100)))
    visible = [
        record
        for record in records
        if can_read(
            assignment.context,
            _resource(
                session,
                owner_id=record.owner_id,
                scope=record.scope,
                status=record.status,
                position_id=record.position_id,
            ),
        )
    ]
    if space != "mine":
        _audit(session, user, AuditEventType.KNOWLEDGE_SPACE_VIEWED, None, None)
    return visible


def list_materials(
    session: Session,
    *,
    user: User,
    space: str,
    author_id: UUID | None = None,
) -> Sequence[Material]:
    set_local_user_id(session, user.id)
    assignment = current_access(session, user)
    query = select(Material).order_by(Material.updated_at.desc(), Material.id)
    if space == "mine":
        query = query.where(Material.owner_id == user.id)
    elif space == "position":
        query = query.where(
            Material.position_id == assignment.context.position_id,
            Material.status == KnowledgeStatus.PUBLISHED.value,
        )
    elif space == "department":
        query = (
            query.join(Position, Position.id == Material.position_id)
            .where(
                Position.department_id == assignment.context.department_id,
                Material.status == KnowledgeStatus.PUBLISHED.value,
            )
        )
    elif space == "review":
        if user.role is not Role.DEPARTMENT_MANAGER:
            raise KnowledgeError("Очередь проверки доступна только руководителю отдела", 403)
        query = (
            query.join(Position, Position.id == Material.position_id)
            .where(
                Position.department_id == assignment.context.department_id,
                Material.status == KnowledgeStatus.IN_REVIEW.value,
            )
        )
    else:
        raise KnowledgeError("Неизвестное пространство", 422)
    if author_id is not None:
        if space not in {"position", "department"}:
            raise KnowledgeError("Фильтр автора доступен только в общих пространствах", 422)
        query = query.where(Material.owner_id == author_id)
    materials = list(session.scalars(query.limit(100)))
    visible = [
        material
        for material in materials
        if can_read(
            assignment.context,
            _resource(
                session,
                owner_id=material.owner_id,
                scope=material.scope,
                status=material.status,
                position_id=material.position_id,
            ),
        )
    ]
    if space != "mine":
        _audit(session, user, AuditEventType.KNOWLEDGE_SPACE_VIEWED, None, None)
    return visible


def audit_material_view(session: Session, *, user: User, material: Material) -> None:
    if material.owner_id != user.id:
        _audit(session, user, AuditEventType.KNOWLEDGE_ITEM_VIEWED, "material", material.id)


def get_material_view(
    session: Session,
    *,
    user: User,
    material_id: UUID,
) -> tuple[Material, MaterialVersion, dict[str, int]] | None:
    set_local_user_id(session, user.id)
    material = session.get(Material, material_id)
    if material is None:
        return None
    assignment = current_access(session, user)
    if not can_read(
        assignment.context,
        _resource(
            session,
            owner_id=material.owner_id,
            scope=material.scope,
            status=material.status,
            position_id=material.position_id,
        ),
    ):
        return None
    version = session.scalar(
        select(MaterialVersion)
        .where(MaterialVersion.material_id == material.id)
        .order_by(MaterialVersion.version_number.desc())
        .limit(1)
    )
    if version is None:
        return None
    redactions = session.scalars(
        select(RedactionEvent).where(RedactionEvent.version_id == version.id)
    )
    audit_material_view(session, user=user, material=material)
    return (
        material,
        version,
        {event.finding_type: event.redacted_count for event in redactions},
    )
