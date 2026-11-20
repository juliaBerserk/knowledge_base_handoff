import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from estafeta.api.auth import COOKIE
from estafeta.auth.security import csrf_token, hash_password
from estafeta.auth.service import create_session, issue_invitation
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.ingest.formats import SourceKind
from estafeta.main import app
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
from estafeta.models.audit import Action, AuditEvent
from estafeta.models.auth import AuthSession, Credential, Invitation
from estafeta.models.handoff import Handoff
from estafeta.workers.handoffs import process_due

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")


@pytest.fixture
def org() -> Iterator[tuple[sa.Engine, sessionmaker[Session], dict[str, UUID], dict[str, str]]]:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    worker = sa.create_engine(get_settings().database_worker_url, hide_parameters=True)
    ids: dict[str, UUID] = {}
    tokens: dict[str, str] = {}
    employees: list[UUID] = []
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        for dept in ("local", "remote"):
            team = Department(name=f"Отдел {dept} {uuid4()}")
            db.add(team)
            db.flush()
            ids[f"dept_{dept}"] = team.id
            for job in ("staff", "head"):
                position = Position(title=job, department_id=team.id)
                db.add(position)
                db.flush()
                ids[f"pos_{job}_{dept}"] = position.id
        for key, role, position in (
            ("hr", Role.HR, None),
            ("employee", Role.EMPLOYEE, "pos_staff_local"),
            ("successor", Role.EMPLOYEE, "pos_staff_remote"),
            ("head", Role.DEPARTMENT_HEAD, "pos_head_local"),
            ("outsider", Role.DEPARTMENT_HEAD, "pos_head_remote"),
        ):
            employee = Employee(full_name=f"Сотрудник {key}", role=role)
            db.add(employee)
            db.flush()
            employees.append(employee.id)
            ids[key] = employee.id
            db.add(
                Credential(
                    employee_id=employee.id,
                    password_hash="fixture",
                    totp_secret=b"fixture" if role is Role.DEPARTMENT_HEAD else None,
                )
            )
            db.flush()
            tokens[key] = create_session(db, employee.id, now, False)
            if position:
                db.add(
                    PositionAssignment(
                        employee_id=employee.id,
                        position_id=ids[position],
                        started_at=now - timedelta(days=5),
                    )
                )
                db.flush()
        spaces = (
            KnowledgeSpace(kind=SpaceKind.PERSONAL, employee_id=ids["employee"]),
            KnowledgeSpace(kind=SpaceKind.POSITION, position_id=ids["pos_staff_local"]),
        )
        for space, key, state in zip(
            spaces, ("private", "published"), ("draft", "published"), strict=True
        ):
            db.add(space)
            db.flush()
            content = f"Содержимое {key} только для проверки доступа"
            item = Material(
                space_id=space.id,
                space_kind=space.kind,
                author_id=ids["employee"],
                status=state,
                cleaned_text=content,
                source_kind=SourceKind.NOTE,
            )
            record = KnowledgeRecord(
                space_id=space.id,
                space_kind=space.kind,
                author_id=ids["employee"],
                status=state,
                cleaned_text=content,
                category="people" if key == "private" else "risks",
            )
            db.add_all([item, record])
            db.flush()
            ids[f"space_{key}"] = space.id
            ids[f"material_{key}"] = item.id
            ids[f"record_{key}"] = record.id
    try:
        yield engine, sessionmaker(worker), ids, tokens
    finally:
        with Session(engine) as db, db.begin():
            db.execute(sa.delete(Handoff).where(Handoff.employee_id.in_(employees)))
            for table in (KnowledgeRecord, Material):
                db.execute(sa.delete(table).where(table.author_id.in_(employees)))
            db.execute(
                sa.delete(KnowledgeSpace).where(
                    KnowledgeSpace.id.in_([ids["space_private"], ids["space_published"]])
                )
            )
            db.execute(
                sa.delete(PositionAssignment).where(PositionAssignment.employee_id.in_(employees))
            )
            db.execute(
                sa.delete(Position).where(
                    Position.department_id.in_((ids["dept_local"], ids["dept_remote"]))
                )
            )
            db.execute(
                sa.delete(Department).where(
                    Department.id.in_((ids["dept_local"], ids["dept_remote"]))
                )
            )
            db.execute(sa.delete(Credential).where(Credential.employee_id.in_(employees)))
            db.execute(sa.delete(Employee).where(Employee.id.in_(employees)))
        engine.dispose()
        worker.dispose()


type Org = tuple[sa.Engine, sessionmaker[Session], dict[str, UUID], dict[str, str]]


