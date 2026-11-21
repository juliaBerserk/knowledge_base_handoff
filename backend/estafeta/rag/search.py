import math
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Session

from estafeta.core.config import Settings
from estafeta.core.permissions import Relation, can_read
from estafeta.indexing.providers import Embedder, ProviderError, embedding_key
from estafeta.models import KnowledgeStatus, Material, SpaceKind
from estafeta.models.indexing import Chunk


class Scope(StrEnum):
    PERSONAL = "personal"
    POSITION = "position"
    DEPARTMENT = "department"


RELATIONS = {
    Scope.PERSONAL: (Relation.OWNER,),
    Scope.POSITION: (Relation.POSITION,),
    Scope.DEPARTMENT: (Relation.POSITION, Relation.DEPARTMENT),
}


@dataclass(frozen=True)
class Hit:
    id: UUID
    material_id: UUID
    text: str


def material_scope(scope: Scope) -> tuple[sa.ColumnElement[bool], ...]:
    return (
        sa.func.knowledge.access_relation(Material.space_id).in_(RELATIONS[scope]),
        Material.status.in_((KnowledgeStatus.DRAFT, KnowledgeStatus.IN_REVIEW))
        if scope is Scope.PERSONAL
        else Material.status == KnowledgeStatus.PUBLISHED,
        Material.space_kind
        == (SpaceKind.PERSONAL if scope is Scope.PERSONAL else SpaceKind.POSITION),
    )


def rrf(*rankings: list[UUID], limit: int) -> list[UUID]:
    scores: dict[UUID, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item_id in enumerate(dict.fromkeys(ranking), 1):
            scores[item_id] += 1 / (60 + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))[:limit]


def query_vector(embedder: Embedder, query: str) -> list[float] | None:
    try:
        vectors = embedder.embed([query])
    except ProviderError:
        return None
    if len(vectors) != 1 or len(vectors[0]) != 1024:
        return None
    vector = vectors[0]
    return vector if any(vector) and all(map(math.isfinite, vector)) else None


def authorized_hits(db: Session, ids: list[UUID], scope: Scope) -> dict[UUID, Hit]:
    if not ids:
        return {}
    relation = sa.func.knowledge.access_relation(Material.space_id)
    stmt = (
        sa.select(
            Chunk.id, Chunk.material_id, Chunk.text, Material.space_kind, Material.status, relation
        )
        .select_from(Chunk)
        .join(Material, Material.id == Chunk.material_id)
        .where(Chunk.active, Chunk.id.in_(ids), *material_scope(scope))
    )
    return {
        chunk_id: Hit(chunk_id, material_id, text)
        for chunk_id, material_id, text, kind, status, key in db.execute(stmt)
        if can_read(Relation(key), kind, status)
    }


def search(
    db: Session, query: str, scope: Scope, vector: list[float] | None, settings: Settings
) -> list[Hit]:
    if vector is not None:
        db.execute(sa.text("SET LOCAL hnsw.iterative_scan = strict_order"))
    candidates = sa.text("""
        SELECT lexical, semantic
        FROM knowledge.rank_chunk_ids(:query, :scope, CAST(:vector AS public.vector),
                                      :model_key, :limit)
    """).bindparams(sa.bindparam("vector", type_=Vector(1024)))
    lexical, semantic = db.execute(candidates, {
        "query": query,
        "scope": scope.value,
        "vector": vector,
        "model_key": embedding_key(settings.embedding_model, settings.embedding_revision),
        "limit": max(32, settings.rag_top_k * 4),
    }).one()
    ranked = rrf(lexical, semantic, limit=settings.rag_top_k)
    allowed = authorized_hits(db, ranked, scope)
    return [allowed[item] for item in ranked if item in allowed]
