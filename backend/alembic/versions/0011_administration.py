"""Restricted account provisioning and retention settings."""

import sqlalchemy as sa
from alembic import op

revision = "0011_administration"
down_revision = "0010_handoffs"
branch_labels = None
depends_on = None

OLD_ACTIONS = (
    "login",
    "logout",
    "password_set",
    "password_changed",
    "totp_enabled",
    "invitation_issued",
    "admin_bootstrapped",
    "knowledge_viewed",
    "material_uploaded",
    "material_redacted",
    "knowledge_published",
    "knowledge_submitted",
    "knowledge_returned",
    "knowledge_archived",
    "publication_settings_changed",
    "knowledge_copied",
    "knowledge_exported",
    "ai_question",
    "handoff_created",
    "handoff_cancelled",
    "handoff_activated",
    "handoff_review_closed",
    "handoff_conflict",
    "employee_transferred",
)
NEW_ACTIONS = (
    "account_created",
    "accounts_imported",
    "department_created",
    "position_created",
    "admin_settings_changed",
)
OLD_TARGETS = ("employee", "material", "record", "space", "chat", "handoff")

AUTHENTICATE = """
CREATE FUNCTION auth.check_admin_session(digest bytea, idle_seconds integer, max_seconds integer)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    actor uuid;
    active_at timestamptz := clock_timestamp();
BEGIN
    IF idle_seconds <= 0 OR max_seconds <= 0 THEN RETURN NULL; END IF;
    SELECT c.employee_id INTO actor
    FROM auth.sessions s
    JOIN auth.credentials c ON c.employee_id = s.employee_id
    JOIN knowledge.employees e ON e.id = c.employee_id
    WHERE s.token_hash = digest AND e.role = 'admin'
        AND NOT s.enrollment_only AND c.totp_secret IS NOT NULL
        AND c.password_hash IS NOT NULL
        AND c.blocked_at IS NULL AND NOT auth.departure_due(c.employee_id)
        AND s.created_at > active_at - make_interval(secs => max_seconds)
        AND s.last_seen_at > active_at - make_interval(secs => idle_seconds)
    FOR UPDATE OF c;
    IF actor IS NULL THEN RETURN NULL; END IF;
    UPDATE auth.sessions s SET last_seen_at = GREATEST(s.last_seen_at, active_at)
        WHERE s.token_hash = digest AND s.employee_id = actor
            AND NOT s.enrollment_only
            AND s.created_at > active_at - make_interval(secs => max_seconds)
            AND s.last_seen_at > active_at - make_interval(secs => idle_seconds);
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN actor;
END
$$
"""

ISSUE = """
CREATE FUNCTION auth.issue_admin_invitation(member uuid, digest bytea)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF digest IS NULL OR octet_length(digest) <> 32 OR NOT EXISTS (
        SELECT 1 FROM knowledge.employees WHERE id = member
    ) THEN RETURN false; END IF;
    INSERT INTO auth.credentials (employee_id) VALUES (member) ON CONFLICT DO NOTHING;
    PERFORM 1 FROM auth.credentials WHERE employee_id = member
        AND blocked_at IS NULL AND NOT auth.departure_due(member) FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO auth.invitations (token_hash, employee_id, expires_at)
    VALUES (digest, member, clock_timestamp() + interval '72 hours')
    ON CONFLICT (employee_id) DO UPDATE
        SET token_hash = excluded.token_hash, expires_at = excluded.expires_at;
    RETURN true;
END
$$
"""

