import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from estafeta.api import administration
from estafeta.api.auth import COOKIE
from estafeta.auth.security import csrf_token, token_hash
from estafeta.auth.service import create_session
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.core.db import get_admin_session_factory
from estafeta.main import app
from estafeta.models import (
    Department,
    Employee,
    Position,
    PositionAssignment,
    PublicationSettings,
    Role,
)
from estafeta.models.audit import Action, AuditEvent, Target
from estafeta.models.auth import Credential, Invitation

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")


@pytest.fixture
def directory() -> Iterator[tuple[sa.Engine, str, dict[str, str]]]:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    tag = uuid4().hex[:12]
    tokens = {}
    with Session(engine) as db, db.begin():
        for account, role in (("admin", Role.ADMIN), ("enrollment", Role.ADMIN), ("hr", Role.HR)):
            employee = Employee(full_name=f"{tag} {account}", role=role)
            db.add(employee)
            db.flush()
            db.add(
                Credential(
                    employee_id=employee.id,
                    password_hash="fixture",
                    totp_secret=b"fixture" if account == "admin" else None,
                )
            )
            db.flush()
            tokens[account] = create_session(
                db, employee.id, datetime.now(UTC), account == "enrollment"
            )
    try:
        yield engine, tag, tokens
    finally:
        with Session(engine) as db, db.begin():
            employee_ids = list(
                db.scalars(sa.select(Employee.id).where(Employee.full_name.startswith(tag)))
            )
            department_ids = list(
                db.scalars(sa.select(Department.id).where(Department.name.startswith(tag)))
            )
            db.execute(
                sa.delete(PositionAssignment).where(
                    PositionAssignment.employee_id.in_(employee_ids)
                )
            )
            db.execute(sa.delete(Credential).where(Credential.employee_id.in_(employee_ids)))
            db.execute(sa.delete(Employee).where(Employee.id.in_(employee_ids)))
            db.execute(sa.delete(Position).where(Position.department_id.in_(department_ids)))
            db.execute(sa.delete(Department).where(Department.id.in_(department_ids)))
        engine.dispose()


type Directory = tuple[sa.Engine, str, dict[str, str]]


def sign(client: TestClient, token: str) -> dict[str, str]:
    client.cookies.set(COOKIE, token)
    return {"X-CSRF-Token": csrf_token(get_settings().app_secret_key, token)}


def organization(client: TestClient, headers: dict[str, str], tag: str) -> tuple[str, str, str]:
    department = f"{tag} Поддержка"
    position = "Специалист"
    unit = client.post("/api/admin/departments", json={"name": department}, headers=headers)
    assert unit.status_code == 201, unit.text
    slot = client.post(
        "/api/admin/positions",
        headers=headers,
        json={
            "department_id": unit.json()["id"],
            "title": position,
        },
    )
    assert slot.status_code == 201, slot.text
    return department, position, slot.json()["id"]


def test_manual_provision_and_password_set(directory: Directory, monkeypatch: pytest.MonkeyPatch):
    engine, tag, tokens = directory
    emails = []

    def accept(_settings, recipients):
        emails.extend(recipients)
        return {0} if recipients else set()

    monkeypatch.setattr(administration, "send_invitations", accept)
    with TestClient(app, base_url="https://testserver") as client:
        for role in ("hr", "enrollment"):
            sign(client, tokens[role])
            assert client.get("/api/admin/accounts").status_code == 403
        headers = sign(client, tokens["admin"])
        _, _, position = organization(client, headers, tag)
        body = {
            "full_name": f"{tag} Иванов Иван",
            "corporate_email": f"{tag}@org.test",
            "position_id": position,
            "role": "employee",
        }
        assert client.post("/api/admin/accounts", json=body).status_code == 403
        result = client.post("/api/admin/accounts", json=body, headers=headers)
        assert result.status_code == 201, result.text
        data = result.json()
        assert data["sent"] and data["url"] is None and len(emails) == 1
        member = UUID(data["id"])
        token = emails[0][1].split("#invite=", 1)[1]
        with Session(engine) as db:
            invitation = db.scalar(sa.select(Invitation).where(Invitation.employee_id == member))
            assert invitation.token_hash == token_hash(token)
            assert db.get(Credential, member).password_hash is None
        accounts = client.get("/api/admin/accounts", params={"q": body["corporate_email"]})
        assert accounts.status_code == 200, accounts.text
        [entry] = accounts.json()["items"]
        assert entry["status"] == "invited" and entry["position"] == "Специалист"
        assert "password_hash" not in entry and "token_hash" not in entry
        assert client.post("/api/admin/accounts", json=body, headers=headers).status_code == 409
        activated = client.post(
            "/api/auth/redeem",
            json={
                "token": token,
                "password": "Тестовый пароль сотрудника Ж7!",
            },
        )
        assert activated.status_code == 200
        [entry] = client.get(
            "/api/admin/accounts",
            params={
                "q": body["corporate_email"],
            },
        ).json()["items"]
        assert entry["status"] == "active"
    with Session(engine) as db:
        actions = list(
            db.scalars(
                sa.select(AuditEvent.action)
                .where(
                    AuditEvent.target_kind == Target.EMPLOYEE,
                    AuditEvent.target_id == member,
                )
                .order_by(AuditEvent.id)
            )
        )
        assert actions == [Action.ACCOUNT_CREATED, Action.INVITATION_ISSUED]


