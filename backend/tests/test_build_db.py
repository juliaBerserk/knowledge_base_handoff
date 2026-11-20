import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from estafeta.api.auth import COOKIE
from estafeta.auth.security import csrf_token
from estafeta.auth.service import create_session
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.indexing.build import Builder
from estafeta.indexing.providers import ProviderError
from estafeta.indexing.queue import LeaseLost, claim_next, guard
from estafeta.ingest.formats import SourceKind
from estafeta.main import app
from estafeta.models import Employee, KnowledgeRecord, KnowledgeSpace, Material, Role, SpaceKind
from estafeta.models.auth import Credential
from estafeta.models.indexing import BuildJob, Chunk

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="нужен PostgreSQL")


class Embeddings:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1023 for _ in texts]


class ExtractionProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if "Ошибка модели" in user:
            raise ProviderError
        rows = json.loads(user.split("\n")[1])
        return json.dumps(
            {
                "entries": [
                    {
                        "category": "processes",
                        "quote": rows[0]["text"],
                        "fragment": 0,
                        "confidence": 0.8,
                    }
                ]
            }
        )


def test_build_queue_partial_restart_edit_and_index_privacy() -> None:
    cfg = get_settings()
    pg = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    worker = sa.create_engine(cfg.database_worker_url, hide_parameters=True)
    factory = sessionmaker(worker)
    provider = ExtractionProvider()
    builder = Builder(factory, cfg, Embeddings(), provider)
    with Session(pg) as db, db.begin():
        actor = Employee(full_name=f"Сборка {uuid4()}", role=Role.EMPLOYEE)
        other = Employee(full_name=f"Коллега {uuid4()}", role=Role.EMPLOYEE)
        db.add_all([actor, other])
        db.flush()
        actor_id, other_id = actor.id, other.id
        db.add_all(
            [Credential(employee_id=e, password_hash="fixture") for e in (actor_id, other_id)]
        )
        db.flush()
        token = create_session(db, actor_id, datetime.now(UTC), False)
        other_token = create_session(db, other_id, datetime.now(UTC), False)
        space = KnowledgeSpace(kind=SpaceKind.PERSONAL, employee_id=actor_id)
        db.add(space)
        db.flush()
        space_id = space.id
        good_id, bad_id = uuid4(), uuid4()
        for item_id, content in (
            (good_id, "Проверяйте очереди документов каждую смену."),
            (bad_id, "Ошибка модели на этом документе."),
        ):
            db.add(
                Material(
                    id=item_id,
                    author_id=actor_id,
                    space_id=space_id,
                    space_kind=SpaceKind.PERSONAL,
                    status="draft",
                    source_kind=SourceKind.NOTE,
                    cleaned_text=content,
                )
            )
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.cookies.set(COOKIE, token)
            headers = {"X-CSRF-Token": csrf_token(cfg.app_secret_key, token)}

            def enqueue_job() -> str:
                result = client.post("/api/build", json={}, headers=headers)
                assert result.status_code == 202, result.text
                return result.json()["id"]

            first = enqueue_job()
            assert enqueue_job() == first
            with factory.begin() as locked, factory.begin() as competing:
                assert locked.scalar(
                    sa.select(BuildJob).where(BuildJob.status == "queued").with_for_update()
                )
                assert claim_next(competing) is None
            with factory.begin() as db:
                old = claim_next(db)
            assert old is not None and str(old.id) == first
            with factory.begin() as db:
                assert claim_next(db) is None
                job = db.get(BuildJob, old.id)
                assert job
                job.leased_until = datetime.now(UTC) - timedelta(seconds=1)
            with factory.begin() as db:
                claimed = claim_next(db)
                assert claimed and claimed.id == old.id
            with factory.begin() as db, pytest.raises(LeaseLost):
                guard(db, old)
            builder.run(claimed)
            job = client.get("/api/build").json()["job"]
            assert job["status"] == "partial"
            assert job["progress"]["built"] == 1
            assert job["progress"]["errors"] == {str(bad_id): "provider"}
            assert provider.calls == 2
            with Session(pg) as db:
                chunk = db.scalar(sa.select(Chunk).where(Chunk.material_id == good_id))
                assert chunk
                chunk_id = chunk.id
                record_id = db.scalar(
                    sa.select(KnowledgeRecord.id).where(KnowledgeRecord.source_chunk_id == chunk_id)
                )
                assert record_id
                assert db.scalar(
                    sa.text(
                        "SELECT search_vector @@ plainto_tsquery('russian', 'документ') "
                        "FROM knowledge.chunks WHERE id = :id"
                    ),
                    {"id": chunk_id},
                )
                assert (
                    db.scalar(
                        sa.text(
                            "SELECT embedding <=> embedding FROM knowledge.chunks WHERE id = :id"
                        ),
                        {"id": chunk_id},
                    )
                    == 0
                )
            source_path = f"/api/build/sources/{chunk_id}"
            assert client.get(source_path).status_code == 200
            client.cookies.set(COOKIE, other_token)
            assert client.get(source_path).status_code == 404
            assert client.get("/api/build", params={"job_id": first}).status_code == 404
            client.cookies.set(COOKIE, token)
            edit = client.post(
                f"/api/build/records/{record_id}/edit",
                headers=headers,
                json={
                    "text": "Ручная правка.\nПароль: fixture-secret",
                },
            )
            assert edit.status_code == 200, edit.text
            assert "fixture-secret" not in edit.json()["text"]
            enqueue_job()
            with factory.begin() as db:
                second = claim_next(db)
                assert second
            builder.run(second)
            assert provider.calls == 3
            assert client.get("/api/build").json()["job"]["progress"]["skipped"] == 1

            with Session(pg) as db, db.begin():
                db.execute(
                    sa.update(Material)
                    .where(Material.id == good_id)
                    .values(cleaned_text="Обновлённая процедура проверки.")
                )
            assert client.get(source_path).status_code == 404
            enqueue_job()
            with factory.begin() as db:
                rebuild = claim_next(db)
                assert rebuild
            builder.run(rebuild)
            assert client.get(source_path).status_code == 200
            client.cookies.set(COOKIE, other_token)
            assert client.get(source_path).status_code == 404
            with Session(pg) as db:
                saved = db.get(KnowledgeRecord, record_id)
                assert saved and saved.cleaned_text == edit.json()["text"]
                assert saved.manually_edited and saved.status == "draft"
                replaced = db.get(Chunk, chunk_id)
                assert replaced and not replaced.active
    finally:
        with Session(pg) as db, db.begin():
            db.execute(sa.delete(BuildJob).where(BuildJob.owner_id == actor_id))
            db.execute(sa.delete(KnowledgeRecord).where(KnowledgeRecord.author_id == actor_id))
            db.execute(sa.delete(Material).where(Material.author_id == actor_id))
            db.execute(sa.delete(KnowledgeSpace).where(KnowledgeSpace.id == space_id))
            db.execute(
                sa.delete(Credential).where(Credential.employee_id.in_((actor_id, other_id)))
            )
            db.execute(sa.delete(Employee).where(Employee.id.in_((actor_id, other_id))))
        worker.dispose()
        pg.dispose()