STATUS = """
CREATE FUNCTION auth.admin_account_status(member uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT CASE
        WHEN c.blocked_at IS NOT NULL OR auth.departure_due(member) THEN 'blocked'
        WHEN c.password_hash IS NOT NULL THEN 'active'
        WHEN i.expires_at > statement_timestamp() THEN 'invited'
        ELSE 'pending'
    END
    FROM knowledge.employees e LEFT JOIN auth.credentials c ON c.employee_id = e.id
    LEFT JOIN auth.invitations i ON i.employee_id = e.id
    WHERE e.id = member
$$
"""
FUNCTIONS = (
    (AUTHENTICATE, "auth.check_admin_session(bytea,integer,integer)"),
    (ISSUE, "auth.issue_admin_invitation(uuid,bytea)"),
    (STATUS, "auth.admin_account_status(uuid)"),
)


def audit_checks(actions: tuple[str, ...], targets: tuple[str, ...]) -> None:
    for field, column, values in (
        ("action", "action", actions),
        ("target", "target_kind", targets),
    ):
        op.drop_constraint(f"ck_event_{field}", "events", schema="audit", type_="check")
        allowed = ", ".join(f"'{value}'" for value in values)
        op.alter_column("events", column, type_=sa.String(max(map(len, values))), schema="audit")
        op.create_check_constraint(
            f"ck_event_{field}", "events", f"{column} IN ({allowed})", schema="audit"
        )


def upgrade() -> None:
    op.add_column(
        "publication_settings",
        sa.Column("handoff_retention", sa.String(8)),
        schema="knowledge",
    )
    op.create_check_constraint(
        "ck_publication_retention",
        "publication_settings",
        "handoff_retention IN ('archive','delete')",
        schema="knowledge",
    )
    op.execute("""
        GRANT UPDATE (handoff_retention) ON knowledge.publication_settings TO estafeta_admin
    """)
    op.execute("GRANT SELECT (full_name) ON knowledge.employees TO estafeta_admin")
    op.execute("""
        GRANT INSERT (id, full_name, corporate_email, role) ON knowledge.employees TO estafeta_admin
    """)
    op.execute(
        "GRANT SELECT, INSERT ON knowledge.departments, knowledge.positions TO estafeta_admin"
    )
    op.execute("GRANT SELECT, INSERT ON knowledge.position_assignments TO estafeta_admin")
    for statement, signature in FUNCTIONS:
        op.execute(statement)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO estafeta_admin")
    op.execute("REVOKE SELECT, INSERT, UPDATE (failures) ON auth.credentials FROM estafeta_admin")
    op.execute("REVOKE SELECT, INSERT, DELETE ON auth.invitations FROM estafeta_admin")
    op.execute("""
        REVOKE SELECT, DELETE, UPDATE (last_seen_at, enrollment_only)
        ON auth.sessions FROM estafeta_admin
    """)
    audit_checks((*OLD_ACTIONS, *NEW_ACTIONS), (*OLD_TARGETS, "department", "position"))


def downgrade() -> None:
    audit_checks(OLD_ACTIONS, OLD_TARGETS)
    op.execute("GRANT SELECT, INSERT, UPDATE (failures) ON auth.credentials TO estafeta_admin")
    op.execute("GRANT SELECT, INSERT, DELETE ON auth.invitations TO estafeta_admin")
    op.execute("""
        GRANT SELECT, DELETE, UPDATE (last_seen_at, enrollment_only)
        ON auth.sessions TO estafeta_admin
    """)
    for _, signature in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute("REVOKE SELECT, INSERT ON knowledge.position_assignments FROM estafeta_admin")
    op.execute("""
        REVOKE SELECT, INSERT ON knowledge.departments, knowledge.positions FROM estafeta_admin
    """)
    op.execute("""
        REVOKE SELECT (full_name), INSERT (id, full_name, corporate_email, role)
        ON knowledge.employees FROM estafeta_admin
    """)
    op.execute("""
        REVOKE UPDATE (handoff_retention) ON knowledge.publication_settings FROM estafeta_admin
    """)
    op.drop_constraint(
        "ck_publication_retention",
        "publication_settings",
        schema="knowledge",
        type_="check",
    )
    op.drop_column("publication_settings", "handoff_retention", schema="knowledge")
