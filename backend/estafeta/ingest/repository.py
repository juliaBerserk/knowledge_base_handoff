from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from estafeta.core.audit import append_event
from estafeta.core.permissions import Relation, can_write
from estafeta.ingest.formats import SourceKind
from estafeta.ingest.pipeline import Preview
from estafeta.ingest.policy import Directory, normalize
from estafeta.models import KnowledgeSpace, KnowledgeStatus, Material, SpaceKind
from estafeta.models.audit import Action, Target


def current_uploader(db: Session) -> UUID | None:
    return db.scalar(sa.select(sa.func.knowledge.current_uploader(type_=sa.Uuid())))


def load_directory(db: Session) -> Directory:
    contacts = sa.func.knowledge.directory_contacts().table_valued(
        "full_name", "corporate_email", joins_implicitly=True
    )
    rows = db.execute(sa.select(contacts.c.full_name, contacts.c.corporate_email)).all()
    return Directory(
        names=frozenset(normalize(row.full_name) for row in rows),
        emails=frozenset(row.corporate_email.casefold() for row in rows if row.corporate_email),
    )


def personal_space(db: Session, actor_id: UUID) -> UUID:
    statement = insert(KnowledgeSpace.__table__).values(
        id=uuid4(),
        kind=SpaceKind.PERSONAL,
        employee_id=actor_id,
    )
    db.execute(statement.on_conflict_do_nothing(index_elements=["employee_id"]).inline())
    space = db.scalar(sa.select(KnowledgeSpace.id).where(KnowledgeSpace.employee_id == actor_id))
    if space is None:
        raise PermissionError("Личное пространство недоступно")
    return space


def save_draft(
    db: Session, actor_id: UUID, material_id: UUID, kind: SourceKind, preview: Preview
) -> None:
    if current_uploader(db) != actor_id or not can_write(Relation.OWNER, KnowledgeStatus.DRAFT):
        raise PermissionError("Нет доступа для сохранения черновика")
    space = personal_space(db, actor_id)
    statement = insert(Material.__table__).values(
        id=material_id,
        space_id=space,
        space_kind=SpaceKind.PERSONAL,
        author_id=actor_id,
        status=KnowledgeStatus.DRAFT,
        source_kind=kind,
        cleaned_text=preview.text,
    )
    inserted = db.scalar(
        statement.on_conflict_do_nothing(index_elements=["id"]).returning(Material.id)
    )
    if inserted is None:
        return
    target = (Target.MATERIAL, material_id)
    append_event(db, Action.MATERIAL_UPLOADED, actor_id=actor_id, target=target)
    for kind_of_mask, count in preview.counts.items():
        append_event(
            db,
            Action.MATERIAL_REDACTED,
            actor_id=actor_id,
            target=target,
            redaction=(kind_of_mask, count),
        )
