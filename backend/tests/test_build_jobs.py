import hashlib
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from estafeta.domain.access import KnowledgeCategory, Role
from estafeta.models import (
    Base,
    BuildJob,
    Chunk,
    KnowledgeRecord,
    Material,
    MaterialVersion,
    RecordSource,
    User,
)
from estafeta.services.build_jobs import get_build_job, run_claimed_job
from estafeta.services.chunking import TextChunk
from estafeta.services.extraction import ExtractedRecord


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if texts and texts[0].startswith("failing material"):
            raise RuntimeError("model failure")
        return [[0.0] * 1024 for _ in texts]


class _FakeLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.calls: list[list[TextChunk]] = []

    def extract(self, chunks: list[TextChunk]) -> list[ExtractedRecord]:
        self.calls.append(chunks)
        return [
            ExtractedRecord(
                source_chunk_ordinal=chunk.ordinal,
                category=KnowledgeCategory.PROCESSES,
                title="Проверка резервной копии",
                content="Проверить резервную копию перед обновлением.",
                confidence=0.9,
            )
            for chunk in chunks
        ]


def _create_database(tmp_path, monkeypatch) -> tuple[sessionmaker[Session], UUID, UUID]:
    engine = create_engine(f"sqlite:///{tmp_path / 'build-jobs.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("estafeta.services.build_jobs.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr(
        "estafeta.services.sanitization._natasha_candidates",
        lambda *_args: [],
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    user_id = uuid4()
    material_id = uuid4()
    text = "A" * 1800
    with session_factory() as session, session.begin():
        session.add(
            User(
                id=user_id,
                full_name="Тестовый сотрудник",
                email="employee@example.test",
                role=Role.EMPLOYEE,
            )
        )
        session.add(
            Material(
                id=material_id,
                owner_id=user_id,
                title="Материал для сборки",
                scope="personal",
                status="draft",
            )
        )
        session.add(
            MaterialVersion(
                id=uuid4(),
                material_id=material_id,
                version_number=1,
                source_kind="note",
                cleaned_text=text,
                content_hash=hashlib.sha256(text.encode()).digest(),
                sanitizer_version="test",
                created_by=user_id,
            )
        )
        session.add(
            BuildJob(
                id=uuid4(),
                requested_by=user_id,
                status="running",
                stage="loading",
            )
        )
    with session_factory() as session:
        job_id = session.scalar(select(BuildJob.id))
    assert job_id is not None
    return session_factory, user_id, job_id


def test_build_indexes_incrementally_and_preserves_manual_edits(tmp_path, monkeypatch) -> None:
    session_factory, user_id, first_job_id = _create_database(tmp_path, monkeypatch)
    embedder = _FakeEmbedder()
    llm = _FakeLLM()

    run_claimed_job(
        session_factory,
        job_id=first_job_id,
        requested_by=user_id,
        embedder=embedder,
        llm=llm,
    )

    with session_factory() as session:
        chunks = list(session.scalars(select(Chunk).order_by(Chunk.ordinal)))
        records = list(session.scalars(select(KnowledgeRecord)))
        sources = list(session.scalars(select(RecordSource)))
        job = session.get(BuildJob, first_job_id)
        version_id = chunks[0].material_version_id
        material_id = chunks[0].material_id
        assert len(chunks) == 3
        assert len(records) == 1
        assert len(sources) == 1
        assert sources[0].chunk_id == chunks[0].id
        assert records[0].source_material_version_id == version_id
        assert records[0].extraction_model == "test-model"
        assert job is not None
        assert (job.status, job.processed_count, job.failed_count) == ("completed", 1, 0)
        record_id = records[0].id

    second_job_id = uuid4()
    with session_factory() as session, session.begin():
        session.add(
            BuildJob(
                id=second_job_id,
                requested_by=user_id,
                status="running",
                stage="loading",
            )
        )
    run_claimed_job(
        session_factory,
        job_id=second_job_id,
        requested_by=user_id,
        embedder=embedder,
        llm=llm,
    )
    assert len(embedder.calls) == 1
    assert len(llm.calls) == 1

    with session_factory() as session, session.begin():
        record = session.get(KnowledgeRecord, record_id)
        assert record is not None
        record.content = "Ручная правка сотрудника."
        record.is_manually_edited = True
        next_text = "Updated material " * 80
        session.add(
            MaterialVersion(
                id=uuid4(),
                material_id=material_id,
                version_number=2,
                source_kind="note",
                cleaned_text=next_text,
                content_hash=hashlib.sha256(next_text.encode()).digest(),
                sanitizer_version="test",
                created_by=user_id,
            )
        )
    third_job_id = uuid4()
    with session_factory() as session, session.begin():
        session.add(
            BuildJob(
                id=third_job_id,
                requested_by=user_id,
                status="running",
                stage="loading",
            )
        )
    run_claimed_job(
        session_factory,
        job_id=third_job_id,
        requested_by=user_id,
        embedder=embedder,
        llm=llm,
    )

    with session_factory() as session:
        edited_record = session.get(KnowledgeRecord, record_id)
        assert edited_record is not None
        assert edited_record.content == "Ручная правка сотрудника."
        assert edited_record.is_current is True
        assert len(
            list(
                session.scalars(
                    select(Chunk).where(
                        Chunk.material_id == material_id,
                        Chunk.is_current.is_(True),
                    )
                )
            )
        ) > 0
        assert len(
            list(
                session.scalars(
                    select(KnowledgeRecord).where(KnowledgeRecord.is_current.is_(True))
                )
            )
        ) == 2
        owner = session.get(User, user_id)
        assert owner is not None
        assert get_build_job(
            session,
            user=owner,
            job_id=first_job_id,
        ) is not None


def test_build_continues_after_a_material_failure(tmp_path, monkeypatch) -> None:
    session_factory, user_id, job_id = _create_database(tmp_path, monkeypatch)
    with session_factory() as session, session.begin():
        user = session.get(User, user_id)
        assert user is not None
        material_id = uuid4()
        text = "failing material " + "x" * 100
        session.add(
            Material(
                id=material_id,
                owner_id=user_id,
                title="Материал с ошибкой",
                scope="personal",
                status="draft",
            )
        )
        session.add(
            MaterialVersion(
                id=uuid4(),
                material_id=material_id,
                version_number=1,
                source_kind="note",
                cleaned_text=text,
                content_hash=hashlib.sha256(text.encode()).digest(),
                sanitizer_version="test",
                created_by=user_id,
            )
        )

    run_claimed_job(
        session_factory,
        job_id=job_id,
        requested_by=user_id,
        embedder=_FakeEmbedder(),
        llm=_FakeLLM(),
    )

    with session_factory() as session:
        job = session.get(BuildJob, job_id)
        assert job is not None
        assert job.status == "completed_with_errors"
        assert job.processed_count == 2
        assert job.failed_count == 1
