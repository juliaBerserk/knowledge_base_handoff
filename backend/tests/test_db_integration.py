import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from estafeta.core.config import clear_settings_cache
from estafeta.core.db import ping, reset_engine, set_local_user_id
from estafeta.main import app

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="нужен PostgreSQL с pgvector (RUN_DB_TESTS=1)",
)


def test_ping_postgres_and_vector() -> None:
    reset_engine()
    clear_settings_cache()
    from estafeta.core.db import get_engine

    database_ok, vector_ok = ping(get_engine())
    assert database_ok is True
    assert vector_ok is True


def test_health_against_postgres() -> None:
    reset_engine()
    clear_settings_cache()
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["vector_extension"] is True


def test_rls_limits_directory_to_role_and_department() -> None:
    from estafeta.core.config import get_settings

    reset_engine()
    clear_settings_cache()
    settings = get_settings()
    owner_engine = create_engine(settings.database_owner_url)
    app_engine = create_engine(settings.database_url)
    department_id = uuid4()
    other_department_id = uuid4()
    position_id = uuid4()
    other_position_id = uuid4()
    manager_id = uuid4()
    colleague_id = uuid4()
    outside_user_id = uuid4()
    hr_id = uuid4()
    admin_id = uuid4()
    inactive_user_id = uuid4()
    created_at = datetime.now(UTC)

    try:
        with owner_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO departments (id, name) VALUES (:id, :name)"),
                [
                    {"id": department_id, "name": f"test-{department_id}"},
                    {"id": other_department_id, "name": f"test-{other_department_id}"},
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO positions (id, department_id, title) "
                    "VALUES (:id, :department_id, :title)"
                ),
                [
                    {
                        "id": position_id,
                        "department_id": department_id,
                        "title": f"manager-{position_id}",
                    },
                    {
                        "id": other_position_id,
                        "department_id": other_department_id,
                        "title": f"other-{other_position_id}",
                    },
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, full_name, email, role) "
                    "VALUES (:id, :full_name, :email, :role)"
                ),
                [
                    {
                        "id": manager_id,
                        "full_name": "Test manager",
                        "email": f"manager-{manager_id}@example.test",
                        "role": "department_manager",
                    },
                    {
                        "id": colleague_id,
                        "full_name": "Test colleague",
                        "email": f"colleague-{colleague_id}@example.test",
                        "role": "employee",
                    },
                    {
                        "id": outside_user_id,
                        "full_name": "Test outside",
                        "email": f"outside-{outside_user_id}@example.test",
                        "role": "employee",
                    },
                    {
                        "id": hr_id,
                        "full_name": "Test HR",
                        "email": f"hr-{hr_id}@example.test",
                        "role": "hr",
                    },
                    {
                        "id": admin_id,
                        "full_name": "Test admin",
                        "email": f"admin-{admin_id}@example.test",
                        "role": "admin",
                    },
                    {
                        "id": inactive_user_id,
                        "full_name": "Inactive test user",
                        "email": f"inactive-{inactive_user_id}@example.test",
                        "role": "employee",
                    },
                ],
            )
            connection.execute(
                text("UPDATE users SET is_active = false WHERE id = :id"),
                {"id": inactive_user_id},
            )
            connection.execute(
                text(
                    "INSERT INTO credentials "
                    "(user_id, password_hash, failed_attempts, locked_at) "
                    "VALUES (:user_id, :password_hash, :failed_attempts, :locked_at)"
                ),
                [
                    {
                        "user_id": manager_id,
                        "password_hash": "not-a-real-password-hash",
                        "failed_attempts": 0,
                        "locked_at": None,
                    },
                    {
                        "user_id": colleague_id,
                        "password_hash": "not-a-real-password-hash",
                        "failed_attempts": 5,
                        "locked_at": created_at,
                    },
                    {
                        "user_id": outside_user_id,
                        "password_hash": "not-a-real-password-hash",
                        "failed_attempts": 0,
                        "locked_at": None,
                    },
                    {
                        "user_id": hr_id,
                        "password_hash": "not-a-real-password-hash",
                        "failed_attempts": 0,
                        "locked_at": None,
                    },
                    {
                        "user_id": admin_id,
                        "password_hash": "not-a-real-password-hash",
                        "failed_attempts": 0,
                        "locked_at": None,
                    },
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO employment_assignments "
                    "(id, user_id, position_id, starts_at) "
                    "VALUES (:id, :user_id, :position_id, :starts_at)"
                ),
                [
                    {
                        "id": uuid4(),
                        "user_id": manager_id,
                        "position_id": position_id,
                        "starts_at": created_at,
                    },
                    {
                        "id": uuid4(),
                        "user_id": colleague_id,
                        "position_id": position_id,
                        "starts_at": created_at,
                    },
                    {
                        "id": uuid4(),
                        "user_id": outside_user_id,
                        "position_id": other_position_id,
                        "starts_at": created_at,
                    },
                ],
            )

        assert _visible_user_ids(app_engine, manager_id) == {manager_id, colleague_id}
        assert _visible_user_ids(app_engine, colleague_id) == {colleague_id}
        assert _visible_user_ids(app_engine, hr_id) == {
            manager_id,
            colleague_id,
            outside_user_id,
            hr_id,
            inactive_user_id,
        }
        assert _visible_user_ids(app_engine, inactive_user_id) == set()
        assert _visible_credential_user_ids(app_engine, admin_id) == {admin_id}
        _admin_unlock(app_engine, admin_id, colleague_id)
        with owner_engine.connect() as connection:
            row = connection.execute(
                text("SELECT failed_attempts, locked_at FROM credentials WHERE user_id = :id"),
                {"id": colleague_id},
            ).one()
            assert row.failed_attempts == 0
            assert row.locked_at is None
        with Session(app_engine) as session:
            with pytest.raises(DBAPIError):
                with session.begin():
                    set_local_user_id(session, manager_id)
                    session.execute(
                        text("SELECT app_admin_unlock(:user_id)"),
                        {"user_id": str(colleague_id)},
                    )
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM credentials WHERE user_id = ANY(:ids)"),
                {"ids": [manager_id, colleague_id, outside_user_id, hr_id, admin_id]},
            )
            connection.execute(
                text("DELETE FROM employment_assignments WHERE user_id = ANY(:ids)"),
                {"ids": [manager_id, colleague_id, outside_user_id]},
            )
            connection.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {
                    "ids": [
                        manager_id,
                        colleague_id,
                        outside_user_id,
                        hr_id,
                        admin_id,
                        inactive_user_id,
                    ]
                },
            )
            connection.execute(
                text("DELETE FROM positions WHERE id = ANY(:ids)"),
                {"ids": [position_id, other_position_id]},
            )
            connection.execute(
                text("DELETE FROM departments WHERE id = ANY(:ids)"),
                {"ids": [department_id, other_department_id]},
            )
        app_engine.dispose()
        owner_engine.dispose()


