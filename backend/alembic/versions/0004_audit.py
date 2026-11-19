"""Append-only журнал аудита.

Revision ID: 0004_audit
Revises: 0003_auth
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_audit"
down_revision: str | None = "0003_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_role", sa.String(length=32)),
        sa.Column("subject_type", sa.String(length=64)),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'auth.login_succeeded', 'auth.login_failed', 'auth.logout', "
            "'auth.invitation_accepted', 'auth.totp_setup_started', "
            "'auth.totp_verification_failed', 'auth.totp_verified', 'auth.totp_enabled', "
            "'admin.user_created', 'admin.csv_import_completed', "
            "'admin.recovery_invitation_created', 'admin.account_unlocked', "
            "'admin.bootstrap_created', 'admin.sanitization_policy_updated', "
            "'material.created', 'material.redaction_recorded'"
            ")",
            name="ck_audit_events_type",
        ),
        sa.CheckConstraint(
            "actor_role IS NULL OR actor_role IN "
            "('employee', 'department_manager', 'hr', 'admin', 'security_auditor')",
            name="ck_audit_events_actor_role",
        ),
        sa.CheckConstraint(
            "subject_type IS NULL OR subject_type IN "
            "('user', 'session', 'material', 'record', 'chunk', 'chat', 'answer', "
            "'handoff', 'job')",
            name="ck_audit_events_subject_type",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object' AND pg_column_size(metadata) <= 4096",
            name="ck_audit_events_metadata",
        ),
        sa.CheckConstraint(
            """
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
                ELSE metadata = '{}'::jsonb
            END
            """,
            name="ck_audit_events_metadata_shape",
        ),
    )
    op.create_index(
        "ix_audit_events_occurred_id",
        "audit_events",
        ["occurred_at", "id"],
    )
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_user_id", "id"])
    op.create_index("ix_audit_events_type_id", "audit_events", ["event_type", "id"])

    op.execute(
        """
        CREATE FUNCTION app_audit_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only'
                USING ERRCODE = '42501';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_row_mutation
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION app_audit_reject_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_truncate
        BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION app_audit_reject_mutation()
        """
    )
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_events_auditor_read ON audit_events
        FOR SELECT USING (public.app_current_role() = 'security_auditor')
        """
    )
    op.execute(
        """
        CREATE POLICY audit_events_actor_insert ON audit_events
        FOR INSERT WITH CHECK (
            (actor_user_id IS NULL AND actor_role IS NULL)
            OR (
                actor_user_id = public.app_current_user_id()
                AND actor_role = public.app_current_role()
            )
        )
        """
    )
    op.execute("REVOKE ALL ON TABLE audit_events FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON TABLE audit_events TO estafeta_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_events FROM estafeta_app")


def downgrade() -> None:
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION app_audit_reject_mutation()")