def sign(client: TestClient, tokens: dict[str, str], user: str) -> dict[str, str]:
    token = tokens[user]
    client.cookies.set(COOKIE, token)
    return {"X-CSRF-Token": csrf_token(get_settings().app_secret_key, token)}


def payload(ids: dict[str, UUID]) -> dict[str, str]:
    return {
        "employee_id": str(ids["employee"]),
        "position_id": str(ids["pos_staff_local"]),
        "effective_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }


def make_due(engine: sa.Engine, handoff: UUID) -> datetime:
    cutoff = datetime.now(UTC) - timedelta(minutes=1)
    with Session(engine) as db, db.begin():
        db.execute(sa.update(Handoff).where(Handoff.id == handoff).values(effective_at=cutoff))
    return cutoff


@pytest.mark.parametrize("mode", ["archive", "delete"])
def test_departure_cutoff_successor_and_retention(
    org: Org,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    engine, factory, ids, tokens = org
    monkeypatch.setattr(get_settings(), "handoff_retention", mode)
    valid_login = {"login": str(ids["employee"]), "password": "Тестовый пароль эстафеты Ж7!"}
    with Session(engine) as db, db.begin():
        db.get(Credential, ids["employee"]).password_hash = hash_password(valid_login["password"])
        assert issue_invitation(db, ids["employee"], datetime.now(UTC))
    private = f"/api/publication/materials/{ids['material_private']}"
    public = f"/api/publication/records/{ids['record_published']}"
    with TestClient(app, base_url="https://testserver") as client:
        assert client.post("/api/auth/login", json=valid_login).status_code == 200
        other = sign(client, tokens, "outsider")
        assert client.post("/api/handoffs", headers=other, json=payload(ids)).status_code == 409
        head = sign(client, tokens, "head")
        assert client.get(private).status_code == 404
        assert (
            client.post(
                "/api/handoffs",
                headers=head,
                json=payload(ids)
                | {
                    "successor_id": str(ids["successor"]),
                },
            ).status_code
            == 409
        )
        sign(client, tokens, "employee")
        assert client.get("/api/handoffs/options").status_code == 403
        hr = sign(client, tokens, "hr")
        options = client.get("/api/handoffs/options")
        assert options.status_code == 200
        assert str(ids["employee"]) in {item["id"] for item in options.json()["employees"]}
        data = payload(ids) | {"successor_id": str(ids["successor"])}
        assert client.post("/api/handoffs", json=data).status_code == 403
        created = client.post("/api/handoffs", headers=hr, json=data)
        assert created.status_code == 201, created.text
        handoff = UUID(created.json()["id"])
        detail = f"/api/handoffs/{handoff}"
        assert client.post("/api/handoffs", headers=hr, json=data).status_code == 409
        assert client.get(private).status_code == 403
        overview = client.get(detail).json()
        assert overview["completeness"]["people"] == 1
        assert overview["completeness"]["risks"] == 1
        assert "Содержимое" not in str(overview)
        sign(client, tokens, "employee")
        assert client.get(detail).json()["completeness"]["people"] == 1
        sign(client, tokens, "successor")
        assert client.get(detail).status_code == 404
        assert client.get(public).status_code == 404
        cutoff = make_due(engine, handoff)
        sign(client, tokens, "employee")
        assert client.get("/api/auth/session").status_code == 401
        assert client.post("/api/auth/login", json=valid_login).status_code == 401
        assert process_due(factory) == 1
        assert process_due(factory) == 0
        with Session(engine) as db:
            assert db.get(Credential, ids["employee"]).blocked_at is not None
            assert (
                db.scalar(sa.select(AuthSession).where(AuthSession.employee_id == ids["employee"]))
                is None
            )
            assert (
                db.scalar(sa.select(Invitation).where(Invitation.employee_id == ids["employee"]))
                is None
            )
            saved = db.get(Handoff, handoff)
            assert saved and saved.review_until == cutoff + timedelta(days=30)
        sign(client, tokens, "head")
        assert client.get(private).status_code == 200
        sign(client, tokens, "outsider")
        assert client.get(private).status_code == 404
        head = sign(client, tokens, "head")
        selected = f"/api/publication/records/{ids['record_private']}/publish"
        promoted = client.post(selected, json={}, headers=head)
        assert promoted.status_code == 200, promoted.text
        sign(client, tokens, "hr")
        assert client.get(detail + "/start").status_code == 403
        sign(client, tokens, "successor")
        assert client.get(public).status_code == 200
        start = client.get(detail + "/start")
        assert start.status_code == 200, start.text
        assert {item["id"] for item in start.json()["items"]} == {
            str(ids["record_published"]),
            str(ids["material_published"]),
            str(ids["record_private"]),
        }
        assert client.get(private).status_code == 404

        expired = datetime.now(UTC) - timedelta(seconds=1)
        with Session(engine) as db, db.begin():
            db.execute(sa.update(Handoff).where(Handoff.id == handoff).values(review_until=expired))
            db.execute(
                sa.update(KnowledgeSpace)
                .where(
                    KnowledgeSpace.id == ids["space_private"],
                )
                .values(review_until=expired)
            )
        sign(client, tokens, "head")
        assert client.get(private).status_code == 404
        assert process_due(factory) == 1
        sign(client, tokens, "hr")
        after = client.get(detail).json()
        assert after["status"] == "completed" and after["completeness"]["people"] == 1
    with Session(engine) as db:
        saved = db.get(Material, ids["material_private"])
        assert (saved is None) if mode == "delete" else (saved and saved.status == "archived")
        published = db.get(KnowledgeRecord, ids["record_published"])
        assert published and published.author_id == ids["employee"]
        rescued = db.get(KnowledgeRecord, ids["record_private"])
        assert rescued and rescued.status == "published" and rescued.author_id == ids["employee"]
        events = db.scalars(
            sa.select(AuditEvent.action)
            .where(
                AuditEvent.target_id == handoff,
            )
            .order_by(AuditEvent.id)
        ).all()
        assert events == [
            Action.HANDOFF_CREATED,
            Action.HANDOFF_ACTIVATED,
            Action.HANDOFF_REVIEW_CLOSED,
        ]


def test_transfer_and_cancel_do_not_block_employee(org: Org) -> None:
    engine, factory, ids, tokens = org
    data = payload(ids) | {"target_position_id": str(ids["pos_staff_remote"])}
    with TestClient(app, base_url="https://testserver") as client:
        head = sign(client, tokens, "head")
        assert client.post("/api/handoffs", headers=head, json=data).status_code == 409
        hr = sign(client, tokens, "hr")
        first = client.post("/api/handoffs", headers=hr, json=data)
        assert first.status_code == 201, first.text
        assert (
            client.post(
                f"/api/handoffs/{first.json()['id']}/cancel", headers=hr, json={}
            ).status_code
            == 200
        )
        second = client.post("/api/handoffs", headers=hr, json=data)
        assert second.status_code == 201, second.text
        make_due(engine, UUID(second.json()["id"]))
        assert process_due(factory) == 1
        sign(client, tokens, "employee")
        assert client.get("/api/auth/session").status_code == 200
        public = f"/api/publication/records/{ids['record_published']}"
        assert client.get(public).status_code == 404
        assert (
            client.get(f"/api/publication/materials/{ids['material_private']}").status_code == 200
        )
    with Session(engine) as db:
        assignment = db.scalar(
            sa.select(PositionAssignment).where(
                PositionAssignment.employee_id == ids["employee"],
                PositionAssignment.ended_at.is_(None),
            )
        )
        assert assignment and assignment.position_id == ids["pos_staff_remote"]
        assert db.get(KnowledgeSpace, ids["space_private"]).review_until is None


def test_changed_successor_is_not_moved_and_departure_still_blocks(org: Org) -> None:
    engine, factory, ids, tokens = org
    with TestClient(app, base_url="https://testserver") as client:
        hr = sign(client, tokens, "hr")
        response = client.post(
            "/api/handoffs",
            headers=hr,
            json=payload(ids)
            | {
                "successor_id": str(ids["successor"]),
            },
        )
        assert response.status_code == 201, response.text
        handoff_id = UUID(response.json()["id"])
        next_assignment = datetime.now(UTC) + timedelta(hours=2)
        with Session(engine) as db, db.begin():
            db.execute(
                sa.update(PositionAssignment)
                .where(
                    PositionAssignment.employee_id == ids["successor"],
                    PositionAssignment.ended_at.is_(None),
                )
                .values(ended_at=next_assignment)
            )
            db.add(
                PositionAssignment(
                    employee_id=ids["successor"],
                    position_id=ids["pos_head_remote"],
                    started_at=next_assignment,
                )
            )
        make_due(engine, handoff_id)
        assert process_due(factory) == 1
        item = client.get(f"/api/handoffs/{handoff_id}").json()
        assert item["status"] == "review"
        assert item["issue"] == "successor_changed" and not item["successor_assigned"]
        sign(client, tokens, "employee")
        assert client.get("/api/auth/session").status_code == 401
        sign(client, tokens, "successor")
        assert client.get(f"/api/handoffs/{handoff_id}").status_code == 404
    with Session(engine) as db:
        current = db.scalar(
            sa.select(PositionAssignment).where(
                PositionAssignment.employee_id == ids["successor"],
                PositionAssignment.ended_at.is_(None),
            )
        )
        assert current and current.position_id == ids["pos_head_remote"]
