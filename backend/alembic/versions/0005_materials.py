"""Материалы, очищенные версии и настройки очистки.

Revision ID: 0005_materials
Revises: 0004_audit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_materials"
down_revision: str | None = "0004_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "materials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=300), nullable=False),
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
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_materials_title_not_empty"),
        sa.CheckConstraint("scope = 'personal'", name="ck_materials_stage5_personal_scope"),
        sa.CheckConstraint("status = 'draft'", name="ck_materials_stage5_draft_status"),
    )
    op.create_index("ix_materials_owner_updated", "materials", ["owner_id", "updated_at"])
    op.create_table(
        "material_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "material_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=8), nullable=False),
        sa.Column("cleaned_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("sanitizer_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("material_id", "version_number", name="uq_material_versions_number"),
        sa.CheckConstraint("version_number >= 1", name="ck_material_versions_number_positive"),
        sa.CheckConstraint(
            "source_kind IN ('note', 'txt', 'md', 'pdf', 'docx')",
            name="ck_material_versions_source_kind",
        ),
        sa.CheckConstraint("length(cleaned_text) > 0", name="ck_material_versions_text_not_empty"),
    )
    op.create_index(
        "ix_material_versions_material_created",
        "material_versions",
        ["material_id", "created_at"],
    )
    op.create_table(
        "redaction_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("material_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("finding_type", sa.String(length=32), nullable=False),
        sa.Column("redacted_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("version_id", "finding_type", name="uq_redaction_events_type"),
        sa.CheckConstraint("redacted_count > 0", name="ck_redaction_events_count_positive"),
    )
    op.create_table(
        "sanitization_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "custom_patterns",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "custom_terms",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "corporate_domains",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="ck_sanitization_policies_singleton"),
        sa.CheckConstraint(
            "jsonb_typeof(custom_patterns) = 'array' "
            "AND jsonb_typeof(custom_terms) = 'array' "
            "AND jsonb_typeof(corporate_domains) = 'array'",
            name="ck_sanitization_policies_json_arrays",
        ),
    )

    for table in (
        "materials",
        "material_versions",
        "redaction_events",
        "sanitization_policies",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

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
    op.execute(
        """
        CREATE POLICY material_versions_owner ON material_versions
        FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = material_versions.material_id
                  AND m.owner_id = public.app_current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY material_versions_owner_insert ON material_versions
        FOR INSERT WITH CHECK (
            created_by = public.app_current_user_id()
            AND EXISTS (
                SELECT 1 FROM public.materials m
                WHERE m.id = material_versions.material_id
                  AND m.owner_id = public.app_current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY redaction_events_owner_read ON redaction_events
        FOR SELECT USING (
            EXISTS (
                SELECT 1
                FROM public.material_versions v
                JOIN public.materials m ON m.id = v.material_id
                WHERE v.id = redaction_events.version_id
                  AND m.owner_id = public.app_current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY redaction_events_owner_insert ON redaction_events
        FOR INSERT WITH CHECK (
            EXISTS (
                SELECT 1
                FROM public.material_versions v
                JOIN public.materials m ON m.id = v.material_id
                WHERE v.id = redaction_events.version_id
                  AND m.owner_id = public.app_current_user_id()
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY sanitization_policies_read ON sanitization_policies
        FOR SELECT USING (public.app_current_role() IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE POLICY sanitization_policies_admin_write ON sanitization_policies
        FOR ALL USING (public.app_current_role() = 'admin')
        WITH CHECK (
            public.app_current_role() = 'admin'
            AND updated_by = public.app_current_user_id()
            AND id = 1
        )
        """
    )
    op.execute("REVOKE ALL ON TABLE sanitization_policies FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE sanitization_policies TO estafeta_app")

    op.execute(
        """
        ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_type;
        ALTER TABLE audit_events ADD CONSTRAINT ck_audit_events_type CHECK (event_type IN (
            'auth.login_succeeded', 'auth.login_failed', 'auth.logout',
            'auth.invitation_accepted', 'auth.totp_setup_started',
            'auth.totp_verification_failed', 'auth.totp_verified', 'auth.totp_enabled',
            'admin.user_created', 'admin.csv_import_completed',
            'admin.recovery_invitation_created', 'admin.account_unlocked',
            'admin.bootstrap_created', 'admin.sanitization_policy_updated',
            'material.created', 'material.redaction_recorded'
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
                        AND
                        metadata - 'target_role' = '{}'::jsonb
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
                    AND
                    metadata - ARRAY['finding_type', 'redacted_count'] = '{}'::jsonb
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
    op.execute("DROP POLICY sanitization_policies_admin_write ON sanitization_policies")
    op.execute("DROP POLICY sanitization_policies_read ON sanitization_policies")
    op.execute("DROP POLICY redaction_events_owner_insert ON redaction_events")
    op.execute("DROP POLICY redaction_events_owner_read ON redaction_events")
    op.execute("DROP POLICY material_versions_owner_insert ON material_versions")
    op.execute("DROP POLICY material_versions_owner ON material_versions")
    op.execute("DROP POLICY materials_owner ON materials")
    op.drop_table("sanitization_policies")
    op.drop_table("redaction_events")
    op.drop_table("material_versions")
    op.drop_table("materials")
