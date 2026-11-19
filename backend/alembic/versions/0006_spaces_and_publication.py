"""Пространства знаний, публикация и копирование записей.

Revision ID: 0006_spaces_and_publication
Revises: 0005_materials
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_spaces_and_publication"
down_revision: str | None = "0005_materials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPES = (
    "material.submitted",
    "material.published",
    "material.archived",
    "knowledge.space_viewed",
    "knowledge.item_viewed",
    "record.created",
    "record.updated",
    "record.submitted",
    "record.published",
    "record.archived",
    "record.copied",
    "record.redaction_recorded",
)


def upgrade() -> None:
    op.add_column(
        "materials",
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="RESTRICT"),
        ),
    )
    op.drop_constraint("ck_materials_stage5_personal_scope", "materials", type_="check")
    op.drop_constraint("ck_materials_stage5_draft_status", "materials", type_="check")
    op.create_check_constraint(
        "ck_materials_scope_status",
        "materials",
        "(scope = 'personal' AND position_id IS NULL AND status = 'draft') OR "
        "(scope = 'position' AND position_id IS NOT NULL AND "
        "status IN ('in_review', 'published', 'archived'))",
    )
    op.create_index("ix_materials_position_status", "materials", ["position_id", "status"])

    op.create_table(
        "knowledge_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "source_material_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("materials.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "copied_from_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_records.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "original_author_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="personal"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
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
            "(scope = 'personal' AND position_id IS NULL AND status = 'draft') OR "
            "(scope = 'position' AND position_id IS NOT NULL AND "
            "status IN ('in_review', 'published', 'archived'))",
            name="ck_knowledge_records_scope_status",
        ),
        sa.CheckConstraint(
            "category IN ('responsibilities', 'processes', 'cases', 'people', "
            "'systems', 'decisions', 'unfinished', 'faqs', 'glossary', 'risks')",
            name="ck_knowledge_records_category",
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_knowledge_records_title"),
        sa.CheckConstraint("length(content) > 0", name="ck_knowledge_records_content"),
    )
    op.create_index(
        "ix_knowledge_records_owner_updated",
        "knowledge_records",
        ["owner_id", "updated_at"],
    )
    op.create_index(
        "ix_knowledge_records_position_status",
        "knowledge_records",
        ["position_id", "status"],
    )

    op.execute("DROP POLICY materials_owner ON materials")
    op.execute(
        """
        CREATE POLICY materials_read ON materials
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
                            WHERE p.id = materials.position_id
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
                        WHERE p.id = materials.position_id
                          AND p.department_id = public.app_current_department_id()
                    )
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY materials_insert_owner ON materials
        FOR INSERT WITH CHECK (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND scope = 'personal'
            AND position_id IS NULL
            AND status = 'draft'
        )
        """
    )
    op.execute(
        """
        CREATE POLICY materials_update_owner ON materials
        FOR UPDATE
        USING (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND (
                (scope = 'personal' AND status = 'draft')
                OR (
                    scope = 'position'
                    AND position_id = public.app_current_position_id()
                )
            )
        )
        WITH CHECK (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND (
                (scope = 'personal' AND position_id IS NULL AND status = 'draft')
                OR (
                    scope = 'position'
                    AND position_id = public.app_current_position_id()
                    AND status IN ('in_review', 'published', 'archived')
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY materials_update_manager ON materials
        FOR UPDATE
        USING (
            public.app_current_role() = 'department_manager'
            AND scope = 'position'
            AND status IN ('in_review', 'published')
            AND EXISTS (
                SELECT 1 FROM public.positions p
                WHERE p.id = materials.position_id
                  AND p.department_id = public.app_current_department_id()
            )
        )
        WITH CHECK (
            public.app_current_role() = 'department_manager'
            AND scope = 'position'
            AND status IN ('published', 'archived')
            AND EXISTS (
                SELECT 1 FROM public.positions p
                WHERE p.id = materials.position_id
                  AND p.department_id = public.app_current_department_id()
            )
        )
        """
    )

    op.execute(
        """
        CREATE POLICY material_versions_shared_read ON material_versions
        FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = material_versions.material_id
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY redaction_events_shared_read ON redaction_events
        FOR SELECT USING (
            EXISTS (
                SELECT 1
                FROM public.material_versions v
                JOIN public.materials m ON m.id = v.material_id
                WHERE v.id = redaction_events.version_id
            )
        )
        """
    )

    op.execute("ALTER TABLE knowledge_records ENABLE ROW LEVEL SECURITY")
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
    op.execute(
        """
        CREATE POLICY knowledge_records_insert_owner ON knowledge_records
        FOR INSERT WITH CHECK (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND scope = 'personal'
            AND position_id IS NULL
            AND status = 'draft'
        )
        """
    )
    op.execute(
        """
        CREATE POLICY knowledge_records_update_owner ON knowledge_records
        FOR UPDATE
        USING (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND (
                (scope = 'personal' AND status = 'draft')
                OR (
                    scope = 'position'
                    AND position_id = public.app_current_position_id()
                )
            )
        )
        WITH CHECK (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND (
                (scope = 'personal' AND position_id IS NULL AND status = 'draft')
                OR (
                    scope = 'position'
                    AND position_id = public.app_current_position_id()
                    AND status IN ('in_review', 'published', 'archived')
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY knowledge_records_update_manager ON knowledge_records
        FOR UPDATE
        USING (
            public.app_current_role() = 'department_manager'
            AND scope = 'position'
            AND status IN ('in_review', 'published')
            AND EXISTS (
                SELECT 1 FROM public.positions p
                WHERE p.id = knowledge_records.position_id
                  AND p.department_id = public.app_current_department_id()
            )
        )
        WITH CHECK (
            public.app_current_role() = 'department_manager'
            AND scope = 'position'
            AND status IN ('published', 'archived')
            AND EXISTS (
                SELECT 1 FROM public.positions p
                WHERE p.id = knowledge_records.position_id
                  AND p.department_id = public.app_current_department_id()
            )
        )
        """
    )

    for table in ("materials", "material_versions", "redaction_events", "knowledge_records"):
        op.execute(f"REVOKE DELETE ON TABLE {table} FROM estafeta_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE knowledge_records TO estafeta_app")

    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_type")
    allowed_events = (
        "'auth.login_succeeded', 'auth.login_failed', 'auth.logout', "
        "'auth.invitation_accepted', 'auth.totp_setup_started', "
        "'auth.totp_verification_failed', 'auth.totp_verified', 'auth.totp_enabled', "
        "'admin.user_created', 'admin.csv_import_completed', "
        "'admin.recovery_invitation_created', 'admin.account_unlocked', "
        "'admin.bootstrap_created', 'admin.sanitization_policy_updated', "
        "'material.created', 'material.redaction_recorded', "
        + ", ".join(f"'{event}'" for event in _EVENT_TYPES)
    )
    op.execute(
        f"ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_type "
        f"CHECK (event_type IN ({allowed_events}))"
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
                        'password', 'token', 'api_key', 'private_key', 'db_connection',
                        'credit_card', 'passport', 'snils', 'inn', 'phone',
                        'external_email', 'person', 'address', 'custom'
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                WHEN 'record.redaction_recorded' THEN
                    metadata ? 'finding_type'
                    AND metadata ? 'redacted_count'
                    AND metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
                    AND metadata->>'finding_type' IN (
                        'password', 'token', 'api_key', 'private_key', 'db_connection',
                        'credit_card', 'passport', 'snils', 'inn', 'phone',
                        'external_email', 'person', 'address', 'custom'
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                ELSE metadata = '{}'::jsonb
            END
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_type"
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
                        'password', 'token', 'api_key', 'private_key', 'db_connection',
                        'credit_card', 'passport', 'snils', 'inn', 'phone',
                        'external_email', 'person', 'address', 'custom'
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                WHEN 'record.redaction_recorded' THEN
                    metadata ? 'finding_type'
                    AND metadata ? 'redacted_count'
                    AND metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
                    AND metadata->>'finding_type' IN (
                        'password', 'token', 'api_key', 'private_key', 'db_connection',
                        'credit_card', 'passport', 'snils', 'inn', 'phone',
                        'external_email', 'person', 'address', 'custom'
                    )
                    AND jsonb_typeof(metadata->'redacted_count') = 'number'
                    AND metadata->>'redacted_count' ~ '^[1-9][0-9]{0,5}$'
                    AND (metadata->>'redacted_count')::integer BETWEEN 1 AND 100000
                ELSE metadata = '{}'::jsonb
            END
        )
        """
    )
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
    for policy in (
        "knowledge_records_update_manager",
        "knowledge_records_update_owner",
        "knowledge_records_insert_owner",
        "knowledge_records_read",
    ):
        op.execute(f"DROP POLICY {policy} ON knowledge_records")
    for policy in (
        "materials_update_manager",
        "materials_update_owner",
        "materials_insert_owner",
        "materials_read",
    ):
        op.execute(f"DROP POLICY {policy} ON materials")
    op.execute("DROP POLICY material_versions_shared_read ON material_versions")
    op.execute("DROP POLICY redaction_events_shared_read ON redaction_events")

    op.execute(
        """
        CREATE POLICY materials_owner ON materials
        FOR ALL USING (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
        )
        WITH CHECK (
            owner_id = public.app_current_user_id()
            AND public.app_current_role() NOT IN ('admin', 'security_auditor')
            AND scope = 'personal'
            AND status = 'draft'
        )
        """
    )
    op.drop_table("knowledge_records")
    op.drop_index("ix_materials_position_status", table_name="materials")
    op.drop_constraint("ck_materials_scope_status", "materials", type_="check")
    op.create_check_constraint(
        "ck_materials_stage5_personal_scope",
        "materials",
        "scope = 'personal'",
    )
    op.create_check_constraint(
        "ck_materials_stage5_draft_status",
        "materials",
        "status = 'draft'",
    )
    op.drop_column("materials", "position_id")
