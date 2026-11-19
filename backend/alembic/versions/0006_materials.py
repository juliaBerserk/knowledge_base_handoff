"""Sanitized material drafts and redaction audit."""

import sqlalchemy as sa
from alembic import op

revision = "0006_materials"
down_revision = "0005_audit"
branch_labels = None
depends_on = None

EVENT_ACTIONS = (
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
)


def replace_action_check(actions: tuple[str, ...]) -> None:
    op.drop_constraint("ck_event_action", "events", schema="audit", type_="check")
    values = ", ".join(f"'{action}'" for action in actions)
    op.create_check_constraint("ck_event_action", "events", f"action IN ({values})", schema="audit")


def upgrade() -> None:
    op.add_column("materials", sa.Column("cleaned_text", sa.Text()), schema="knowledge")
    op.add_column(
        "materials",
        sa.Column(
            "source_kind",
            sa.Enum(
                "note",
                "txt",
                "markdown",
                "docx",
                "pdf",
                name="ck_material_source_kind",
                native_enum=False,
                create_constraint=True,
            ),
        ),
        schema="knowledge",
    )
    op.create_check_constraint(
        "ck_material_source_pair",
        "materials",
        "(cleaned_text IS NULL) = (source_kind IS NULL)",
        schema="knowledge",
    )
    op.execute("""
        CREATE FUNCTION knowledge.current_uploader() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT e.id FROM knowledge.employees e
            JOIN auth.credentials c ON c.employee_id = e.id
            WHERE e.id = NULLIF(current_setting('app.user_id', true), '')::uuid
                AND e.role IN ('employee', 'department_head')
                AND c.blocked_at IS NULL AND c.password_hash IS NOT NULL
        $$
    """)
    op.execute("""
        CREATE FUNCTION knowledge.directory_contacts()
        RETURNS TABLE (full_name varchar, corporate_email varchar)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
            SELECT e.full_name, e.corporate_email FROM knowledge.employees e
            WHERE knowledge.current_uploader() IS NOT NULL
        $$
    """)
    for function in ("current_uploader", "directory_contacts"):
        op.execute(f"REVOKE ALL ON FUNCTION knowledge.{function}() FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION knowledge.{function}() TO estafeta_app")
    op.execute("""
        ALTER POLICY read_space ON knowledge.spaces USING (
            (kind = 'personal' AND employee_id = (SELECT knowledge.current_uploader()))
            OR knowledge.access_relation(id) IN ('owner', 'handoff', 'position', 'department')
        )
    """)
    op.execute("GRANT INSERT (id, kind, employee_id) ON knowledge.spaces TO estafeta_app")
    op.execute("""
        GRANT INSERT (id, space_id, space_kind, author_id, status, cleaned_text, source_kind)
        ON knowledge.materials TO estafeta_app
    """)
    op.execute("""
        CREATE POLICY create_personal_space ON knowledge.spaces FOR INSERT TO estafeta_app
        WITH CHECK (kind = 'personal' AND employee_id = (SELECT knowledge.current_uploader()))
    """)
    op.execute("""
        CREATE POLICY create_personal_draft ON knowledge.materials FOR INSERT TO estafeta_app
        WITH CHECK (
            space_kind = 'personal' AND status = 'draft' AND cleaned_text IS NOT NULL
            AND length(cleaned_text) BETWEEN 1 AND 300000
            AND author_id = (SELECT knowledge.current_uploader())
            AND knowledge.access_relation(space_id) = 'owner'
        )
    """)
    op.add_column(
        "events",
        sa.Column(
            "redaction_kind",
            sa.Enum(
                "password",
                "token",
                "api_key",
                "private_key",
                "connection",
                "card",
                "passport",
                "snils",
                "inn",
                "phone",
                "address",
                "email",
                "person",
                "manual",
                "confidential",
                name="ck_event_redaction_kind",
                native_enum=False,
                create_constraint=True,
            ),
        ),
        schema="audit",
    )
    op.add_column("events", sa.Column("redaction_count", sa.Integer()), schema="audit")
    replace_action_check((*EVENT_ACTIONS, "material_redacted"))
    op.create_check_constraint(
        "ck_event_redaction_metadata",
        "events",
        "(action = 'material_redacted' AND redaction_kind IS NOT NULL "
        "AND redaction_count IS NOT NULL AND redaction_count > 0) OR "
        "(action <> 'material_redacted' AND redaction_kind IS NULL "
        "AND redaction_count IS NULL)",
        schema="audit",
    )
    op.execute("GRANT INSERT (redaction_kind, redaction_count) ON audit.events TO estafeta_app")


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            LOCK TABLE knowledge.materials, audit.events IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM knowledge.materials WHERE cleaned_text IS NOT NULL)
                OR EXISTS (SELECT 1 FROM audit.events WHERE action = 'material_redacted') THEN
                RAISE EXCEPTION 'Downgrade would discard sanitized materials or audit metadata';
            END IF;
        END
        $$
    """)
    op.execute("REVOKE INSERT (redaction_kind, redaction_count) ON audit.events FROM estafeta_app")
    op.drop_constraint("ck_event_redaction_metadata", "events", schema="audit", type_="check")
    replace_action_check(EVENT_ACTIONS)
    op.drop_column("events", "redaction_count", schema="audit")
    op.drop_column("events", "redaction_kind", schema="audit")
    op.execute("DROP POLICY create_personal_draft ON knowledge.materials")
    op.execute("DROP POLICY create_personal_space ON knowledge.spaces")
    op.execute("""
        ALTER POLICY read_space ON knowledge.spaces USING (
            knowledge.access_relation(id) IN ('owner', 'handoff', 'position', 'department')
        )
    """)
    op.execute("""
        REVOKE INSERT (id, space_id, space_kind, author_id, status, cleaned_text, source_kind)
        ON knowledge.materials FROM estafeta_app
    """)
    op.execute("REVOKE INSERT (id, kind, employee_id) ON knowledge.spaces FROM estafeta_app")
    op.execute("DROP FUNCTION knowledge.directory_contacts()")
    op.execute("DROP FUNCTION knowledge.current_uploader()")
    op.drop_constraint("ck_material_source_pair", "materials", schema="knowledge", type_="check")
    op.drop_column("materials", "source_kind", schema="knowledge")
    op.drop_column("materials", "cleaned_text", schema="knowledge")