def test_batch_preview_and_import_are_atomic(directory: Directory):
    engine, tag, tokens = directory
    with TestClient(app, base_url="https://testserver") as client:
        headers = sign(client, tokens["admin"])
        dept, position, _ = organization(client, headers, tag)
        good = (
            f"ФИО;корпоративная почта;отдел;должность;роль\n{tag} Без почты;;{dept};{position};HR\n"
        )
        bad = good + f"{tag} Ошибка;;{dept};Отсутствует;Сотрудник\n"
        root = "/api/admin/accounts/import"
        rejected = client.post(root, json={"csv_text": bad}, headers=headers)
        assert rejected.status_code == 422
        assert rejected.json()["errors"][0]["row"] == 3
        with Session(engine) as db:
            assert (
                db.scalar(sa.select(Employee.id).where(Employee.full_name == f"{tag} Без почты"))
                is None
            )
        preview = client.post(root + "/preview", json={"csv_text": good}, headers=headers)
        assert preview.status_code == 200, preview.text
        assert preview.json() == {"count": 1}
        result = client.post(root, json={"csv_text": good}, headers=headers)
        assert result.status_code == 201, result.text
        [row] = result.json()["accounts"]
        assert row["row"] == 2 and row["url"] and not row["sent"]
        assert client.post(root, json={"csv_text": good}, headers=headers).status_code == 422


def test_retention_settings_and_database_admin_privileges(directory: Directory):
    engine, _, tokens = directory
    with get_admin_session_factory().begin() as db:
        for table in (
            "knowledge.materials",
            "knowledge.records",
            "knowledge.chunks",
            "knowledge.chat_turns",
            "auth.credentials",
            "auth.invitations",
            "auth.sessions",
        ):
            allowed = db.scalar(
                sa.text("SELECT has_table_privilege(:table, 'SELECT')"), {"table": table}
            )
            assert allowed is False
        assert (
            db.scalar(
                sa.text(
                    "SELECT has_column_privilege('auth.credentials', 'password_hash', 'SELECT')"
                )
            )
            is False
        )
    with Session(engine) as db:
        original = db.get(PublicationSettings, 1)
        body = {"review_required": not original.review_required, "handoff_retention": "delete"}
        initial = {
            "review_required": original.review_required,
            "handoff_retention": original.handoff_retention,
        }
    try:
        with TestClient(app, base_url="https://testserver") as client:
            hr = sign(client, tokens["hr"])
            assert client.post("/api/admin/settings", json=body, headers=hr).status_code == 403
            assert client.get("/api/handoffs/summary").json() == {
                "counts": [],
                "without_successor": 0,
            }
            admin = sign(client, tokens["admin"])
            saved = client.post("/api/admin/settings", json=body, headers=admin)
            assert saved.status_code == 200, saved.text
            assert client.get("/api/admin/settings").json() == body
            assert (
                client.get("/api/settings/publication").json()["review_required"]
                == body["review_required"]
            )
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                sa.update(PublicationSettings).where(PublicationSettings.id == 1).values(**initial)
            )
