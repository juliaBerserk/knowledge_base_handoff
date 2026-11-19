import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from estafeta.auth import security
from estafeta.core.config import MigrationSettings
from estafeta.core.db import get_admin_session_factory, get_session_factory
from estafeta.core.knowledge_access import knowledge_transaction, read_knowledge
from estafeta.models import (
    Department,
    Employee,
    KnowledgeRecord,
    KnowledgeSpace,
    Material,
    Position,
    PositionAssignment,
    Role,
    SpaceKind,
)
from estafeta.models import KnowledgeStatus as Status
from estafeta.models.audit import Action, AuditEvent
from estafeta.models.auth import AuthSession, Credential

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")


@pytest.fixture(scope="module")
def data() -> Iterator[tuple[dict[str, UUID], dict[str, str]]]:
    engine = sa.create_engine(MigrationSettings().database_owner_url)
    ids: dict[str, UUID] = {}
    tokens: dict[str, str] = {}
    now = datetime.now(UTC)
    items: list[UUID] = []
    assignments: list[UUID] = []
    spaces: list[UUID] = []
    employees: list[UUID] = []
    positions: list[UUID] = []
    departments: list[UUID] = []
    with Session(engine) as db, db.begin():
        for group in ("team", "other"):
            department = Department(name=f"{group}-{uuid4()}")
            db.add(department)
            db.flush()
            departments.append(department.id)
            for name in ("lead", "staff"):
                position = Position(title=name, department_id=department.id)
                db.add(position)
                db.flush()
                ids[f"{group}_{name}"] = position.id
                positions.append(position.id)
            ids[group] = department.id

        profiles = {
            "owner": (Role.EMPLOYEE, "team_staff"),
            "head": (Role.DEPARTMENT_HEAD, "team_lead"),
            "other_head": (Role.DEPARTMENT_HEAD, "other_lead"),
            "successor": (Role.EMPLOYEE, "team_staff"),
            "former": (Role.EMPLOYEE, "team_staff"),
            "departed": (Role.EMPLOYEE, "team_staff"),
            "admin": (Role.ADMIN, None),
            "hr": (Role.HR, "team_staff"),
            "auditor": (Role.SECURITY_AUDITOR, "team_staff"),
        }
        for name, (role, position_name) in profiles.items():
            employee = Employee(full_name=name, role=role)
            db.add(employee)
            db.flush()
            ids[name] = employee.id
            employees.append(employee.id)
            db.add(
                Credential(
                    employee_id=employee.id,
                    password_hash="fixture",
                    blocked_at=now if name == "departed" else None,
                    totp_secret=b"fixture" if role in {Role.ADMIN, Role.DEPARTMENT_HEAD} else None,
                )
            )
            db.flush()
            tokens[name] = security.new_token()
            db.add(
                AuthSession(
                    employee_id=employee.id,
                    token_hash=security.token_hash(tokens[name]),
                    enrollment_only=False,
                    created_at=now,
                    last_seen_at=now,
                )
            )
            if position_name is not None:
                assignment = PositionAssignment(
                    employee_id=employee.id,
                    position_id=ids[position_name],
                    started_at=now - timedelta(days=2),
                    ended_at=now - timedelta(days=1) if name in {"departed", "former"} else None,
                )
                db.add(assignment)
                db.flush()
                assignments.append(assignment.id)

        review_properties = {
            "personal": {},
            "handoff": {
                "review_department_id": ids["team"],
                "review_until": now + timedelta(days=1),
            },
        }
        for name, props in review_properties.items():
            space = KnowledgeSpace(
                kind=SpaceKind.PERSONAL,
                employee_id=ids["owner" if name == "personal" else "departed"],
                **props,
            )
            db.add(space)
            db.flush()
            ids[name] = space.id
            spaces.append(space.id)

        role_space = KnowledgeSpace(kind=SpaceKind.POSITION, position_id=ids["team_staff"])
        db.add(role_space)
        db.flush()
        ids["role_space"] = role_space.id
        spaces.append(role_space.id)

        for name, space_name, status in (
            ("draft", "personal", Status.DRAFT),
            ("submitted", "personal", Status.IN_REVIEW),
            ("handoff_draft", "handoff", Status.DRAFT),
            ("published", "role_space", Status.PUBLISHED),
            ("archived", "role_space", Status.ARCHIVED),
        ):
            space_kind = SpaceKind.POSITION if space_name == "role_space" else SpaceKind.PERSONAL
            for model in (Material, KnowledgeRecord):
                item = model(
                    space_id=ids[space_name],
                    space_kind=space_kind,
                    status=status,
                    author_id=ids["departed" if space_name == "handoff" else "owner"],
                )
                db.add(item)
                db.flush()
                ids[f"{name}_{model.__tablename__}"] = item.id
                items.append(item.id)
    try:
        yield ids, tokens
    finally:
        with Session(engine) as db, db.begin():
            for model, values in (
                (Material, items),
                (KnowledgeRecord, items),
                (KnowledgeSpace, spaces),
                (PositionAssignment, assignments),
                (Position, positions),
                (Department, departments),
            ):
                db.execute(sa.delete(model).where(model.id.in_(values)))
            db.execute(sa.delete(Credential).where(Credential.employee_id.in_(employees)))
            db.execute(sa.delete(Employee).where(Employee.id.in_(employees)))
        engine.dispose()


