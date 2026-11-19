"""Очередь сборки, чанки, полнотекстовый и векторный индексы.

Revision ID: 0007_build_index
Revises: 0006_spaces_and_publication
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_build_index"
down_revision: str | None = "0006_spaces_and_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REDACTION_TYPES = (
    "'password', 'token', 'api_key', 'private_key', 'db_connection', "
    "'credit_card', 'passport', 'snils', 'inn', 'phone', 'external_email', "
    "'person', 'address', 'custom'"
)


def upgrade() -> None:
    op.add_column(
        "knowledge_records",
        sa.Column(
            "source_material_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("material_versions.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "knowledge_records",
        sa.Column(
            "record_origin",
            sa.String(length=16),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column(
        "knowledge_records",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "knowledge_records",
        sa.Column(
            "is_manually_edited",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("knowledge_records", sa.Column("confidence", sa.Float()))
    op.add_column(
        "knowledge_records",
        sa.Column("extraction_model", sa.String(length=200)),
    )
    op.add_column(
        "knowledge_records",
        sa.Column("extraction_prompt_version", sa.String(length=32)),
    )
    op.create_check_constraint(
        "ck_knowledge_records_origin",
        "knowledge_records",
        "record_origin IN ('manual', 'extracted', 'copied')",
    )
    op.create_check_constraint(
        "ck_knowledge_records_confidence",
        "knowledge_records",
        "confidence IS NULL OR confidence BETWEEN 0 AND 1",
    )
    op.create_index(
        "ix_knowledge_records_current_scope",
        "knowledge_records",
        ["scope", "status", "is_current"],
    )
    op.execute("DROP POLICY knowledge_records_read ON knowledge_records")
    op.execute(
        """
        CREATE POLICY knowledge_records_read ON knowledge_records
        FOR SELECT USING (
            is_current
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND (
                (
                    owner_id = public.app_current_user_id()
                    AND scope = 'personal'
                )
                OR (
                    owner_id = public.app_current_user_id()
                    AND scope = 'position'
                    AND position_id = public.app_current_position_id()
                )
                OR (
                    scope = 'position'
                    AND status = 'published'
                    AND position_id IS NOT NULL
                    AND (
                        position_id = public.app_current_position_id()
                        OR EXISTS (
                            SELECT 1 FROM public.positions p
                            WHERE p.id = knowledge_records.position_id
                              AND p.department_id = public.app_current_department_id()
                        )
                    )
                )
                OR (
                    scope = 'position'
                    AND status = 'in_review'
                    AND public.app_current_role() = 'department_manager'
                    AND EXISTS (
                        SELECT 1 FROM public.positions p
                        WHERE p.id = knowledge_records.position_id
                          AND p.department_id = public.app_current_department_id()
                    )
                )
            )
        )
        """
    )

    op.create_table(
        "build_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_started_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')",
            name="ck_build_jobs_status",
        ),
        sa.CheckConstraint(
            "stage IN ('queued', 'loading', 'chunking', 'embedding', 'extracting', "
            "'complete', 'failed')",
            name="ck_build_jobs_stage",
        ),
        sa.CheckConstraint("total_count >= 0", name="ck_build_jobs_total_nonnegative"),
        sa.CheckConstraint(
            "processed_count >= 0",
            name="ck_build_jobs_processed_nonnegative",
        ),
        sa.CheckConstraint("failed_count >= 0", name="ck_build_jobs_failed_nonnegative"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_build_jobs_attempt_nonnegative"),
    )
    op.create_index(
        "ix_build_jobs_owner_created",
        "build_jobs",
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_build_jobs_pending_created",
        "build_jobs",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.execute("ALTER TABLE build_jobs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY build_jobs_owner_read ON build_jobs
        FOR SELECT USING (
            requested_by = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
        )
        """
    )
    op.execute(
        """
        CREATE POLICY build_jobs_owner_insert ON build_jobs
        FOR INSERT WITH CHECK (
            requested_by = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND status = 'pending'
            AND stage = 'queued'
        )
        """
    )
    op.execute(
        """
        CREATE POLICY build_jobs_owner_update ON build_jobs
        FOR UPDATE
        USING (
            requested_by = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
        )
        WITH CHECK (
            requested_by = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
        )
        """
    )

    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "material_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("materials.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "material_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("material_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian'::regconfig, text)", persisted=True),
        ),
        sa.Column("embedding", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "material_version_id",
            "ordinal",
            name="uq_chunks_version_ordinal",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal_nonnegative"),
        sa.CheckConstraint("length(text) > 0", name="ck_chunks_text_not_empty"),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end > char_start",
            name="ck_chunks_source_offsets",
        ),
    )
    op.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(1024) USING NULL")
    op.create_index(
        "ix_chunks_version_current",
        "chunks",
        ["material_version_id", "is_current"],
    )
    op.create_index(
        "ix_chunks_current_material",
        "chunks",
        ["material_id", "is_current", "ordinal"],
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_search_vector_gin
        ON chunks USING gin (search_vector)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
        ON chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE is_current
        """
    )
    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY chunks_read ON chunks
        FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = chunks.material_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY chunks_insert_material_owner ON chunks
        FOR INSERT WITH CHECK (
            is_current
            AND EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = chunks.material_id
                  AND m.owner_id = public.app_current_user_id()
                  AND m.status <> 'archived'
            )
            AND EXISTS (
                SELECT 1 FROM public.material_versions v
                WHERE v.id = chunks.material_version_id
                  AND v.material_id = chunks.material_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY chunks_update_material_owner ON chunks
        FOR UPDATE
        USING (
            EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = chunks.material_id
                  AND m.owner_id = public.app_current_user_id()
                  AND m.status <> 'archived'
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = chunks.material_id
                  AND m.owner_id = public.app_current_user_id()
                  AND m.status <> 'archived'
            )
        )
        """
    )

    op.create_table(
        "record_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("record_id", "chunk_id", name="uq_record_sources_record_chunk"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_record_sources_confidence",
        ),
    )
    op.create_index("ix_record_sources_chunk", "record_sources", ["chunk_id"])
    op.execute("ALTER TABLE record_sources ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY record_sources_read ON record_sources
        FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM public.knowledge_records r
                JOIN public.chunks c ON c.id = record_sources.chunk_id
                WHERE r.id = record_sources.record_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY record_sources_insert_owner ON record_sources
        FOR INSERT WITH CHECK (
            EXISTS (
                SELECT 1 FROM public.knowledge_records r
                WHERE r.id = record_sources.record_id
                  AND r.owner_id = public.app_current_user_id()
                  AND r.record_origin = 'extracted'
            )
            AND EXISTS (
                SELECT 1
                FROM public.chunks c
                JOIN public.materials m ON m.id = c.material_id
                WHERE c.id = record_sources.chunk_id
                  AND m.owner_id = public.app_current_user_id()
            )
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION app_claim_build_job()
        RETURNS TABLE(job_id uuid, requested_by uuid)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            selected_job public.build_jobs%ROWTYPE;
        BEGIN
            UPDATE public.build_jobs
            SET status = CASE WHEN attempt_count >= 3 THEN 'failed' ELSE 'pending' END,
                stage = CASE WHEN attempt_count >= 3 THEN 'failed' ELSE 'queued' END,
                completed_at = CASE WHEN attempt_count >= 3 THEN now() ELSE NULL END,
                lease_started_at = NULL
            WHERE status = 'running'
              AND lease_started_at < now() - interval '60 minutes';

            UPDATE public.build_jobs j
            SET status = 'failed',
                stage = 'failed',
                completed_at = now(),
                lease_started_at = NULL
            WHERE j.status = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.users u
                  WHERE u.id = j.requested_by
                    AND u.is_active
                    AND u.role NOT IN ('admin', 'security_auditor')
              );

            SELECT * INTO selected_job
            FROM public.build_jobs
            WHERE status = 'pending'
              AND EXISTS (
                  SELECT 1
                  FROM public.users u
                  WHERE u.id = build_jobs.requested_by
                    AND u.is_active
                    AND u.role NOT IN ('admin', 'security_auditor')
              )
            ORDER BY created_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1;

            IF NOT FOUND THEN
                RETURN;
            END IF;

            UPDATE public.build_jobs
            SET status = 'running',
                stage = 'loading',
                attempt_count = attempt_count + 1,
                lease_started_at = now(),
                started_at = COALESCE(started_at, now()),
                updated_at = now()
            WHERE id = selected_job.id;

            RETURN QUERY SELECT selected_job.id, selected_job.requested_by;
        END
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION app_claim_build_job() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_claim_build_job() TO estafeta_app")

    op.execute("REVOKE DELETE ON TABLE build_jobs, chunks, record_sources FROM estafeta_app")
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_type")
    op.execute(
        """
        ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_type CHECK (event_type IN (
            'auth.login_succeeded', 'auth.login_failed', 'auth.logout',
            'auth.invitation_accepted', 'auth.totp_setup_started',
            'auth.totp_verification_failed', 'auth.totp_verified', 'auth.totp_enabled',
            'admin.user_created', 'admin.csv_import_completed',
            'admin.recovery_invitation_created', 'admin.account_unlocked',
            'admin.bootstrap_created', 'admin.sanitization_policy_updated',
            'material.created', 'material.redaction_recorded',
            'material.submitted', 'material.published', 'material.archived',
            'knowledge.space_viewed', 'knowledge.item_viewed',
            'record.created', 'record.updated', 'record.submitted', 'record.published',
            'record.archived', 'record.copied', 'record.redaction_recorded',
            'build_job.created', 'build_job.completed'
        ))
        """
    )
    op.execute(
        """
        ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_metadata_shape;
        ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_metadata_shape CHECK (
            CASE event_type
                WHEN 'admin.user_created' THEN
                    metadata = '{}'::jsonb
                    OR (
                        metadata ? 'target_role'
                        AND metadata - 'target_role' = '{}'::jsonb
                        AND metadata->>'target_role' IN (
                            'employee', 'department_manager', 'hr', 'admin',
                            'security_auditor'
                        )
                    )
                WHEN 'admin.csv_import_completed' THEN
                    metadata = '{}'::jsonb
                    OR (
                        CASE
                            WHEN NOT metadata ? 'imported_count' THEN false
                            WHEN metadata - 'imported_count' <> '{}'::jsonb THEN false
                            WHEN jsonb_typeof(metadata->'imported_count') <> 'number'
                                THEN false
                            WHEN metadata->>'imported_count' !~ '^[1-9][0-9]{0,2}$'
                                THEN false
                            ELSE (metadata->>'imported_count')::integer BETWEEN 1 AND 500
                        END
                    )
                WHEN 'material.redaction_recorded' THEN
                    metadata ? 'finding_type'
                    AND metadata ? 'redacted_count'
                    AND metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
                    AND metadata->>'finding_type' IN (
                        {redaction_types}
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                WHEN 'record.redaction_recorded' THEN
                    metadata ? 'finding_type'
                    AND metadata ? 'redacted_count'
                    AND metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
                    AND metadata->>'finding_type' IN (
                        {redaction_types}
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                ELSE metadata = '{}'::jsonb
            END
        )
        """.replace("{redaction_types}", _REDACTION_TYPES)
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION app_claim_build_job()")
    for policy in (
        "record_sources_insert_owner",
        "record_sources_read",
    ):
        op.execute(f"DROP POLICY {policy} ON record_sources")
    for policy in (
        "chunks_update_material_owner",
        "chunks_insert_material_owner",
        "chunks_read",
    ):
        op.execute(f"DROP POLICY {policy} ON chunks")
    for policy in (
        "build_jobs_owner_update",
        "build_jobs_owner_insert",
        "build_jobs_owner_read",
    ):
        op.execute(f"DROP POLICY {policy} ON build_jobs")
    op.execute("DROP POLICY knowledge_records_read ON knowledge_records")
    op.execute(
        """
        CREATE POLICY knowledge_records_read ON knowledge_records
        FOR SELECT USING (
            public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND (
                (
                    owner_id = public.app_current_user_id()
                    AND scope = 'personal'
                )
                OR (
                    owner_id = public.app_current_user_id()
                    AND scope = 'position'
                    AND position_id = public.app_current_position_id()
                )
                OR (
                    scope = 'position'
                    AND status = 'published'
                    AND position_id IS NOT NULL
                    AND (
                        position_id = public.app_current_position_id()
                        OR EXISTS (
                            SELECT 1 FROM public.positions p
                            WHERE p.id = knowledge_records.position_id
                              AND p.department_id = public.app_current_department_id()
                        )
                    )
                )
                OR (
                    scope = 'position'
                    AND status = 'in_review'
                    AND public.app_current_role() = 'department_manager'
                    AND EXISTS (
                        SELECT 1 FROM public.positions p
                        WHERE p.id = knowledge_records.position_id
                          AND p.department_id = public.app_current_department_id()
                    )
                )
            )
        )
        """
    )
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_type")
    op.execute(
        """
        ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_type CHECK (event_type IN (
            'auth.login_succeeded', 'auth.login_failed', 'auth.logout',
            'auth.invitation_accepted', 'auth.totp_setup_started',
            'auth.totp_verification_failed', 'auth.totp_verified', 'auth.totp_enabled',
            'admin.user_created', 'admin.csv_import_completed',
            'admin.recovery_invitation_created', 'admin.account_unlocked',
            'admin.bootstrap_created', 'admin.sanitization_policy_updated',
            'material.created', 'material.redaction_recorded',
            'material.submitted', 'material.published', 'material.archived',
            'knowledge.space_viewed', 'knowledge.item_viewed',
            'record.created', 'record.updated', 'record.submitted', 'record.published',
            'record.archived', 'record.copied', 'record.redaction_recorded'
        ))
        """
    )
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_metadata_shape")
    op.execute(
        """
        ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_metadata_shape CHECK (
            CASE event_type
                WHEN 'admin.user_created' THEN
                    metadata = '{}'::jsonb
                    OR (
                        metadata ? 'target_role'
                        AND metadata - 'target_role' = '{}'::jsonb
                        AND metadata->>'target_role' IN (
                            'employee', 'department_manager', 'hr', 'admin',
                            'security_auditor'
                        )
                    )
                WHEN 'admin.csv_import_completed' THEN
                    metadata = '{}'::jsonb
                    OR (
                        CASE
                            WHEN NOT metadata ? 'imported_count' THEN false
                            WHEN metadata - 'imported_count' <> '{}'::jsonb THEN false
                            WHEN jsonb_typeof(metadata->'imported_count') <> 'number'
                                THEN false
                            WHEN metadata->>'imported_count' !~ '^[1-9][0-9]{0,2}$'
                                THEN false
                            ELSE (metadata->>'imported_count')::integer BETWEEN 1 AND 500
                        END
                    )
                WHEN 'material.redaction_recorded' THEN
                    metadata ? 'finding_type'
                    AND metadata ? 'redacted_count'
                    AND metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
                    AND metadata->>'finding_type' IN (
                        {redaction_types}
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                WHEN 'record.redaction_recorded' THEN
                    metadata ? 'finding_type'
                    AND metadata ? 'redacted_count'
                    AND metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
                    AND metadata->>'finding_type' IN (
                        {redaction_types}
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                ELSE metadata = '{}'::jsonb
            END
        )
        """.replace("{redaction_types}", _REDACTION_TYPES)
    )

    op.drop_table("record_sources")
    op.execute("DROP INDEX ix_chunks_embedding_hnsw")
    op.execute("DROP INDEX ix_chunks_search_vector_gin")
    op.drop_table("chunks")
    op.drop_table("build_jobs")
    op.drop_index("ix_knowledge_records_current_scope", table_name="knowledge_records")
    op.drop_constraint("ck_knowledge_records_confidence", "knowledge_records", type_="check")
    op.drop_constraint("ck_knowledge_records_origin", "knowledge_records", type_="check")
    op.drop_column("knowledge_records", "extraction_prompt_version")
    op.drop_column("knowledge_records", "extraction_model")
    op.drop_column("knowledge_records", "confidence")
    op.drop_column("knowledge_records", "is_manually_edited")
    op.drop_column("knowledge_records", "is_current")
    op.drop_column("knowledge_records", "record_origin")
    op.drop_column("knowledge_records", "source_material_version_id")