def _visible_user_ids(engine: Engine, user_id: UUID) -> set[UUID]:
    with Session(engine) as session, session.begin():
        set_local_user_id(session, user_id)
        role = session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        assert role.rolsuper is False
        assert role.rolbypassrls is False
        result = session.execute(text("SELECT id FROM users"))
        return {row.id for row in result}


def _visible_credential_user_ids(engine: Engine, user_id: UUID) -> set[UUID]:
    with Session(engine) as session, session.begin():
        set_local_user_id(session, user_id)
        result = session.execute(text("SELECT user_id FROM credentials"))
        return {row.user_id for row in result}


def _admin_unlock(engine: Engine, admin_id: UUID, target_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        set_local_user_id(session, admin_id)
        result = session.execute(
            text("SELECT app_admin_unlock(:user_id)"),
            {"user_id": str(target_id)},
        )
        assert result.scalar_one() is True


def test_audit_rls_and_append_only_guards() -> None:
    from estafeta.core.config import get_settings

    reset_engine()
    clear_settings_cache()
    settings = get_settings()
    owner_engine = create_engine(settings.database_owner_url)
    app_engine = create_engine(settings.database_url)
    employee_id = uuid4()
    admin_id = uuid4()
    auditor_id = uuid4()
    event_id = None

    try:
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, full_name, email, role) "
                    "VALUES (:id, :name, :email, :role)"
                ),
                [
                    {
                        "id": employee_id,
                        "name": "Audit test employee",
                        "email": f"audit-employee-{employee_id}@example.test",
                        "role": "employee",
                    },
                    {
                        "id": admin_id,
                        "name": "Audit test administrator",
                        "email": f"audit-admin-{admin_id}@example.test",
                        "role": "admin",
                    },
                    {
                        "id": auditor_id,
                        "name": "Audit test auditor",
                        "email": f"audit-reader-{auditor_id}@example.test",
                        "role": "security_auditor",
                    },
                ],
            )

        with Session(app_engine) as session, session.begin():
            set_local_user_id(session, employee_id)
            event_id = session.execute(
                text(
                    "INSERT INTO audit_events (event_type, actor_user_id, actor_role) "
                    "VALUES ('auth.logout', :actor_id, 'employee') RETURNING id"
                ),
                {"actor_id": employee_id},
            ).scalar_one()

        assert _visible_audit_ids(app_engine, auditor_id) == {event_id}
        assert _visible_audit_ids(app_engine, employee_id) == set()
        assert _visible_audit_ids(app_engine, admin_id) == set()

        with Session(app_engine) as session:
            with pytest.raises(DBAPIError):
                with session.begin():
                    set_local_user_id(session, employee_id)
                    session.execute(
                        text(
                            "INSERT INTO audit_events "
                            "(event_type, actor_user_id, actor_role) "
                            "VALUES ('auth.logout', :actor_id, 'admin')"
                        ),
                        {"actor_id": admin_id},
                    )

        with Session(app_engine) as session:
            with pytest.raises(DBAPIError):
                with session.begin():
                    set_local_user_id(session, employee_id)
                    session.execute(
                        text(
                            "INSERT INTO audit_events "
                            "(event_type, actor_user_id, actor_role, metadata) "
                            "VALUES ('auth.logout', :actor_id, 'employee', "
                            """'{"password":"not-a-real-secret"}'::jsonb)"""
                        ),
                        {"actor_id": employee_id},
                    )

        with Session(app_engine) as session:
            with pytest.raises(DBAPIError):
                with session.begin():
                    set_local_user_id(session, employee_id)
                    session.execute(
                        text(
                            "INSERT INTO audit_events "
                            "(event_type, actor_user_id, actor_role, subject_type) "
                            "VALUES ('auth.logout', :actor_id, 'employee', 'secret-value')"
                        ),
                        {"actor_id": employee_id},
                    )

        for statement in (
            text("UPDATE audit_events SET event_type = 'auth.login_failed' WHERE id = :id"),
            text("DELETE FROM audit_events WHERE id = :id"),
            text("TRUNCATE audit_events"),
        ):
            with Session(app_engine) as session:
                with pytest.raises(DBAPIError):
                    with session.begin():
                        set_local_user_id(session, auditor_id)
                        parameters = {"id": event_id} if ":id" in str(statement) else {}
                        session.execute(statement, parameters)

        for statement in (
            text("UPDATE audit_events SET event_type = 'auth.login_failed' WHERE id = :id"),
            text("DELETE FROM audit_events WHERE id = :id"),
            text("TRUNCATE audit_events"),
        ):
            with pytest.raises(DBAPIError):
                with owner_engine.begin() as connection:
                    parameters = {"id": event_id} if ":id" in str(statement) else {}
                    connection.execute(statement, parameters)

        assert _visible_audit_ids(app_engine, auditor_id) == {event_id}
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": [employee_id, admin_id, auditor_id]},
            )
        app_engine.dispose()


