import json
import os
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from estafeta.api import rag
from estafeta.api.auth import COOKIE
from estafeta.auth.security import csrf_token
from estafeta.auth.service import create_session
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.indexing.providers import Delta, embedding_key
from estafeta.ingest.formats import SourceKind
from estafeta.main import app
from estafeta.models import (
    Department,
    Employee,
    KnowledgeSpace,
    Material,
    Position,
    PositionAssignment,
    Role,
    SpaceKind,
)
from estafeta.models.audit import Action, AuditEvent
from estafeta.models.auth import Credential
from estafeta.models.chat import Chat, ChatTurn
from estafeta.models.indexing import Chunk

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")
VECTOR = [1.0] + [0.0] * 1023
POSITION_TEXT = "Регламент. Проверить журнал смены."


class FixedEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [VECTOR for _ in texts]


class Model:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, system: str, user: str) -> Generator[Delta, None, None]:
        self.calls += 1
        assert "Чужой личный" not in user
        assert "Мой личный" not in user
        assert "Другая должность" not in user
        assert "sample-password-abc" not in user
        assert POSITION_TEXT in user
        yield Delta('{"source":1,"quote":"Выдуманный факт"}\n', "fixture-model-2026")
        yield Delta('{"source":1,"quote":"Проверить журнал смены."}\n', "fixture-model-2026")


def seed(db: Session) -> tuple[dict[str, UUID], dict[str, str]]:
    cfg = get_settings()
    ids: dict[str, UUID] = {}
    tokens: dict[str, str] = {}
    team = Department(name=f"Тестовый отдел {uuid4()}")
    db.add(team)
    db.flush()
    ids["team"] = team.id
    for key in ("current", "colleague"):
        employee = Employee(full_name=f"Тест {key}", role=Role.EMPLOYEE)
        position = Position(title=key, department_id=team.id)
        db.add_all([employee, position])
        db.flush()
        ids[f"employee_{key}"] = employee.id
        ids[f"position_{key}"] = position.id
        db.add(Credential(employee_id=employee.id, password_hash="fixture"))
        db.flush()
        tokens[key] = create_session(db, employee.id, datetime.now(UTC), False)
        db.add(
            PositionAssignment(
                employee_id=employee.id,
                position_id=position.id,
                started_at=datetime.now(UTC),
            )
        )
        for kind in (SpaceKind.PERSONAL, SpaceKind.POSITION):
            space = KnowledgeSpace(
                kind=kind,
                employee_id=employee.id if kind is SpaceKind.PERSONAL else None,
                position_id=position.id if kind is SpaceKind.POSITION else None,
            )
            db.add(space)
            db.flush()
            ids[f"space_{kind}_{key}"] = space.id
            content = {
                ("current", SpaceKind.PERSONAL): "Мой личный регламент.",
                ("current", SpaceKind.POSITION): POSITION_TEXT,
                ("colleague", SpaceKind.PERSONAL): "Чужой личный регламент.",
                ("colleague", SpaceKind.POSITION): "Другая должность. Регламент.",
            }[key, kind]
            material = Material(
                author_id=employee.id,
                space_id=space.id,
                space_kind=kind,
                status="draft" if kind is SpaceKind.PERSONAL else "published",
                source_kind=SourceKind.NOTE,
                cleaned_text=content,
            )
            db.add(material)
            db.flush()
            ids[f"material_{kind}_{key}"] = material.id
            chunk = Chunk(
                material_id=material.id,
                ordinal=0,
                text=content,
                start_offset=0,
                end_offset=len(content),
                source_hash=sha256(content.encode()).hexdigest(),
                fingerprint="test",
                embedding=VECTOR,
                embedding_key=embedding_key(cfg.embedding_model, cfg.embedding_revision),
            )
            db.add(chunk)
            db.flush()
    return ids, tokens


def parse_events(text: str) -> dict[str, dict[str, str]]:
    events = {}
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        events[name.removeprefix("event: ")] = json.loads(data.removeprefix("data: "))
    return events


