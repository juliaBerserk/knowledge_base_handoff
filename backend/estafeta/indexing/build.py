import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from estafeta.core.config import Settings
from estafeta.core.permissions import Relation, can_read, can_write
from estafeta.indexing.chunking import Fragment, split_text
from estafeta.indexing.extraction import PROMPT_VERSION, Entry, extract_batch
from estafeta.indexing.providers import Embedder, LLMClient, ProviderError, embedding_key
from estafeta.indexing.queue import Claim, guard
from estafeta.ingest.repository import current_uploader, personal_space
from estafeta.models import KnowledgeRecord, KnowledgeStatus, Material, SpaceKind
from estafeta.models.indexing import Chunk


class SourceChanged(RuntimeError):
    pass


@dataclass
class Progress:
    total: int
    built: int = 0
    skipped: int = 0
    failed: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def payload(self) -> dict[str, object]:
        return {
            "total": self.total,
            "built": self.built,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": dict(self.errors),
        }


def digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def build_fingerprint(source_hash: str, settings: Settings) -> str:
    return digest(
        json.dumps(
            [
                source_hash,
                "chunker-v2",
                settings.chunk_size,
                settings.chunk_overlap,
                settings.embedding_model,
                settings.embedding_revision,
                settings.llm_base_url,
                settings.llm_model,
                PROMPT_VERSION,
            ]
        )
    )


def source_query(actor: UUID) -> sa.Select:
    return sa.select(Material.id).where(
        Material.author_id == actor,
        Material.cleaned_text.is_not(None),
        Material.status.in_((KnowledgeStatus.DRAFT, KnowledgeStatus.PUBLISHED)),
    )


def fetch_source(db: Session, actor: UUID, material_id: UUID) -> str:
    relation = sa.func.knowledge.access_relation(Material.space_id)
    row = db.execute(
        source_query(actor)
        .where(Material.id == material_id)
        .add_columns(Material.cleaned_text, Material.space_kind, Material.status, relation)
    ).one_or_none()
    if row is None:
        raise SourceChanged
    _, text, kind, status, key = row
    if not text or not can_read(Relation(key), kind, status):
        raise SourceChanged
    return text


def batches[T](items: list[T], size: int) -> Iterator[tuple[int, list[T]]]:
    for offset in range(0, len(items), size):
        yield offset, items[offset : offset + size]


def persist(
    db: Session,
    actor: UUID,
    material_id: UUID,
    source_hash: str,
    fingerprint: str,
    fragments: list[Fragment],
    vectors: list[list[float]],
    entries: list[tuple[int, Entry]],
    model_key: str,
) -> None:
    if digest(fetch_source(db, actor, material_id)) != source_hash:
        raise SourceChanged
    if not can_write(Relation.OWNER, KnowledgeStatus.DRAFT):
        raise PermissionError
    table = Chunk.__table__
    old_chunks = sa.select(Chunk.id).where(Chunk.material_id == material_id)
    db.execute(
        sa.update(table)
        .where(
            Chunk.material_id == material_id,
            Chunk.active,
        )
        .values(active=False)
    )
    ids = [uuid4() for _ in fragments]
    db.execute(
        sa.insert(table).inline(),
        [
            {
                "id": chunk_id,
                "material_id": material_id,
                "ordinal": ordinal,
                "text": fragment.text,
                "start_offset": fragment.start,
                "end_offset": fragment.end,
                "embedding": vector,
                "embedding_key": model_key,
                "source_hash": source_hash,
                "fingerprint": fingerprint,
            }
            for ordinal, (chunk_id, fragment, vector) in enumerate(
                zip(ids, fragments, vectors, strict=True)
            )
        ],
    )
    space = personal_space(db, actor) if entries else None
    records = KnowledgeRecord.__table__
    known: set[UUID] = set()
    for ordinal, entry in entries:
        record_id = uuid5(material_id, f"{entry.category}:{entry.quote}")
        if record_id in known:
            continue
        known.add(record_id)
        stmt = insert(records).values(
            id=record_id,
            space_id=space,
            space_kind=SpaceKind.PERSONAL,
            author_id=actor,
            status=KnowledgeStatus.DRAFT,
            cleaned_text=entry.quote,
            source_chunk_id=ids[ordinal],
            category=entry.category,
            confidence=entry.confidence,
        )
        db.execute(stmt.on_conflict_do_nothing(index_elements=["id"]).inline())
        db.execute(
            sa.update(records)
            .where(
                records.c.id == record_id,
                records.c.author_id == actor,
                records.c.status == KnowledgeStatus.DRAFT,
                ~records.c.manually_edited,
            )
            .values(source_chunk_id=ids[ordinal])
        )
    db.execute(
        sa.update(records)
        .where(
            records.c.author_id == actor,
            records.c.status == KnowledgeStatus.DRAFT,
            ~records.c.manually_edited,
            records.c.source_chunk_id.in_(old_chunks),
            records.c.id.not_in(known),
        )
        .values(status=KnowledgeStatus.ARCHIVED)
    )