def test_knowledge_space_rls_hides_drafts_and_limits_department_reads() -> None:
    from estafeta.core.config import get_settings

    reset_engine()
    clear_settings_cache()
    settings = get_settings()
    owner_engine = create_engine(settings.database_owner_url)
    app_engine = create_engine(settings.database_url)
    department_id = uuid4()
    other_department_id = uuid4()
    author_position_id = uuid4()
    reader_position_id = uuid4()
    outside_position_id = uuid4()
    author_id = uuid4()
    reader_id = uuid4()
    outside_id = uuid4()
    admin_id = uuid4()
    published_record_id = uuid4()
    private_record_id = uuid4()
    outside_record_id = uuid4()
    published_material_id = uuid4()
    private_material_id = uuid4()
    outside_material_id = uuid4()
    version_id = uuid4()
    created_at = datetime.now(UTC)

    try:
        with owner_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO departments (id, name) VALUES (:id, :name)"),
                [
                    {"id": department_id, "name": f"knowledge-{department_id}"},
                    {"id": other_department_id, "name": f"outside-{other_department_id}"},
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO positions (id, department_id, title) "
                    "VALUES (:id, :department_id, :title)"
                ),
                [
                    {
                        "id": author_position_id,
                        "department_id": department_id,
                        "title": f"author-{author_position_id}",
                    },
                    {
                        "id": reader_position_id,
                        "department_id": department_id,
                        "title": f"reader-{reader_position_id}",
                    },
                    {
                        "id": outside_position_id,
                        "department_id": other_department_id,
                        "title": f"outside-{outside_position_id}",
                    },
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, full_name, email, role) "
                    "VALUES (:id, :name, :email, :role)"
                ),
                [
                    {
                        "id": author_id,
                        "name": "Knowledge author",
                        "email": f"knowledge-author-{author_id}@example.test",
                        "role": "employee",
                    },
                    {
                        "id": reader_id,
                        "name": "Knowledge reader",
                        "email": f"knowledge-reader-{reader_id}@example.test",
                        "role": "employee",
                    },
                    {
                        "id": outside_id,
                        "name": "Knowledge outsider",
                        "email": f"knowledge-outsider-{outside_id}@example.test",
                        "role": "employee",
                    },
                    {
                        "id": admin_id,
                        "name": "Knowledge administrator",
                        "email": f"knowledge-admin-{admin_id}@example.test",
                        "role": "admin",
                    },
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO employment_assignments "
                    "(id, user_id, position_id, starts_at) "
                    "VALUES (:id, :user_id, :position_id, :starts_at)"
                ),
                [
                    {
                        "id": uuid4(),
                        "user_id": author_id,
                        "position_id": author_position_id,
                        "starts_at": created_at,
                    },
                    {
                        "id": uuid4(),
                        "user_id": reader_id,
                        "position_id": reader_position_id,
                        "starts_at": created_at,
                    },
                    {
                        "id": uuid4(),
                        "user_id": outside_id,
                        "position_id": outside_position_id,
                        "starts_at": created_at,
                    },
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO materials "
                    "(id, owner_id, position_id, title, scope, status) "
                    "VALUES (:id, :owner_id, :position_id, :title, :scope, :status)"
                ),
                [
                    {
                        "id": published_material_id,
                        "owner_id": author_id,
                        "position_id": author_position_id,
                        "title": "Published",
                        "scope": "position",
                        "status": "published",
                    },
                    {
                        "id": private_material_id,
                        "owner_id": author_id,
                        "position_id": None,
                        "title": "Private",
                        "scope": "personal",
                        "status": "draft",
                    },
                    {
                        "id": outside_material_id,
                        "owner_id": outside_id,
                        "position_id": outside_position_id,
                        "title": "Outside",
                        "scope": "position",
                        "status": "published",
                    },
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO material_versions "
                    "(id, material_id, version_number, source_kind, cleaned_text, "
                    "content_hash, sanitizer_version, created_by) "
                    "VALUES (:id, :material_id, 1, 'note', :content, :hash, '1', :author)"
                ),
                [
                    {
                        "id": version_id,
                        "material_id": published_material_id,
                        "content": "Department-shared material text",
                        "hash": b"x" * 32,
                        "author": author_id,
                    }
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO knowledge_records "
                    "(id, owner_id, position_id, category, title, content, scope, status) "
                    "VALUES (:id, :owner, :position, 'processes', :title, :content, "
                    ":scope, :status)"
                ),
                [
                    {
                        "id": published_record_id,
                        "owner": author_id,
                        "position": author_position_id,
                        "title": "Shared record",
                        "content": "Published department process",
                        "scope": "position",
                        "status": "published",
                    },
                    {
                        "id": private_record_id,
                        "owner": author_id,
                        "position": None,
                        "title": "Private record",
                        "content": "Author-only draft",
                        "scope": "personal",
                        "status": "draft",
                    },
                    {
                        "id": outside_record_id,
                        "owner": outside_id,
                        "position": outside_position_id,
                        "title": "Outside record",
                        "content": "Other department",
                        "scope": "position",
                        "status": "published",
                    },
                ],
            )

        with Session(app_engine) as session, session.begin():
            set_local_user_id(session, reader_id)
            assert set(session.scalars(text("SELECT id FROM knowledge_records"))) == {
                published_record_id
            }
            assert set(session.scalars(text("SELECT id FROM materials"))) == {
                published_material_id
            }
            assert set(
                session.scalars(text("SELECT material_id FROM material_versions"))
            ) == {published_material_id}
            result = session.execute(
                text(
                    "UPDATE knowledge_records SET status = 'archived' "
                    "WHERE id = :record_id"
                ),
                {"record_id": published_record_id},
            )
            assert result.rowcount == 0

        with Session(app_engine) as session, session.begin():
            set_local_user_id(session, author_id)
            assert set(session.scalars(text("SELECT id FROM knowledge_records"))) == {
                published_record_id,
                private_record_id,
            }
            assert set(session.scalars(text("SELECT id FROM materials"))) == {
                published_material_id,
                private_material_id,
            }

        with Session(app_engine) as session, session.begin():
            set_local_user_id(session, outside_id)
            assert list(session.scalars(text("SELECT id FROM knowledge_records"))) == []
            assert list(session.scalars(text("SELECT id FROM materials"))) == []

        with Session(app_engine) as session:
            set_local_user_id(session, admin_id)
            assert list(session.scalars(text("SELECT id FROM knowledge_records"))) == []
            assert list(session.scalars(text("SELECT id FROM materials"))) == []
            with pytest.raises(DBAPIError):
                session.execute(text("DELETE FROM knowledge_records"))
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM knowledge_records WHERE id = ANY(:ids)"),
                {"ids": [published_record_id, private_record_id, outside_record_id]},
            )
            connection.execute(
                text("DELETE FROM material_versions WHERE id = :id"),
                {"id": version_id},
            )
            connection.execute(
                text("DELETE FROM materials WHERE id = ANY(:ids)"),
                {"ids": [published_material_id, private_material_id, outside_material_id]},
            )
            connection.execute(
                text("DELETE FROM employment_assignments WHERE user_id = ANY(:ids)"),
                {"ids": [author_id, reader_id, outside_id]},
            )
            connection.execute(
                text("DELETE FROM users WHERE id = ANY(:ids)"),
                {"ids": [author_id, reader_id, outside_id, admin_id]},
            )
            connection.execute(
                text("DELETE FROM positions WHERE id = ANY(:ids)"),
                {"ids": [author_position_id, reader_position_id, outside_position_id]},
            )
            connection.execute(
                text("DELETE FROM departments WHERE id = ANY(:ids)"),
                {"ids": [department_id, other_department_id]},
            )
        app_engine.dispose()
        owner_engine.dispose()


def _visible_audit_ids(engine: Engine, user_id: UUID) -> set[int]:
    with Session(engine) as session, session.begin():
        set_local_user_id(session, user_id)
        result = session.execute(text("SELECT id FROM audit_events"))
        return {row.id for row in result}
