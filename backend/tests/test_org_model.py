import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from estafeta.core.config import MigrationSettings
from estafeta.models import (
    Department,
    Employee,
    KnowledgeRecord,
    KnowledgeSpace,
    KnowledgeStatus,
    Material,
    Position,
    PositionAssignment,
    Role,
    SpaceKind,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="нужен PostgreSQL с применёнными миграциями (RUN_DB_TESTS=1)",
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(MigrationSettings().database_owner_url)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def org_unit(db: Session) -> tuple[Department, Position, Employee]:
    department = Department(name=f"Отдел {uuid4()}")
    employee = Employee(full_name="Тестовый сотрудник", role=Role.EMPLOYEE)
    db.add_all([department, employee])
    db.flush()
    position = Position(department_id=department.id, title="Специалист")
    db.add(position)
    db.flush()
    return department, position, employee


def test_position_space_survives_vacancy_and_keeps_author(db: Session) -> None:
    _, position, author = org_unit(db)
    successor = Employee(full_name="Тестовый преемник", role=Role.EMPLOYEE)
    space = KnowledgeSpace(kind=SpaceKind.POSITION, position_id=position.id)
    started = datetime(2025, 1, 1, tzinfo=UTC)
    db.add_all([successor, space])
    db.flush()
    assignment = PositionAssignment(
        employee_id=author.id, position_id=position.id, started_at=started
    )
    record = KnowledgeRecord(
        author_id=author.id,
        space_id=space.id,
        space_kind=SpaceKind.POSITION,
        status=KnowledgeStatus.PUBLISHED,
    )
    db.add_all([assignment, record])
    db.flush()
    assignment.ended_at = started + timedelta(days=1)
    db.flush()
    db.add(
        PositionAssignment(
            employee_id=successor.id,
            position_id=position.id,
            started_at=started + timedelta(days=2),
        )
    )
    db.flush()
    db.refresh(record)
    assert record.author_id == author.id
    assert record.space_id == space.id
    assert record.status is KnowledgeStatus.PUBLISHED


def test_employee_cannot_have_two_open_assignments(db: Session) -> None:
    _, position, employee = org_unit(db)
    for day in (1, 2):
        db.add(
            PositionAssignment(
                employee_id=employee.id,
                position_id=position.id,
                started_at=datetime(2025, 1, day, tzinfo=UTC),
            )
        )
    with pytest.raises(IntegrityError, match="uq_assignment_open_employee"):
        db.flush()


def test_space_requires_owner_matching_kind(db: Session) -> None:
    department, _, _ = org_unit(db)
    db.add(KnowledgeSpace(kind=SpaceKind.PERSONAL, department_id=department.id))
    with pytest.raises(IntegrityError, match="ck_space_binding"):
        db.flush()


def test_personal_space_is_unique_per_employee(db: Session) -> None:
    _, _, employee = org_unit(db)
    db.add_all(
        [
            KnowledgeSpace(kind=SpaceKind.PERSONAL, employee_id=employee.id),
            KnowledgeSpace(kind=SpaceKind.PERSONAL, employee_id=employee.id),
        ]
    )
    with pytest.raises(IntegrityError):
        db.flush()


@pytest.mark.parametrize(
    ("model", "constraint"),
    [
        (Material, "ck_material_space_status"),
        (KnowledgeRecord, "ck_record_space_status"),
    ],
)
@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (SpaceKind.PERSONAL, KnowledgeStatus.PUBLISHED),
        (SpaceKind.POSITION, KnowledgeStatus.DRAFT),
        (SpaceKind.DEPARTMENT, KnowledgeStatus.PUBLISHED),
    ],
)
def test_knowledge_status_matches_space(
    db: Session,
    model: type[Material] | type[KnowledgeRecord],
    constraint: str,
    kind: SpaceKind,
    status: KnowledgeStatus,
) -> None:
    department, position, employee = org_unit(db)
    space = KnowledgeSpace(
        kind=kind,
        employee_id=employee.id if kind is SpaceKind.PERSONAL else None,
        position_id=position.id if kind is SpaceKind.POSITION else None,
        department_id=department.id if kind is SpaceKind.DEPARTMENT else None,
    )
    db.add(space)
    db.flush()
    db.add(model(author_id=employee.id, space_id=space.id, space_kind=kind, status=status))
    with pytest.raises(IntegrityError, match=constraint):
        db.flush()


def test_material_cannot_claim_different_space_kind(db: Session) -> None:
    _, position, employee = org_unit(db)
    space = KnowledgeSpace(kind=SpaceKind.POSITION, position_id=position.id)
    db.add(space)
    db.flush()
    db.add(
        Material(
            author_id=employee.id,
            space_id=space.id,
            space_kind=SpaceKind.PERSONAL,
            status=KnowledgeStatus.DRAFT,
        )
    )
    with pytest.raises(IntegrityError, match="fk_material_space_binding"):
        db.flush()