class Builder:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        embedder: Embedder,
        client: LLMClient,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.embedder = embedder
        self.client = client

    def stage(self, claim: Claim, stage: str, material_id: UUID, source_hash: str) -> None:
        with self.factory.begin() as db:
            guard(db, claim).stage = stage
            if digest(fetch_source(db, claim.owner_id, material_id)) != source_hash:
                raise SourceChanged

    def build_one(self, claim: Claim, material_id: UUID) -> bool:
        with self.factory.begin() as db:
            job = guard(db, claim)
            job.stage = "chunking"
            text = fetch_source(db, claim.owner_id, material_id)
            source_hash = digest(text)
            fingerprint = build_fingerprint(source_hash, self.settings)
            exists = db.scalar(
                sa.select(Chunk.id)
                .where(
                    Chunk.material_id == material_id,
                    Chunk.active,
                    Chunk.fingerprint == fingerprint,
                )
                .limit(1)
            )
            if exists is not None:
                return False
        fragments = split_text(text, self.settings.chunk_size, self.settings.chunk_overlap)
        if not fragments:
            raise SourceChanged
        vectors: list[list[float]] = []
        for _, batch in batches(fragments, 32):
            self.stage(claim, "embedding", material_id, source_hash)
            vectors.extend(self.embedder.embed([part.text for part in batch]))
        if len(vectors) != len(fragments) or any(
            len(vector) != 1024 or not any(vector) or not all(map(math.isfinite, vector))
            for vector in vectors
        ):
            raise ProviderError("Неверный размер или значения вектора")
        entries: list[tuple[int, Entry]] = []
        for offset, batch in batches(fragments, 8):
            self.stage(claim, "extracting", material_id, source_hash)
            entries.extend(
                (offset + entry.fragment, entry) for entry in extract_batch(self.client, batch)
            )
        self.stage(claim, "saving", material_id, source_hash)
        with self.factory.begin() as db:
            guard(db, claim)
            persist(
                db,
                claim.owner_id,
                material_id,
                source_hash,
                fingerprint,
                fragments,
                vectors,
                entries,
                embedding_key(self.settings.embedding_model, self.settings.embedding_revision),
            )
        return True

    def run(self, claim: Claim) -> None:
        with self.factory.begin() as db:
            job = guard(db, claim)
            if current_uploader(db) != claim.owner_id:
                job.status, job.stage = "failed", "finished"
                job.lease = job.leased_until = None
                job.progress = {"error": "access_revoked"}
                return
            material_ids = list(db.scalars(source_query(claim.owner_id).order_by(Material.id)))
            progress = Progress(total=len(material_ids))
            job.progress = progress.payload()
        for material_id in material_ids:
            try:
                built = self.build_one(claim, material_id)
            except SourceChanged:
                progress.skipped += 1
            except (ProviderError, sa.exc.SQLAlchemyError) as exc:
                progress.failed += 1
                if len(progress.errors) < 100:
                    progress.errors[str(material_id)] = (
                        "provider" if isinstance(exc, ProviderError) else "storage"
                    )
            else:
                progress.built += int(built)
                progress.skipped += int(not built)
            with self.factory.begin() as db:
                guard(db, claim).progress = progress.payload()
        with self.factory.begin() as db:
            job = guard(db, claim)
            job.status = (
                "succeeded"
                if not progress.failed
                else "failed"
                if progress.failed == progress.total
                else "partial"
            )
            job.stage = "finished"
            job.lease = job.leased_until = None