def set_actor(db: Session, employee_id: UUID) -> None:
    db.execute(sa.text("SELECT set_config('app.user_id', :id, true)"), {"id": str(employee_id)})


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        ("owner", {"draft", "submitted", "published"}),
        ("head", {"submitted", "handoff_draft", "published"}),
        ("other_head", set()),
        ("successor", {"published"}),
        ("former", set()),
        ("departed", set()),
        ("admin", set()),
        ("hr", set()),
        ("auditor", set()),
    ],
)
def test_raw_sql_uses_rls(
    data: tuple[dict[str, UUID], dict[str, str]], actor: str, expected: set[str]
) -> None:
    ids, _ = data
    with get_session_factory().begin() as db:
        set_actor(db, ids[actor])
        for model in (Material, KnowledgeRecord):
            actual = set(db.scalars(sa.select(model.id)))
            assert actual == {ids[f"{name}_{model.__tablename__}"] for name in expected}
        if actor == "head":
            assert ids["personal"] not in set(db.scalars(sa.select(KnowledgeSpace.id)))
            assert ids["handoff"] in set(db.scalars(sa.select(KnowledgeSpace.id)))


def test_admin_db_role_has_no_select(data: tuple[dict[str, UUID], dict[str, str]]) -> None:
    ids, tokens = data
    with get_admin_session_factory().begin() as db, pytest.raises(ProgrammingError) as error:
        set_actor(db, ids["owner"])
        db.execute(sa.text("RESET ROLE"))
        assert db.scalar(sa.text("SELECT current_user")) == "estafeta_admin"
        db.execute(sa.select(Material.id))
    assert error.value.orig.sqlstate == "42501"
    with knowledge_transaction(tokens["admin"]) as db:
        assert db is None


def test_context_is_transaction_local_and_repository_checks_permissions(
    data: tuple[dict[str, UUID], dict[str, str]],
) -> None:
    ids, tokens = data
    with knowledge_transaction(tokens["owner"]) as db:
        assert db is not None
        assert read_knowledge(db, Material, ids["draft_materials"]) is not None
        assert read_knowledge(db, Material, ids["handoff_draft_materials"]) is None
    with get_session_factory().begin() as db:
        assert db.scalar(sa.select(Material.id)) is None
        assert db.scalar(sa.text("SELECT current_user")) == "estafeta_app"
    with knowledge_transaction(tokens["departed"]) as db:
        assert db is None


def test_successful_foreign_read_is_audited(
    data: tuple[dict[str, UUID], dict[str, str]],
) -> None:
    ids, tokens = data
    with knowledge_transaction(tokens["head"]) as db:
        assert db is not None
        assert read_knowledge(db, Material, ids["draft_materials"]) is None
        assert read_knowledge(db, Material, ids["submitted_materials"]) is not None
    engine = sa.create_engine(MigrationSettings().database_owner_url)
    try:
        with Session(engine) as db:
            items = db.scalars(
                sa.select(AuditEvent.target_id).where(
                    AuditEvent.action == Action.KNOWLEDGE_VIEWED,
                    AuditEvent.actor_id == ids["head"],
                )
            )
            assert list(items) == [ids["submitted_materials"]]
    finally:
        engine.dispose()
