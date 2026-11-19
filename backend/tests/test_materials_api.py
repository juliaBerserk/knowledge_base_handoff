import json
import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from estafeta.api.auth import COOKIE
from estafeta.auth.security import csrf_token
from estafeta.auth.service import create_session
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.main import app
from estafeta.models import Employee, KnowledgeSpace, Material, Role
from estafeta.models.audit import Action, AuditEvent
from estafeta.models.auth import Credential

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")


def seed(db: Session) -> tuple[UUID, str]:
    employee = Employee(full_name="Сотрудник", role=Role.EMPLOYEE)
    db.add(employee)
    db.flush()
    db.add(Credential(employee_id=employee.id, password_hash="fixture"))
    db.flush()
    return employee.id, create_session(db, employee.id, datetime.now(UTC), False)


def test_preview_confirmation_with_rls_clean_storage_and_audit() -> None:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    with Session(engine) as db, db.begin():
        owner, owner_token = seed(db)
        other, other_token = seed(db)
    source = "Пароль: raw-secret-91\nКонтакт sample@external.example\nЧерновая пометка"
    raw = source.encode()
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.cookies.set(COOKIE, owner_token)
            headers = {
                "Content-Type": "application/octet-stream",
                "X-CSRF-Token": csrf_token(get_settings().app_secret_key, owner_token),
                "X-Material-Format": "note",
            }
            without_csrf = {key: value for key, value in headers.items() if "CSRF" not in key}
            response = client.post("/api/materials/preview", content=raw, headers=without_csrf)
            assert response.status_code == 403
            response = client.post("/api/materials/preview", content=raw, headers=headers)
            assert response.status_code == 200, response.text
            preview = response.json()
            assert "raw-secret-91" not in preview["text"]
            assert "sample@external.example" not in preview["text"]
            with Session(engine) as db:
                assert db.scalar(sa.select(Material.id).where(Material.author_id == owner)) is None
            assert client.post("/api/materials", content=raw, headers=headers).status_code == 400
            signed = headers | {"X-Material-Proof": preview["proof"]}
            assert (
                client.post("/api/materials", content=raw + b"!", headers=signed).status_code == 409
            )

            contact = next(
                marker["contact_id"] for marker in preview["markers"] if marker["contact_id"]
            )
            opted_in = headers | {"X-Material-Choices": json.dumps({"work": [contact]})}
            new_preview = client.post(
                "/api/materials/preview", content=raw, headers=opted_in
            ).json()
            assert "sample@external.example" in new_preview["text"]
            assert "raw-secret-91" not in new_preview["text"]
            opted_in["X-Material-Proof"] = new_preview["proof"]
            first = client.post("/api/materials", content=raw, headers=opted_in)
            assert first.status_code == 201, first.text
            material_id = UUID(first.json()["id"])
            repeated = client.post("/api/materials", content=raw, headers=opted_in)
            assert repeated.json()["id"] == str(material_id)
            assert client.get(f"/api/materials/{material_id}").json()["text"] == new_preview["text"]
            client.cookies.set(COOKIE, other_token)
            assert client.get(f"/api/materials/{material_id}").status_code == 404
        with Session(engine) as db:
            stored = db.get(Material, material_id)
            assert stored and stored.cleaned_text == new_preview["text"]
            assert "raw-secret-91" not in stored.cleaned_text
            events = db.scalars(
                sa.select(AuditEvent)
                .where(AuditEvent.target_id == material_id)
                .order_by(AuditEvent.id)
            ).all()
            assert [event.action for event in events] == [
                Action.MATERIAL_UPLOADED,
                Action.MATERIAL_REDACTED,
            ]
            assert events[1].redaction_kind == "password" and events[1].redaction_count == 1
    finally:
        ids = [owner, other]
        with Session(engine) as db, db.begin():
            db.execute(sa.delete(Material).where(Material.author_id.in_(ids)))
            db.execute(sa.delete(KnowledgeSpace).where(KnowledgeSpace.employee_id.in_(ids)))
            db.execute(sa.delete(Credential).where(Credential.employee_id.in_(ids)))
            db.execute(sa.delete(Employee).where(Employee.id.in_(ids)))
        engine.dispose()
