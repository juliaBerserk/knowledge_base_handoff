import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from estafeta.api import auth as auth_api
from estafeta.auth import security, service
from estafeta.auth.bootstrap import bootstrap
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.core.db import get_session_factory
from estafeta.main import app
from estafeta.models import Employee, Role
from estafeta.models.auth import AuthSession, Credential, Invitation

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")
PASSWORD = "Субботнее письмо с пятью апельсинами!"
NEW_PASSWORD = "На улице мяты выросло семь кустов!"


@pytest.fixture
def seed_employee() -> Iterator[Callable[[Role], UUID]]:
    engine = sa.create_engine(MigrationSettings().database_owner_url)
    ids: list[UUID] = []

    def create(role: Role) -> UUID:
        with Session(engine) as db, db.begin():
            employee = Employee(full_name="Тестовая учётная запись", role=role)
            db.add(employee)
            db.flush()
            ids.append(employee.id)
            return employee.id

    yield create
    with Session(engine) as db, db.begin():
        db.execute(sa.delete(Credential).where(Credential.employee_id.in_(ids)))
        db.execute(sa.delete(Employee).where(Employee.id.in_(ids)))
    engine.dispose()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    ip = str(uuid4())
    monkeypatch.setattr(auth_api, "client_key", lambda request, settings: ip)
    with TestClient(app, base_url="https://testserver") as api_client:
        yield api_client


def prepare_invitation(employee: UUID, now: datetime | None = None) -> str:
    with get_session_factory().begin() as db:
        token = service.issue_invitation(db, employee, now or datetime.now(UTC))
        assert token
        return token


def activate(client: TestClient, employee: UUID) -> None:
    response = client.post(
        "/api/auth/redeem", json={"token": prepare_invitation(employee), "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert response.json()["login_id"] == str(employee)


def log_in(client: TestClient, employee: UUID, **values: str) -> Response:
    return client.post(
        "/api/auth/login", json={"login": str(employee), "password": PASSWORD} | values
    )


def test_invitation_expires_and_reissue_invalidates_previous(
    seed_employee: Callable[[Role], UUID], client: TestClient
) -> None:
    employee = seed_employee(Role.EMPLOYEE)
    old = prepare_invitation(employee, datetime.now(UTC) - timedelta(hours=72))
    assert (
        client.post("/api/auth/redeem", json={"token": old, "password": PASSWORD}).status_code
        == 400
    )
    previous = prepare_invitation(employee)
    current = prepare_invitation(employee)
    with get_session_factory().begin() as db:
        invitation = db.get(Invitation, security.token_hash(current))
        assert invitation is not None
        assert invitation.employee_id == employee
    for token, status in [(previous, 400), (current, 200), (current, 400)]:
        response = client.post("/api/auth/redeem", json={"token": token, "password": PASSWORD})
        assert response.status_code == status


def test_concurrent_redemption_only_succeeds_once(seed_employee: Callable[[Role], UUID]) -> None:
    employee = seed_employee(Role.EMPLOYEE)
    token = prepare_invitation(employee)

    def redeem(_: int) -> UUID | None:
        with get_session_factory().begin() as db:
            return service.redeem_invitation(db, token, PASSWORD, datetime.now(UTC))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, range(2)))
    assert results.count(employee) == 1
    assert results.count(None) == 1


