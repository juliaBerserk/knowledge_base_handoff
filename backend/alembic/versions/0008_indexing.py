"""Build queue, source chunks and extracted knowledge."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision = "0008_indexing"
down_revision = "0007_publication"
branch_labels = None
depends_on = None

SOURCE_WRITABLE = """EXISTS (
    SELECT 1 FROM knowledge.materials m
    WHERE m.id = material_id AND m.author_id = (SELECT knowledge.current_uploader())
        AND m.cleaned_text IS NOT NULL AND m.status IN ('draft', 'published')
)"""


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("knowledge.employees.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.String(12), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(16), server_default="queued", nullable=False),
        sa.Column("lease", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress", JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed')", name="ck_job_status"
        ),
        schema="knowledge",
    )
    for name, columns, unique, predicate in (
        ("uq_build_job_active_owner", ["owner_id"], True, "status IN ('queued','running')"),
        ("ix_build_job_queue", ["created_at", "id"], False, "status IN ('queued','running')"),
        ("ix_build_job_owner_history", ["owner_id", "created_at"], False, None),
    ):
        op.create_index(
            name,
            "jobs",
            columns,
            unique=unique,
            schema="knowledge",
            postgresql_where=sa.text(predicate) if predicate else None,
        )
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "material_id",
            sa.Uuid(),
            sa.ForeignKey("knowledge.materials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed("to_tsvector('russian'::regconfig, text)", persisted=True),
        ),
        schema="knowledge",
    )
    op.alter_column("chunks", "search_vector", nullable=False, schema="knowledge")
    op.create_index(
        "ix_chunks_material_active", "chunks", ["material_id", "active"], schema="knowledge"
    )
    op.create_index(
        "ix_chunks_fts",
        "chunks",
        ["search_vector"],
        schema="knowledge",
        postgresql_using="gin",
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "ix_chunks_embedding",
        "chunks",
        ["embedding"],
        schema="knowledge",
        postgresql_using="hnsw",
        postgresql_where=sa.text("active"),
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    for column in (
        sa.Column("source_chunk_id", sa.Uuid()),
        sa.Column("category", sa.String(24)),
        sa.Column("confidence", sa.Float()),
        sa.Column("manually_edited", sa.Boolean(), server_default=sa.false(), nullable=False),
    ):
        op.add_column("records", column, schema="knowledge")
    op.create_foreign_key(
        "fk_record_source_chunk",
        "records",
        "chunks",
        ["source_chunk_id"],
        ["id"],
        source_schema="knowledge",
        referent_schema="knowledge",
    )
    op.create_index(
        "ix_knowledge_records_source_chunk_id", "records", ["source_chunk_id"], schema="knowledge"
    )
    op.create_check_constraint(
        "ck_record_confidence", "records", "confidence BETWEEN 0 AND 1", schema="knowledge"
    )
    grant_policies()


def grant_policies() -> None:
    op.execute("ALTER TABLE knowledge.jobs ENABLE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, INSERT (id, owner_id) ON knowledge.jobs TO estafeta_app")
    op.execute("GRANT UPDATE ON knowledge.jobs TO estafeta_worker")
    op.execute("""
        CREATE POLICY own_jobs ON knowledge.jobs FOR SELECT TO estafeta_app
        USING (owner_id = (SELECT knowledge.current_uploader()))
    """)
    op.execute("""
        CREATE POLICY enqueue_job ON knowledge.jobs FOR INSERT TO estafeta_app
        WITH CHECK (owner_id = (SELECT knowledge.current_uploader()))
    """)
    op.execute("CREATE POLICY claim_jobs ON knowledge.jobs TO estafeta_worker USING (true)")
    op.execute("ALTER TABLE knowledge.chunks ENABLE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT ON knowledge.chunks TO estafeta_app")
    op.execute("GRANT INSERT, UPDATE (active) ON knowledge.chunks TO estafeta_worker")
    op.execute("""
        CREATE POLICY visible_chunks ON knowledge.chunks FOR SELECT TO estafeta_app
        USING (active AND EXISTS (
            SELECT 1 FROM knowledge.materials m
            WHERE m.id = material_id AND m.status <> 'archived'
                AND source_hash = encode(sha256(convert_to(m.cleaned_text, 'UTF8')), 'hex')
        ))
    """)
    op.execute("""
        CREATE FUNCTION knowledge.owns_retained_citation(chunk_id uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT EXISTS (
                SELECT 1 FROM knowledge.records r
                WHERE r.source_chunk_id = chunk_id AND r.manually_edited
                    AND r.author_id = knowledge.current_uploader()
                    AND knowledge.access_relation(r.space_id) IN ('owner','position','department')
            )
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION knowledge.owns_retained_citation(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION knowledge.owns_retained_citation(uuid) TO estafeta_app")
    op.execute("""
        CREATE POLICY retained_citations ON knowledge.chunks FOR SELECT TO estafeta_app
        USING (
            NOT active AND EXISTS (
                SELECT 1 FROM knowledge.materials m
                WHERE m.id = material_id AND m.status <> 'archived'
                    AND m.author_id = (SELECT knowledge.current_uploader())
            ) AND knowledge.owns_retained_citation(id)
        )
    """)
    op.execute(f"""
        CREATE POLICY worker_chunks ON knowledge.chunks TO estafeta_worker
        USING ({SOURCE_WRITABLE}) WITH CHECK ({SOURCE_WRITABLE})
    """)
    op.execute("""
        GRANT INSERT (id, space_id, space_kind, author_id, status, cleaned_text,
                      source_chunk_id, category, confidence)
        ON knowledge.records TO estafeta_worker
    """)
    draft = """
        space_kind = 'personal' AND author_id = (SELECT knowledge.current_uploader())
        AND knowledge.access_relation(space_id) = 'owner'
    """
    op.execute(f"""
        CREATE POLICY extracted_drafts ON knowledge.records FOR INSERT TO estafeta_worker
        WITH CHECK ({draft} AND status = 'draft' AND source_chunk_id IS NOT NULL
                    AND EXISTS (SELECT 1 FROM knowledge.chunks c WHERE c.id = source_chunk_id))
    """)
    op.execute("GRANT UPDATE (status, source_chunk_id) ON knowledge.records TO estafeta_worker")
    op.execute(f"""
        CREATE POLICY refresh_extracted_drafts ON knowledge.records FOR UPDATE TO estafeta_worker
        USING ({draft} AND status IN ('draft','archived') AND NOT manually_edited
               AND source_chunk_id IS NOT NULL)
        WITH CHECK ({draft} AND status IN ('draft','archived') AND NOT manually_edited
                    AND source_chunk_id IS NOT NULL)
    """)
    op.execute("GRANT UPDATE (cleaned_text, manually_edited) ON knowledge.records TO estafeta_app")
    op.execute(f"""
        CREATE POLICY edit_personal_records ON knowledge.records FOR UPDATE TO estafeta_app
        USING ({draft} AND status = 'draft')
        WITH CHECK ({draft} AND status = 'draft' AND manually_edited
                    AND length(cleaned_text) BETWEEN 1 AND 16000)
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE knowledge.jobs, knowledge.chunks, knowledge.records
                IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM knowledge.jobs)
                OR EXISTS (SELECT 1 FROM knowledge.chunks)
                OR EXISTS (SELECT 1 FROM knowledge.records WHERE manually_edited)
            THEN RAISE EXCEPTION 'Downgrade would discard build history or source references';
            END IF;
        END
        $$
    """)
    for policy in ("edit_personal_records", "refresh_extracted_drafts", "extracted_drafts"):
        op.execute(f"DROP POLICY {policy} ON knowledge.records")
    op.execute("""
        REVOKE UPDATE (cleaned_text, manually_edited) ON knowledge.records FROM estafeta_app;
        REVOKE UPDATE (status, source_chunk_id) ON knowledge.records FROM estafeta_worker;
        REVOKE INSERT (id, space_id, space_kind, author_id, status, cleaned_text,
                       source_chunk_id, category, confidence)
        ON knowledge.records FROM estafeta_worker
    """)
    op.drop_constraint("ck_record_confidence", "records", schema="knowledge", type_="check")
    op.drop_constraint("fk_record_source_chunk", "records", schema="knowledge", type_="foreignkey")
    for name in ("source_chunk_id", "category", "confidence", "manually_edited"):
        op.drop_column("records", name, schema="knowledge")
    op.drop_table("chunks", schema="knowledge")
    op.execute("DROP FUNCTION knowledge.owns_retained_citation(uuid)")
    op.drop_table("jobs", schema="knowledge")
