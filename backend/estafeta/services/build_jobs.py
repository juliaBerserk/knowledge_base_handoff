import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from estafeta.core.db import set_local_user_id
from estafeta.domain.access import Role
from estafeta.models import (
    BuildJob,
    Chunk,
    KnowledgeRecord,
    Material,
    MaterialVersion,
    RecordSource,
    User,
)
from estafeta.models.indexing import EMBEDDING_DIMENSIONS
from estafeta.schemas.audit import AuditEventType, AuditMetadata
from estafeta.services.audit import record_audit_event
from estafeta.services.chunking import split_text
from estafeta.services.document_input import ExtractedDocument
from estafeta.services.embeddings import EMBEDDING_BATCH_SIZE, Embedder
from estafeta.services.extraction import (
    EXTRACTION_BATCH_SIZE,
    EXTRACTION_PROMPT_VERSION,
    ExtractedRecord,
    LLMClient,
)
from estafeta.services.materials import prepare_material
from estafeta.services.sanitization import SanitizationError

logger = logging.getLogger("estafeta.build")
type SessionFactory = sessionmaker[Session]


class MaterialChangedDuringBuild(Exception):
    pass


class BuildProcessingError(Exception):
    pass


def create_build_job(session: Session, *, user: User) -> BuildJob:
    set_local_user_id(session, user.id)
    if user.role in {Role.ADMIN, Role.SECURITY_AUDITOR} or not user.is_active:
        raise PermissionError("Нет доступа к сборке базы")
    material_exists = session.scalar(
        select(Material.id)
        .where(
            Material.owner_id == user.id,
            Material.status != "archived",
        )
        .limit(1)
    )
    if material_exists is None:
        raise ValueError("Нет доступных материалов для сборки")
    job = BuildJob(
        id=uuid4(),
        requested_by=user.id,
        status="pending",
        stage="queued",
    )
    session.add(job)
    session.flush()
    record_audit_event(
        session,
        AuditEventType.BUILD_JOB_CREATED,
        actor_user_id=user.id,
        actor_role=user.role.value,
        subject_type="job",
        subject_id=job.id,
    )
    return job


def list_build_jobs(session: Session, *, user: User) -> list[BuildJob]:
    set_local_user_id(session, user.id)
    return list(
        session.scalars(
            select(BuildJob)
            .where(BuildJob.requested_by == user.id)
            .order_by(BuildJob.created_at.desc(), BuildJob.id)
            .limit(50)
        )
    )


def get_build_job(session: Session, *, user: User, job_id: UUID) -> BuildJob | None:
    set_local_user_id(session, user.id)
    return session.scalar(
        select(BuildJob).where(
            BuildJob.id == job_id,
            BuildJob.requested_by == user.id,
        )
    )


def claim_build_job(session_factory: SessionFactory) -> tuple[UUID, UUID] | None:
    with session_factory() as session, session.begin():
        row = session.execute(
            text("SELECT job_id, requested_by FROM app_claim_build_job()")
        ).one_or_none()
        if row is None:
            return None
        return row.job_id, row.requested_by


def run_claimed_job(
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    requested_by: UUID,
    embedder: Embedder,
    llm: LLMClient,
) -> None:
    material_ids = _owned_material_ids(session_factory, requested_by)
    _update_job(
        session_factory,
        job_id=job_id,
        requested_by=requested_by,
        total_count=len(material_ids),
        stage="chunking",
    )
    for material_id in material_ids:
        try:
            _build_material(
                session_factory,
                job_id=job_id,
                requested_by=requested_by,
                material_id=material_id,
                embedder=embedder,
                llm=llm,
            )
            _finish_material(
                session_factory,
                job_id=job_id,
                requested_by=requested_by,
                failed=False,
            )
        except MaterialChangedDuringBuild:
            _finish_material(
                session_factory,
                job_id=job_id,
                requested_by=requested_by,
                failed=False,
            )
        except Exception as exc:
            logger.error(
                "Сбой обработки материала в задании %s (материал %s, тип %s)",
                job_id,
                material_id,
                type(exc).__name__,
            )
            _finish_material(
                session_factory,
                job_id=job_id,
                requested_by=requested_by,
                failed=True,
            )

    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        job = session.get(BuildJob, job_id)
        user = session.get(User, requested_by)
        if job is None or user is None:
            raise BuildProcessingError("Задание сборки исчезло из очереди")
        job.status = "completed_with_errors" if job.failed_count else "completed"
        job.stage = "complete"
        job.completed_at = datetime.now(UTC)
        job.lease_started_at = None
        session.flush()
        record_audit_event(
            session,
            AuditEventType.BUILD_JOB_COMPLETED,
            actor_user_id=requested_by,
            actor_role=user.role.value,
            subject_type="job",
            subject_id=job_id,
        )


