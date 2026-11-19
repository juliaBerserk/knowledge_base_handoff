"""Append-only security audit."""

import sqlalchemy as sa
from alembic import op

revision = "0005_audit"
down_revision = "0004_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema("audit"))
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.Enum(
                "login",
                "logout",
                "password_set",
                "password_changed",
                "totp_enabled",
                "invitation_issued",
                "admin_bootstrapped",
                "knowledge_viewed",
                "material_uploaded",
                "knowledge_published",
                "knowledge_copied",
                "knowledge_exported",
                "ai_question",
                name="ck_event_action",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(
                "success",
                "denied",
                "throttled",
                "challenge",
                name="ck_event_outcome",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column(
            "target_kind",
            sa.Enum(
                "employee",
                "material",
                "record",
                "space",
                "chat",
                name="ck_event_target",
                native_enum=False,
                create_constraint=True,
            ),
        ),
        sa.Column("target_id", sa.Uuid()),
        sa.CheckConstraint(
            "(target_kind IS NULL) = (target_id IS NULL)", name="ck_event_target_pair"
        ),
        schema="audit",
    )
    op.create_index("ix_audit_events_action_id", "events", ["action", "id"], schema="audit")
    op.execute("""
        CREATE FUNCTION audit.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'Audit records are append-only' USING ERRCODE = '55000';
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION audit.reject_mutation() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER reject_audit_mutation BEFORE UPDATE OR DELETE OR TRUNCATE
        ON audit.events FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation()
    """)
    op.execute("""
        CREATE FUNCTION audit.can_read() RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT EXISTS (
                SELECT 1 FROM knowledge.employees e
                JOIN auth.credentials c ON c.employee_id = e.id
                WHERE e.id = NULLIF(current_setting('app.user_id', true), '')::uuid
                    AND e.role = 'security_auditor'
                    AND c.blocked_at IS NULL AND c.password_hash IS NOT NULL
            )
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION audit.can_read() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION audit.can_read() TO estafeta_app")
    op.execute("GRANT USAGE ON SCHEMA audit TO estafeta_app, estafeta_admin")
    op.execute("""
        GRANT INSERT (action, outcome, actor_id, target_kind, target_id)
        ON audit.events TO estafeta_app, estafeta_admin
    """)
    op.execute("GRANT SELECT ON audit.events TO estafeta_app")
    op.execute("ALTER TABLE audit.events ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY append_event ON audit.events FOR INSERT TO estafeta_app, estafeta_admin
        WITH CHECK (true)
    """)
    op.execute("""
        CREATE POLICY read_events ON audit.events FOR SELECT TO estafeta_app
        USING ((SELECT audit.can_read()))
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE audit.events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM audit.events) THEN
                RAISE EXCEPTION 'Downgrade would discard audit records';
            END IF;
        END
        $$
    """)
    op.drop_table("events", schema="audit")
    op.execute("DROP FUNCTION audit.can_read()")
    op.execute("DROP FUNCTION audit.reject_mutation()")
    op.execute(sa.schema.DropSchema("audit"))
