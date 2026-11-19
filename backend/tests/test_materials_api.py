from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from estafeta.api.dependencies import (
    Principal,
    get_db_session,
    require_admin,
    require_knowledge_user,
)
from estafeta.core.security import token_digest
from estafeta.domain.access import Role
from estafeta.main import app
from estafeta.models import (
    AuditEvent,
    Base,
    Material,
    MaterialVersion,
    RedactionEvent,
    SanitizationPolicy,
    User,
)


def _principal(role: Role) -> Principal:
    now = datetime.now(UTC)
    user = User(
        id=uuid4(),
        full_name="Материал тест",
        email="material@example.com",
        role=role,
        is_active=True,
    )
    from estafeta.models import AuthSession

    auth_session = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=token_digest("session-token"),
        csrf_hash=token_digest("csrf-token"),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=12),
        totp_verified_at=now,
    )
    return Principal(user, auth_session, totp_enabled=False)


def _session_override(engine):
    def override():
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return override


def test_note_preview_and_save_persist_only_sanitized_text(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("estafeta.services.materials.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.knowledge.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.sanitization._natasha_candidates", lambda *_args: [])
    user_principal = _principal(Role.EMPLOYEE)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'materials-api.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(user_principal.user)
        session.commit()

    app.dependency_overrides[get_db_session] = _session_override(engine)
    app.dependency_overrides[require_knowledge_user] = lambda: user_principal
    try:
        client = TestClient(app)
        csrf = {"estafeta_csrf": "csrf-token"}
        headers = {"X-CSRF-Token": "csrf-token"}
        source_text = "Deployment notes. password=top-secret-value Contact vendor@example.net"
        preview = client.post(
            "/api/materials/preview-note",
            json={"title": "Deploy guide", "text": source_text},
            cookies=csrf,
            headers=headers,
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert "top-secret-value" not in preview_body["cleaned_text"]
        assert "vendor@example.net" not in preview_body["cleaned_text"]
        assert preview_body["warning"]
        contact_id = next(
            finding["contact_id"]
            for finding in preview_body["findings"]
            if finding["contact_id"] is not None
        )

        saved = client.post(
            "/api/materials/note",
            json={
                "title": "Deploy guide",
                "text": source_text,
                "approved_external_contact_ids": [contact_id],
            },
            cookies=csrf,
            headers=headers,
        )
        assert saved.status_code == 201
        saved_body = saved.json()
        assert "top-secret-value" not in saved_body["cleaned_text"]
        assert "vendor@example.net" in saved_body["cleaned_text"]
        assert saved_body["redaction_counts"]["password"] == 1

        with Session(engine) as session:
            stored = session.scalar(select(MaterialVersion))
            assert stored is not None
            assert "top-secret-value" not in stored.cleaned_text
            assert "vendor@example.net" in stored.cleaned_text
            assert session.scalar(
                select(Material).where(Material.id == UUID(saved_body["id"]))
            )
            assert session.scalar(select(RedactionEvent)).redacted_count == 1
            audit_rows = list(session.scalars(select(AuditEvent)))
            assert {event.event_type for event in audit_rows} == {
                "material.created",
                "material.redaction_recorded",
            }
            assert all("top-secret-value" not in str(event.metadata_json) for event in audit_rows)

        read_back = client.get(
            f"/api/materials/{saved_body['id']}",
            cookies=csrf,
        )
        assert read_back.status_code == 200
        assert read_back.json()["cleaned_text"] == saved_body["cleaned_text"]
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(require_knowledge_user, None)
        engine.dispose()


def test_policy_configuration_is_restricted_to_admin_and_audited(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("estafeta.services.materials.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.knowledge.set_local_user_id", lambda *_args: None)
    admin = _principal(Role.ADMIN)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'policy-api.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(admin.user)
        session.commit()
    app.dependency_overrides[get_db_session] = _session_override(engine)
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        client = TestClient(app)
        response = client.put(
            "/api/admin/sanitization-policy",
            json={
                "custom_patterns": [{"name": "ticket", "pattern": r"\bINC-\d{5}\b"}],
                "custom_terms": ["internal codename"],
                "corporate_domains": ["company.ru"],
            },
            cookies={"estafeta_csrf": "csrf-token"},
            headers={"X-CSRF-Token": "csrf-token"},
        )

        assert response.status_code == 200
        assert response.json()["corporate_domains"] == ["company.ru"]
        with Session(engine) as session:
            policy = session.get(SanitizationPolicy, 1)
            assert policy is not None
            assert policy.custom_terms == ["internal codename"]
            audit = session.scalar(select(AuditEvent))
            assert audit is not None
            assert audit.event_type == "admin.sanitization_policy_updated"
            assert audit.metadata_json == {}
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(require_admin, None)
        engine.dispose()