def _owned_material_ids(session_factory: SessionFactory, user_id: UUID) -> list[UUID]:
    with session_factory() as session, session.begin():
        set_local_user_id(session, user_id)
        return list(
            session.scalars(
                select(Material.id)
                .where(
                    Material.owner_id == user_id,
                    Material.status != "archived",
                )
                .order_by(Material.created_at, Material.id)
            )
        )


def _build_material(
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    requested_by: UUID,
    material_id: UUID,
    embedder: Embedder,
    llm: LLMClient,
) -> None:
    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        material = session.get(Material, material_id)
        if (
            material is None
            or material.owner_id != requested_by
            or material.status == "archived"
        ):
            raise BuildProcessingError("Материал больше недоступен")
        version = session.scalar(
            select(MaterialVersion)
            .where(MaterialVersion.material_id == material.id)
            .order_by(MaterialVersion.version_number.desc())
            .limit(1)
        )
        if version is None:
            raise BuildProcessingError("У материала нет сохранённой версии")
        current_chunks = session.scalar(
            select(Chunk.id)
            .where(
                Chunk.material_version_id == version.id,
                Chunk.is_current.is_(True),
            )
            .limit(1)
        )
        if current_chunks is not None:
            return
        clean_text = version.cleaned_text
        version_id = version.id

    chunks = split_text(clean_text)
    if not chunks:
        raise BuildProcessingError("В материале нет текста для индексации")
    vectors: list[list[float]] = []
    for offset in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
        batch = chunks[offset : offset + EMBEDDING_BATCH_SIZE]
        _update_job(
            session_factory,
            job_id=job_id,
            requested_by=requested_by,
            stage="embedding",
            refresh_lease=True,
        )
        batch_vectors = embedder.embed([chunk.text for chunk in batch])
        if len(batch_vectors) != len(batch):
            raise BuildProcessingError("Эмбеддер вернул неверное число векторов")
        if any(
            len(vector) != EMBEDDING_DIMENSIONS
            or any(not _is_finite(value) for value in vector)
            for vector in batch_vectors
        ):
            raise BuildProcessingError("Эмбеддер вернул некорректный вектор")
        vectors.extend(batch_vectors)

    extracted: dict[
        tuple[str, str, str],
        tuple[int, ExtractedRecord, str, str, dict[str, int]],
    ] = {}
    for offset in range(0, len(chunks), EXTRACTION_BATCH_SIZE):
        batch = chunks[offset : offset + EXTRACTION_BATCH_SIZE]
        _update_job(
            session_factory,
            job_id=job_id,
            requested_by=requested_by,
            stage="extracting",
            refresh_lease=True,
        )
        batch_ordinals = {chunk.ordinal for chunk in batch}
        for item in llm.extract(batch):
            if item.source_chunk_ordinal not in batch_ordinals:
                raise BuildProcessingError("Модель вернула неизвестный номер фрагмента")
            prepared = _sanitize_extracted_record(
                session_factory,
                requested_by=requested_by,
                title=item.title,
                content=item.content,
            )
            if not prepared[0].strip() or not prepared[1].strip():
                continue
            key = (
                item.category.value,
                prepared[0].casefold(),
                prepared[1].casefold(),
            )
            extracted.setdefault(
                key,
                (
                    item.source_chunk_ordinal,
                    item,
                    prepared[0],
                    prepared[1],
                    prepared[2],
                )
            )

    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        material = session.get(Material, material_id)
        latest_version = session.scalar(
            select(MaterialVersion)
            .where(MaterialVersion.material_id == material_id)
            .order_by(MaterialVersion.version_number.desc())
            .limit(1)
        )
        if (
            material is None
            or material.owner_id != requested_by
            or material.status == "archived"
            or latest_version is None
            or latest_version.id != version_id
        ):
            raise MaterialChangedDuringBuild
        actor = session.get(User, requested_by)
        if actor is None:
            raise BuildProcessingError("Автор задания больше недоступен")
        actor_role = actor.role.value

        session.execute(
            update(Chunk)
            .where(Chunk.material_id == material_id, Chunk.is_current.is_(True))
            .values(is_current=False)
        )
        session.execute(
            update(KnowledgeRecord)
            .where(
                KnowledgeRecord.source_material_id == material_id,
                KnowledgeRecord.record_origin == "extracted",
                KnowledgeRecord.is_manually_edited.is_(False),
                KnowledgeRecord.is_current.is_(True),
            )
            .values(is_current=False)
        )
        chunk_rows = [
            Chunk(
                id=uuid4(),
                material_id=material_id,
                material_version_id=version_id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                embedding=vector,
                is_current=True,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        session.add_all(chunk_rows)
        session.flush()
        chunks_by_ordinal = {chunk.ordinal: chunk for chunk in chunk_rows}
        record_rows: list[KnowledgeRecord] = []
        source_rows: list[RecordSource] = []
        for ordinal, item, record_title, content, redactions in extracted.values():
            record = KnowledgeRecord(
                id=uuid4(),
                owner_id=requested_by,
                source_material_id=material_id,
                source_material_version_id=version_id,
                category=item.category.value,
                title=record_title,
                content=content,
                scope="personal",
                status="draft",
                record_origin="extracted",
                is_current=True,
                is_manually_edited=False,
                confidence=item.confidence,
                extraction_model=llm.model,
                extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
            )
            record_rows.append(record)
            source_rows.append(
                RecordSource(
                    id=uuid4(),
                    record_id=record.id,
                    chunk_id=chunks_by_ordinal[ordinal].id,
                    confidence=item.confidence,
                )
            )
            for finding_type, redacted_count in redactions.items():
                record_audit_event(
                    session,
                    AuditEventType.RECORD_REDACTION_RECORDED,
                    actor_user_id=requested_by,
                    actor_role=actor_role,
                    subject_type="record",
                    subject_id=record.id,
                    metadata=AuditMetadata(
                        finding_type=finding_type,
                        redacted_count=redacted_count,
                    ),
                )
        session.add_all([*record_rows, *source_rows])
        _update_job_in_session(
            session,
            job_id=job_id,
            stage="extracting",
            refresh_lease=True,
        )


def _sanitize_extracted_record(
    session_factory: SessionFactory,
    *,
    requested_by: UUID,
    title: str,
    content: str,
) -> tuple[str, str, dict[str, int]]:
    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        try:
            prepared = prepare_material(
                session,
                title=title,
                document=ExtractedDocument(content, "note"),
            )
        except (ValueError, SanitizationError) as exc:
            raise BuildProcessingError("Не удалось очистить извлечённую запись") from exc
        return (
            prepared.title.cleaned_text,
            prepared.content.cleaned_text,
            prepared.redaction_counts(),
        )


def _update_job(
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    requested_by: UUID,
    total_count: int | None = None,
    stage: str | None = None,
    refresh_lease: bool = False,
) -> None:
    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        job = session.get(BuildJob, job_id)
        if job is None:
            raise BuildProcessingError("Задание сборки исчезло из очереди")
        if total_count is not None:
            job.total_count = total_count
        if stage is not None:
            job.stage = stage
        if refresh_lease:
            job.lease_started_at = datetime.now(UTC)


def _update_job_in_session(
    session: Session,
    *,
    job_id: UUID,
    stage: str,
    refresh_lease: bool,
) -> None:
    job = session.get(BuildJob, job_id)
    if job is None:
        raise BuildProcessingError("Задание сборки исчезло из очереди")
    job.stage = stage
    if refresh_lease:
        job.lease_started_at = datetime.now(UTC)


def _finish_material(
    session_factory: SessionFactory,
    *,
    job_id: UUID,
    requested_by: UUID,
    failed: bool,
) -> None:
    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        job = session.get(BuildJob, job_id)
        if job is None:
            raise BuildProcessingError("Задание сборки исчезло из очереди")
        job.processed_count += 1
        if failed:
            job.failed_count += 1
        job.lease_started_at = datetime.now(UTC)


def _is_finite(value: float) -> bool:
    return value == value and abs(value) != float("inf")
