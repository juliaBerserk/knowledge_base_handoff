"""Optimize scoped retrieval while keeping RLS for chunk content."""

from alembic import op

revision = "0012_chunk_visibility"
down_revision = "0011_administration"
branch_labels = None
depends_on = None


RANK_VECTOR = """
CREATE FUNCTION knowledge.rank_vectors(
    query_vector public.vector, model_key text, allowed_hashes jsonb, wanted integer
) RETURNS uuid[]
LANGUAGE sql STABLE SET search_path = pg_catalog SET enable_sort = off SET jit = off AS $$
    SELECT ARRAY(
        SELECT c.id FROM knowledge.chunks c
        WHERE query_vector IS NOT NULL AND c.active AND c.embedding IS NOT NULL
          AND c.embedding_key = model_key
          AND c.source_hash = (allowed_hashes ->> c.material_id::text)
        ORDER BY c.embedding OPERATOR(public.<=>) query_vector
        LIMIT greatest(0, least(wanted, 80))
    )
$$
"""

RANK_CHUNKS = """
CREATE FUNCTION knowledge.rank_chunk_ids(
    question text, area text, query_vector public.vector, model_key text, wanted integer
) RETURNS TABLE (lexical uuid[], semantic uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    WITH allowed_spaces AS MATERIALIZED (
        SELECT s.id
        FROM knowledge.spaces s
        WHERE CASE area
            WHEN 'personal' THEN s.kind = 'personal'
                AND knowledge.access_relation(s.id) = 'owner'
            WHEN 'position' THEN s.kind = 'position'
                AND knowledge.access_relation(s.id) = 'position'
            WHEN 'department' THEN s.kind = 'position'
                AND knowledge.access_relation(s.id) IN ('position', 'department')
            ELSE false
        END
    ),
    allowed_materials AS MATERIALIZED (
        SELECT m.id, m.indexed_text_hash
        FROM knowledge.materials m JOIN allowed_spaces s ON s.id = m.space_id
        WHERE m.indexed_text_hash IS NOT NULL
          AND CASE WHEN area = 'personal' THEN m.status IN ('draft', 'in_review')
                   ELSE m.status = 'published' END
    ),
    allowed_hashes AS (
        SELECT jsonb_object_agg(id::text, indexed_text_hash) AS by_id FROM allowed_materials
    )
    SELECT
        ARRAY(
            SELECT c.id FROM knowledge.chunks c
            WHERE c.active
              AND c.search_vector @@ websearch_to_tsquery('russian', left(question, 1000))
              AND (c.material_id, c.source_hash) IN (SELECT * FROM allowed_materials)
            ORDER BY ts_rank_cd(
                c.search_vector, websearch_to_tsquery('russian', left(question, 1000))
            ) DESC, c.id
            LIMIT greatest(0, least(wanted, 80))
        ),
        knowledge.rank_vectors(
            query_vector, model_key, (SELECT by_id FROM allowed_hashes), wanted
        )
$$
"""

RANK_SIGNATURE = "knowledge.rank_chunk_ids(text,text,public.vector,text,integer)"
VECTOR_SIGNATURE = "knowledge.rank_vectors(public.vector,text,jsonb,integer)"


def upgrade() -> None:
    # Подзапрос по-прежнему выполняется под RLS материалов. Пара из идентификатора
    # и хэша сохраняет проверку актуальности и допускает хеширование выборки.
    op.execute("""
        ALTER POLICY visible_chunks ON knowledge.chunks USING (active AND
            (material_id, source_hash) IN (
                SELECT m.id, m.indexed_text_hash FROM knowledge.materials m
                WHERE m.status <> 'archived' AND m.indexed_text_hash IS NOT NULL
            )
        )
    """)
    # Владелец может использовать поисковые индексы. Результат ограничен
    # идентификаторами из текущей области доступа, текст читается через RLS.
    # Настройка планировщика относится только к векторной ветке и предпочитает
    # упорядоченный проход HNSW сортировке всех расстояний.
    op.execute(RANK_VECTOR)
    op.execute(f"REVOKE ALL ON FUNCTION {VECTOR_SIGNATURE} FROM PUBLIC")
    op.execute(RANK_CHUNKS)
    op.execute(f"REVOKE ALL ON FUNCTION {RANK_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {RANK_SIGNATURE} TO estafeta_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {RANK_SIGNATURE}")
    op.execute(f"DROP FUNCTION {VECTOR_SIGNATURE}")
    op.execute("""
        ALTER POLICY visible_chunks ON knowledge.chunks USING (active AND EXISTS (
            SELECT 1 FROM knowledge.materials m
            WHERE m.id = material_id AND m.status <> 'archived'
                AND source_hash = m.indexed_text_hash
        ))
    """)