def test_search_chat_history_feedback_and_revocation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    model = Model()
    monkeypatch.setattr(rag, "embedder", FixedEmbedder)
    monkeypatch.setattr(rag, "llm", lambda: model)
    with Session(engine) as db, db.begin():
        ids, tokens = seed(db)
    owners = [ids["employee_current"], ids["employee_colleague"]]
    key = get_settings().app_secret_key
    try:
        with TestClient(app, base_url="https://testserver") as client:

            def sign(user: str) -> dict[str, str]:
                token = tokens[user]
                client.cookies.set(COOKIE, token)
                return {"X-CSRF-Token": csrf_token(key, token)}

            headers = sign("current")
            for scope, expected in (
                ("personal", {"material_personal_current"}),
                ("position", {"material_position_current"}),
                ("department", {"material_position_current", "material_position_colleague"}),
            ):
                found = client.post(
                    "/api/search", headers=headers, json={"question": "регламент", "scope": scope}
                )
                assert found.status_code == 200, found.text
                assert found.json()["semantic_available"] is True
                assert {item["material_id"] for item in found.json()["results"]} == {
                    str(ids[name]) for name in expected
                }
            assert client.post("/api/chat/ask", json={"question": "что делать"}).status_code == 403
            response = client.post(
                "/api/chat/ask",
                headers=headers,
                json={
                    "question": "Как проходит смена?\nПароль: sample-password-abc",
                    "scope": "position",
                },
            )
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            events = parse_events(response.text)
            assert [*events] == ["started", "citation", "done"], events
            assert events["citation"]["text"] == "Проверить журнал смены."
            assert "Выдуманный факт" not in response.text
            assert "sample-password-abc" not in response.text
            chat_id, turn_id = events["started"]["chat_id"], events["done"]["turn_id"]
            detail = f"/api/chat/{chat_id}"
            feedback = f"/api/chat/turns/{turn_id}/feedback"
            other_headers = sign("colleague")
            assert client.get(detail).status_code == 404
            assert (
                client.post(detail + "/delete", json={}, headers=other_headers).status_code == 404
            )
            assert (
                client.post(feedback, json={"rating": -1}, headers=other_headers).status_code == 404
            )
            sign("current")
            assert client.get("/api/chat").json()["chats"][0]["id"] == chat_id
            assert (
                client.post(
                    feedback, json={"rating": 1, "comment": "Полезно"}, headers=headers
                ).json()["rating"]
                == 1
            )
            item = client.get(detail).json()["turns"][0]
            assert item["model_version"] == "fixture-model-2026"
            assert item["prompt_version"] == "grounded-quotes-v1"
            assert item["comment"] == "Полезно"
            with Session(engine) as db, db.begin():
                db.execute(
                    sa.update(Material)
                    .where(Material.id == ids["material_position_current"])
                    .values(status="archived")
                )
            item = client.get(detail).json()["turns"][0]
            assert item["segments"] == [] and "Доступ изменился" in item["message"]
            no_data = client.post(
                "/api/chat/ask",
                headers=headers,
                json={
                    "question": "Что делать?",
                    "scope": "position",
                    "chat_id": chat_id,
                },
            )
            assert parse_events(no_data.text)["done"]["message"] == "в базе нет информации"
            assert model.calls == 1
            assert client.post(detail + "/delete", json={}, headers=headers).status_code == 200
            assert client.get(detail).status_code == 404
        with Session(engine) as db:
            assert db.get(ChatTurn, UUID(turn_id)) is None
            actions = db.scalars(
                sa.select(AuditEvent).where(
                    AuditEvent.actor_id == ids["employee_current"],
                    AuditEvent.action == Action.AI_QUESTION,
                )
            ).all()
            assert len(actions) == 2
            assert {event.target_id for event in actions} == {UUID(chat_id)}
    finally:
        with Session(engine) as db, db.begin():
            db.execute(sa.delete(Chat).where(Chat.owner_id.in_(owners)))
            db.execute(sa.delete(Material).where(Material.author_id.in_(owners)))
            db.execute(
                sa.delete(KnowledgeSpace).where(
                    KnowledgeSpace.id.in_(
                        [value for name, value in ids.items() if name.startswith("space_")]
                    )
                )
            )
            db.execute(
                sa.delete(PositionAssignment).where(PositionAssignment.employee_id.in_(owners))
            )
            db.execute(sa.delete(Position).where(Position.department_id == ids["team"]))
            db.execute(sa.delete(Department).where(Department.id == ids["team"]))
            db.execute(sa.delete(Credential).where(Credential.employee_id.in_(owners)))
            db.execute(sa.delete(Employee).where(Employee.id.in_(owners)))
        engine.dispose()
