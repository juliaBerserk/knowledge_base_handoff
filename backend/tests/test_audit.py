import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from estafeta.api.auth import COOKIE
from estafeta.auth.security import hash_password
from estafeta.auth.service import create_session
from estafeta.core.audit import append_event
from estafeta.core.config import MigrationSettings
from estafeta.core.db import get_admin_session_factory, get_session_factory
from estafeta.main import app
from estafeta.models import Employee, Role
from estafeta.models.audit import Action, AuditEvent, Target
from estafeta.models.auth import Credential

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")
PASSWORD = "Тестовый пароль Ж7! для журнала аудита"


@pytest.fixture
def accounts() -> Iterator[dict[Role, tuple[UUID, str]]]:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    identities: dict[Role, tuple[UUID, str]] = {}
    with Session(engine) as db, db.begin():
        for role in (Role.EMPLOYEE, Role.SECURITY_AUDITOR, Role.ADMIN, Role.HR):
            employee = Employee(full_name="Тест аудита", role=role)
            db.add(employee)
            db.flush()
            db.add(
                Credential(
                    employee_id=employee.id,
                    password_hash=hash_password(PASSWORD),
                    totp_secret=b"fixture" if role is Role.ADMIN else None,
                )
            )
            db.flush()
            token = create_session(db, employee.id, datetime.now(UTC), False)
            identities[role] = employee.id, token
    try:
        yield identities
    finally:
        ids = [value[0] for value in identities.values()]
        with Session(engine) as db, db.begin():
            db.execute(sa.delete(Credential).where(Credential.employee_id.in_(ids)))
            db.execute(sa.delete(Employee).where(Employee.id.in_(ids)))
        engine.dispose()


def test_auditor_can_page_metadata_and_other_roles_cannot(
    accounts: dict[Role, tuple[UUID, str]],
) -> None:
    employee, _ = accounts[Role.EMPLOYEE]
    first_target, last_target = uuid4(), uuid4()
    with get_admin_session_factory().begin() as db:
        for target in (first_target, last_target):
            append_event(
                db, Action.INVITATION_ISSUED, actor_id=employee, target=(Target.EMPLOYEE, target)
            )
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/audit/events").status_code == 401
        for role in (Role.EMPLOYEE, Role.HR, Role.ADMIN):
            client.cookies.set(COOKIE, accounts[role][1])
            assert client.get("/api/audit/events").status_code == 403
        client.cookies.set(COOKIE, accounts[Role.SECURITY_AUDITOR][1])
        params: dict[str, str | int] = {"action": "invitation_issued", "limit": 1}
        response = client.get("/api/audit/events", params=params)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        page = response.json()
        assert page["events"][0]["target_id"] == str(last_target)
        assert page["next_before_id"] == page["events"][0]["id"]
        params["before_id"] = page["next_before_id"]
        second = client.get("/api/audit/events", params=params).json()["events"][0]
        assert second["target_id"] == str(first_target)
        assert set(second) == {
            "id",
            "occurred_at",
            "action",
            "outcome",
            "actor_id",
            "target_kind",
            "target_id",
            "redaction_kind",
            "redaction_count",
        }


def test_login_logs_success_and_denial_without_credentials(
    accounts: dict[Role, tuple[UUID, str]],
) -> None:
    employee, _ = accounts[Role.EMPLOYEE]
    wrong_password = "Не_сохранять_эту_строку!123"
    with TestClient(app, base_url="https://testserver") as client:
        denied = client.post(
            "/api/auth/login",
            json={
                "login": str(employee),
                "password": wrong_password,
            },
        )
        assert denied.status_code == 401
        accepted = client.post(
            "/api/auth/login",
            json={
                "login": str(employee),
                "password": PASSWORD,
            },
        )
        assert accepted.status_code == 200
        client.cookies.clear()
        client.cookies.set(COOKIE, accounts[Role.SECURITY_AUDITOR][1])
        response = client.get("/api/audit/events", params={"action": "login", "limit": 2})
        assert response.status_code == 200
        success, failure = response.json()["events"]
        assert success["outcome"] == "success" and success["actor_id"] == str(employee)
        assert failure["outcome"] == "denied" and failure["actor_id"] is None
        assert wrong_password not in response.text
        assert PASSWORD not in response.text


def test_app_writes_but_cannot_read_without_auditor_context() -> None:
    with get_session_factory().begin() as db:
        append_event(db, Action.MATERIAL_UPLOADED, target=(Target.MATERIAL, uuid4()))
        assert db.scalar(sa.select(AuditEvent.id).limit(1)) is None
    with pytest.raises(DBAPIError) as error, get_admin_session_factory().begin() as db:
        db.execute(sa.select(AuditEvent.id).limit(1))
    assert error.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as error, get_session_factory().begin() as db:
        db.execute(
            sa.text("""
            INSERT INTO audit.events (action, outcome, occurred_at)
            VALUES ('login', 'denied', '2000-01-01')
        """)
        )
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit.events SET outcome = 'success'",
        "DELETE FROM audit.events",
        "TRUNCATE audit.events",
    ],
)
def test_even_owner_dml_cannot_mutate_log(sql: str) -> None:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    try:
        with pytest.raises(DBAPIError) as error, engine.begin() as db:
            db.execute(sa.text(sql))
        assert error.value.orig.sqlstate == "55000"
    finally:
        engine.dispose()
