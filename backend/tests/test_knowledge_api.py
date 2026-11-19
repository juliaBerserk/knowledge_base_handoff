from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from estafeta.api.dependencies import Principal, get_db_session, require_knowledge_user
from estafeta.core.security import token_digest
from estafeta.domain.access import Role
from estafeta.main import app
from estafeta.models import (
    AuditEvent,
    AuthSession,
    Base,
    Department,
    EmploymentAssignment,
    Position,
    User,
)


def _principal(user: User) -> Principal:
    now = datetime.now(UTC)
    auth_session = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=token_digest("knowledge-session"),
        csrf_hash=token_digest("knowledge-csrf"),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=12),
        totp_verified_at=now,
    )
    return Principal(user, auth_session, totp_enabled=False)


def test_record_and_material_publication_department_access_and_copy(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("estafeta.services.materials.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.knowledge.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'knowledge-api.sqlite'}")
    Base.metadata.create_all(engine)

    department_id = uuid4()
    position_id = uuid4()
    other_position_id = uuid4()
    other_department_id = uuid4()
    outsider_position_id = uuid4()
    users = {
        "author": User(
            id=uuid4(),
            full_name="Author User",
            email="author@example.test",
            role=Role.EMPLOYEE,
            is_active=True,
        ),
        "reader": User(
            id=uuid4(),
            full_name="Reader User",
            email="reader@example.test",
            role=Role.EMPLOYEE,
            is_active=True,
        ),
        "manager": User(
            id=uuid4(),
            full_name="Manager User",
            email="manager@example.test",
            role=Role.DEPARTMENT_MANAGER,
            is_active=True,
        ),
        "outsider": User(
            id=uuid4(),
            full_name="Outsider User",
            email="outsider@example.test",
            role=Role.EMPLOYEE,
            is_active=True,
        ),
    }
    departments = [
        Department(
            id=department_id,
            name="Engineering",
            publication_review_required=True,
        ),
        Department(
            id=other_department_id,
            name="Finance",
            publication_review_required=True,
        ),
    ]
    positions = [
        Position(id=position_id, department_id=department_id, title="Engineer"),
        Position(id=other_position_id, department_id=department_id, title="Designer"),
        Position(id=outsider_position_id, department_id=other_department_id, title="Analyst"),
    ]
    now = datetime.now(UTC)
    assignments = [
        EmploymentAssignment(user_id=users["author"].id, position_id=position_id, starts_at=now),
        EmploymentAssignment(
            user_id=users["reader"].id,
            position_id=other_position_id,
            starts_at=now,
        ),
        EmploymentAssignment(user_id=users["manager"].id, position_id=position_id, starts_at=now),
        EmploymentAssignment(
            user_id=users["outsider"].id,
            position_id=outsider_position_id,
            starts_at=now,
        ),
    ]
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([*users.values(), *departments, *positions, *assignments])
        session.commit()

    active_user = {"principal": _principal(users["author"])}

    def override_principal() -> Principal:
        return active_user["principal"]

    def override_session():
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[require_knowledge_user] = override_principal
    app.dependency_overrides[get_db_session] = override_session
    client = TestClient(app)
    cookies = {"estafeta_csrf": "knowledge-csrf"}
    headers = {"X-CSRF-Token": "knowledge-csrf"}
    try:
        created = client.post(
            "/api/records",
            json={
                "category": "processes",
                "title": "Deployment",
                "content": "password: super-secret-value. Deploy via approved pipeline.",
            },
            cookies=cookies,
            headers=headers,
        )
        assert created.status_code == 201
        original = created.json()
        assert original["status"] == "draft"
        assert original["scope"] == "personal"
        assert "super-secret-value" not in original["content"]

        updated = client.put(
            f"/api/records/{original['id']}",
            json={
                "category": "processes",
                "title": "Deployment procedure",
                "content": "password: another-secret. Deploy through release checks.",
            },
            cookies=cookies,
            headers=headers,
        )
        assert updated.status_code == 200
        assert "another-secret" not in updated.json()["content"]

        active_user["principal"] = _principal(users["reader"])
        assert client.get(f"/api/records/{original['id']}").status_code == 404
        assert client.get("/api/records?space=department").json()["items"] == []

        active_user["principal"] = _principal(users["author"])
        material = client.post(
            "/api/materials/note",
            json={
                "title": "Release notes",
                "text": "Release process uses the approved deployment pipeline.",
            },
            cookies=cookies,
            headers=headers,
        )
        assert material.status_code == 201
        material_id = material.json()["id"]
        submitted_material = client.post(
            f"/api/materials/{material_id}/submit",
            cookies=cookies,
            headers=headers,
        )
        assert submitted_material.status_code == 200
        assert submitted_material.json()["status"] == "in_review"

        submitted_record = client.post(
            f"/api/records/{original['id']}/submit",
            cookies=cookies,
            headers=headers,
        )
        assert submitted_record.status_code == 200
        assert submitted_record.json()["status"] == "in_review"

        active_user["principal"] = _principal(users["manager"])
        review_queue = client.get("/api/records?space=review")
        assert [item["id"] for item in review_queue.json()["items"]] == [original["id"]]
        assert client.get("/api/materials?space=review").json()["items"][0]["id"] == material_id
        published_record = client.post(
            f"/api/records/{original['id']}/publish",
            cookies=cookies,
            headers=headers,
        )
        assert published_record.status_code == 200
        assert published_record.json()["status"] == "published"
        published_material = client.post(
            f"/api/materials/{material_id}/publish",
            cookies=cookies,
            headers=headers,
        )
        assert published_material.status_code == 200
        assert published_material.json()["status"] == "published"

        active_user["principal"] = _principal(users["reader"])
        department_records = client.get("/api/records?space=department").json()["items"]
        assert [item["id"] for item in department_records] == [original["id"]]
        colleague_records = client.get(
            "/api/records",
            params={"space": "department", "author_id": str(users["author"].id)},
        ).json()["items"]
        assert [item["id"] for item in colleague_records] == [original["id"]]
        copied = client.post(
            f"/api/records/{original['id']}/copy",
            cookies=cookies,
            headers=headers,
        )
        assert copied.status_code == 201
        copied_record = copied.json()
        assert copied_record["scope"] == "personal"
        assert copied_record["status"] == "draft"
        assert copied_record["copied_from_id"] == original["id"]
        assert copied_record["original_author_id"] == str(users["author"].id)

        department_materials = client.get("/api/materials?space=department").json()["items"]
        assert [item["id"] for item in department_materials] == [material_id]
        colleague_materials = client.get(
            "/api/materials",
            params={"space": "department", "author_id": str(users["author"].id)},
        ).json()["items"]
        assert [item["id"] for item in colleague_materials] == [material_id]
        assert client.get(f"/api/materials/{material_id}").status_code == 200

        active_user["principal"] = _principal(users["outsider"])
        assert client.get("/api/records?space=department").json()["items"] == []
        assert client.get(f"/api/records/{original['id']}").status_code == 404

        active_user["principal"] = _principal(users["manager"])
        archived = client.post(
            f"/api/records/{original['id']}/archive",
            cookies=cookies,
            headers=headers,
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"

        active_user["principal"] = _principal(users["reader"])
        assert client.get("/api/records?space=department").json()["items"] == []
        assert [
            item["id"] for item in client.get("/api/records?space=mine").json()["items"]
        ] == [copied_record["id"]]
        assert client.get(f"/api/records/{copied_record['id']}").status_code == 200

        with Session(engine) as session:
            department = session.get(Department, department_id)
            assert department is not None
            department.publication_review_required = False
            session.commit()
        active_user["principal"] = _principal(users["author"])
        direct_record = client.post(
            "/api/records",
            json={
                "category": "faqs",
                "title": "Release question",
                "content": "Run the release validation before deployment.",
            },
            cookies=cookies,
            headers=headers,
        )
        assert direct_record.status_code == 201
        direct_publication = client.post(
            f"/api/records/{direct_record.json()['id']}/submit",
            cookies=cookies,
            headers=headers,
        )
        assert direct_publication.status_code == 200
        assert direct_publication.json()["status"] == "published"
        active_user["principal"] = _principal(users["reader"])
        visible_ids = {
            item["id"] for item in client.get("/api/records?space=department").json()["items"]
        }
        assert visible_ids == {direct_record.json()["id"]}

        with Session(engine) as session:
            audit_rows = list(session.query(AuditEvent))
        assert any(event.event_type == "record.redaction_recorded" for event in audit_rows)
        assert any(event.event_type == "knowledge.space_viewed" for event in audit_rows)
        assert all(
            "super-secret-value" not in str(event.metadata_json)
            and "another-secret" not in str(event.metadata_json)
            for event in audit_rows
        )
    finally:
        app.dependency_overrides.pop(require_knowledge_user, None)
        app.dependency_overrides.pop(get_db_session, None)
        engine.dispose()
