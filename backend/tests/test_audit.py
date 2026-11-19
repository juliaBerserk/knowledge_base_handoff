from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from estafeta.api.dependencies import Principal, get_db_session, get_principal
from estafeta.domain.access import Role
from estafeta.main import app
from estafeta.models import AuditEvent, AuthSession
from estafeta.schemas.audit import AuditEventType, AuditMetadata
from estafeta.services.audit import record_audit_event


class _FakeSession:
    def __init__(self, rows: list[AuditEvent]) -> None:
        self.rows = rows
        self.added: list[AuditEvent] = []
        self.statement = None

    def add(self, event: AuditEvent) -> None:
        self.added.append(event)

    def scalars(self, statement):
        self.statement = statement
        return iter(self.rows)


def _principal(role: Role) -> Principal:
    now = datetime.now(UTC)
    user = SimpleNamespace(id=uuid4(), role=role, is_active=True)
    auth_session = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=b"session",
        csrf_hash=b"csrf",
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=1),
        totp_verified_at=now,
    )
    return Principal(user, auth_session, totp_enabled=False)


def test_record_audit_event_accepts_only_event_specific_metadata() -> None:
    session = _FakeSession([])
    actor_id = uuid4()
    subject_id = uuid4()

    event = record_audit_event(
        session,
        AuditEventType.USER_CREATED,
        actor_user_id=actor_id,
        actor_role=Role.ADMIN.value,
        subject_type="user",
        subject_id=subject_id,
        metadata=AuditMetadata(target_role=Role.EMPLOYEE),
    )

    assert session.added == [event]
    assert event.actor_user_id == actor_id
    assert event.subject_id == subject_id
    assert event.metadata_json == {"target_role": Role.EMPLOYEE.value}

    redaction = record_audit_event(
        session,
        AuditEventType.RECORD_REDACTION_RECORDED,
        actor_user_id=actor_id,
        actor_role=Role.EMPLOYEE.value,
        subject_type="record",
        subject_id=subject_id,
        metadata=AuditMetadata(finding_type="password", redacted_count=2),
    )
    assert redaction.metadata_json == {"finding_type": "password", "redacted_count": 2}

    with pytest.raises(ValueError, match="Недопустимые метаданные"):
        record_audit_event(
            session,
            AuditEventType.LOGIN_SUCCEEDED,
            actor_user_id=actor_id,
            actor_role=Role.EMPLOYEE.value,
            metadata=AuditMetadata(imported_count=1),
        )
    with pytest.raises(ValueError, match="Недопустимый тип объекта"):
        record_audit_event(
            session,
            AuditEventType.LOGIN_SUCCEEDED,
            actor_user_id=actor_id,
            actor_role=Role.EMPLOYEE.value,
            subject_type="user-supplied-secret",
        )


def test_audit_api_requires_security_auditor() -> None:
    app.dependency_overrides[get_principal] = lambda: _principal(Role.EMPLOYEE)
    try:
        response = TestClient(app).get("/api/audit/events")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert response.status_code == 403


def test_audit_api_filters_and_returns_cursor_for_auditor() -> None:
    now = datetime.now(UTC)
    rows = [
        AuditEvent(
            id=30,
            occurred_at=now,
            event_type=AuditEventType.USER_CREATED.value,
            actor_user_id=uuid4(),
            actor_role=Role.ADMIN.value,
            subject_type="user",
            subject_id=uuid4(),
            metadata_json={"target_role": Role.EMPLOYEE.value},
        ),
        AuditEvent(
            id=29,
            occurred_at=now - timedelta(seconds=1),
            event_type=AuditEventType.USER_CREATED.value,
            actor_user_id=None,
            actor_role=None,
            subject_type=None,
            subject_id=None,
            metadata_json={},
        ),
    ]
    session = _FakeSession(rows)
    app.dependency_overrides[get_principal] = lambda: _principal(Role.SECURITY_AUDITOR)
    app.dependency_overrides[get_db_session] = lambda: session
    try:
        response = TestClient(app).get(
            "/api/audit/events",
            params={
                "event_type": AuditEventType.USER_CREATED.value,
                "before_id": 45,
                "limit": 1,
                "since": "2026-10-30T00:00:00+03:00",
            },
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(get_db_session, None)

    assert response.status_code == 200
    assert response.json()["next_before_id"] == 30
    assert [item["id"] for item in response.json()["items"]] == [30]
    assert response.json()["items"][0]["metadata"]["target_role"] == Role.EMPLOYEE.value
    assert "audit_events.event_type =" in str(session.statement)
    assert "audit_events.id <" in str(session.statement)
    assert "audit_events.occurred_at >=" in str(session.statement)


def test_audit_api_rejects_unbounded_or_invalid_time_filters() -> None:
    app.dependency_overrides[get_principal] = lambda: _principal(Role.SECURITY_AUDITOR)
    try:
        client = TestClient(app)
        oversized = client.get("/api/audit/events?limit=101")
        naive_date = client.get("/api/audit/events?since=2026-10-30T12:00:00")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert oversized.status_code == 422
    assert naive_date.status_code == 422