def test_recovery_and_permanent_block_revoke_existing_sessions(
    seed_employee: Callable[[Role], UUID], client: TestClient
) -> None:
    employee = seed_employee(Role.EMPLOYEE)
    activate(client, employee)
    assert log_in(client, employee).status_code == 200
    assert client.get("/api/auth/session").status_code == 200
    response = client.post(
        "/api/auth/redeem", json={"token": prepare_invitation(employee), "password": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    assert log_in(client, employee).status_code == 401
    assert log_in(client, employee, password=NEW_PASSWORD).status_code == 200
    with get_session_factory().begin() as db:
        service.block_account(db, employee, datetime.now(UTC))
        assert service.issue_invitation(db, employee, datetime.now(UTC)) is None
    assert client.get("/api/auth/session").status_code == 401
    assert log_in(client, employee, password=NEW_PASSWORD).status_code == 401


def test_five_failures_persist_in_database(
    seed_employee: Callable[[Role], UUID], client: TestClient
) -> None:
    employee = seed_employee(Role.EMPLOYEE)
    activate(client, employee)
    for _ in range(5):
        assert log_in(client, employee, password="wrong").status_code == 401
    assert log_in(client, employee).status_code == 401
    settings = get_settings()
    with get_session_factory().begin() as db:
        credential = db.get(Credential, employee)
        assert credential and credential.locked_until
        result = service.login(db, settings, str(employee), PASSWORD, None, credential.locked_until)
        assert result.token


@pytest.mark.parametrize("role", [Role.ADMIN, Role.DEPARTMENT_HEAD, Role.EMPLOYEE])
def test_totp_enrollment_replaces_session_and_admin_actions_require_full_session(
    seed_employee: Callable[[Role], UUID], client: TestClient, role: Role
) -> None:
    employee = seed_employee(role)
    target = seed_employee(Role.EMPLOYEE)
    activate(client, employee)
    response = log_in(client, employee)
    assert response.status_code == 200
    assert response.json()["enrollment_only"] == (role is not Role.EMPLOYEE)
    initial_token = client.cookies.get(auth_api.COOKIE)
    initial_headers = {"X-CSRF-Token": response.json()["csrf_token"]}
    invite_path = f"/api/auth/invitations/{target}"
    assert client.post(invite_path, json={}, headers=initial_headers).status_code == 403
    response = client.post("/api/auth/totp/start", json={}, headers=initial_headers)
    assert response.status_code == 200

    current = datetime.now(UTC)
    with get_session_factory().begin() as db:
        assert initial_token
        pending = db.get(AuthSession, security.token_hash(initial_token))
        assert pending and pending.pending_totp
        secret = security.totp_cipher(get_settings().app_secret_key).decrypt(pending.pending_totp)
    step = int(current.timestamp()) // 30
    code = security.totp_code(secret, step)
    response = client.post("/api/auth/totp/confirm", json={"code": code}, headers=initial_headers)
    assert response.status_code == 200
    assert response.json()["enrollment_only"] is False
    assert client.cookies.get(auth_api.COOKIE) != initial_token
    headers = {"X-CSRF-Token": response.json()["csrf_token"]}
    expected = 201 if role is Role.ADMIN else 403
    assert client.post(invite_path, json={}, headers=headers).status_code == expected
    assert client.post("/api/auth/logout", json={}, headers=headers).status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    assert log_in(client, employee).json()["totp_required"] is True
    assert log_in(client, employee, code=code).status_code == 401
    assert log_in(client, employee, code=security.totp_code(secret, step + 1)).status_code == 200


def test_ip_rate_limit_advances_to_next_window() -> None:
    ip = str(uuid4())
    now = datetime.fromtimestamp(1_800_000_001, UTC)
    with get_session_factory().begin() as db:
        assert service.consume_limit(db, "test", ip, now, 2)
        assert service.consume_limit(db, "test", ip, now, 2)
        assert not service.consume_limit(db, "test", ip, now, 2)
        assert not service.consume_limit(db, "test", ip, now - timedelta(minutes=1), 2)
        assert service.consume_limit(db, "test", ip, now + timedelta(minutes=1), 2)


def test_auth_database_role_only_reads_employee_auth_columns() -> None:
    with get_session_factory().begin() as db:
        assert db.scalar(sa.text("SELECT current_user")) == "estafeta_app"
        assert db.scalar(
            sa.text("SELECT has_column_privilege('knowledge.employees', 'role', 'SELECT')")
        )
        assert not db.scalar(
            sa.text("SELECT has_column_privilege('knowledge.employees', 'full_name', 'SELECT')")
        )
        assert not db.scalar(sa.text("SELECT has_table_privilege('knowledge.materials', 'SELECT')"))


def test_bootstrap_only_reissues_until_admin_sets_password() -> None:
    owner = sa.create_engine(MigrationSettings().database_owner_url)
    now = datetime.now(UTC)
    try:
        with Session(owner) as db, db.begin():
            first = bootstrap(db, "initial-admin@example.test", now)
            second = bootstrap(db, "initial-admin@example.test", now)
            assert first and second and first != second
            assert service.redeem_invitation(db, first, PASSWORD, now) is None
            assert service.redeem_invitation(db, second, PASSWORD, now) is not None
            assert bootstrap(db, "initial-admin@example.test", now) is None
            db.rollback()
    finally:
        owner.dispose()
