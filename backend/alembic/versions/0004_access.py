"""Knowledge access and row-level security."""

import sqlalchemy as sa
from alembic import op

revision = "0004_access"
down_revision = "0003_auth"
branch_labels = None
depends_on = None

TABLES = ("spaces", "materials", "records")

ACCESS_RELATION = """
CREATE FUNCTION knowledge.access_relation(target_space uuid)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT COALESCE((
        SELECT CASE
            WHEN s.kind = 'personal' AND s.employee_id = actor.id THEN 'owner'
            WHEN s.kind = 'personal' AND actor.role = 'department_head'
                AND s.review_department_id = position.department_id
                AND s.review_until > statement_timestamp() THEN 'handoff'
            WHEN s.kind = 'personal' AND actor.role = 'department_head'
                AND position.department_id = (
                    SELECT p.department_id FROM knowledge.position_assignments a
                    JOIN knowledge.positions p ON p.id = a.position_id
                    WHERE a.employee_id = s.employee_id
                        AND a.started_at <= statement_timestamp()
                        AND (a.ended_at IS NULL OR a.ended_at > statement_timestamp())
                    ORDER BY a.started_at DESC, a.id
                    LIMIT 1
                ) THEN 'supervisor'
            WHEN s.kind = 'position' AND s.position_id = position.id THEN 'position'
            WHEN s.kind = 'position' AND EXISTS (
                SELECT 1 FROM knowledge.positions p
                WHERE p.id = s.position_id AND p.department_id = position.department_id
            ) THEN 'department'
            WHEN s.kind = 'department' AND s.department_id = position.department_id
                THEN 'department'
            ELSE 'none'
        END
        FROM knowledge.spaces s
        JOIN knowledge.employees actor
            ON actor.id = NULLIF(current_setting('app.user_id', true), '')::uuid
        JOIN auth.credentials credential ON credential.employee_id = actor.id
        LEFT JOIN LATERAL (
            SELECT a.position_id FROM knowledge.position_assignments a
            WHERE a.employee_id = actor.id
                AND a.started_at <= statement_timestamp()
                AND (a.ended_at IS NULL OR a.ended_at > statement_timestamp())
            ORDER BY a.started_at DESC, a.id
            LIMIT 1
        ) assignment ON true
        LEFT JOIN knowledge.positions position ON position.id = assignment.position_id
        WHERE s.id = target_space
            AND actor.role IN ('employee', 'department_head')
            AND credential.blocked_at IS NULL AND credential.password_hash IS NOT NULL
    ), 'none')
$$
"""


def upgrade() -> None:
    op.add_column("spaces", sa.Column("review_department_id", sa.Uuid()), schema="knowledge")
    op.add_column(
        "spaces", sa.Column("review_until", sa.DateTime(timezone=True)), schema="knowledge"
    )
    op.create_foreign_key(
        "fk_space_review_department",
        "spaces",
        "departments",
        ["review_department_id"],
        ["id"],
        source_schema="knowledge",
        referent_schema="knowledge",
    )
    op.create_check_constraint(
        "ck_space_review_binding",
        "spaces",
        "(review_department_id IS NULL AND review_until IS NULL) OR "
        "(kind = 'personal' AND review_department_id IS NOT NULL AND review_until IS NOT NULL)",
        schema="knowledge",
    )
    op.execute(ACCESS_RELATION)
    op.execute("REVOKE ALL ON FUNCTION knowledge.access_relation(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION knowledge.access_relation(uuid) TO estafeta_app")
    op.execute("GRANT USAGE ON SCHEMA auth, knowledge TO estafeta_admin")
    op.execute("GRANT SELECT (id, corporate_email, role) ON knowledge.employees TO estafeta_admin")
    op.execute("GRANT SELECT, INSERT, UPDATE (failures) ON auth.credentials TO estafeta_admin")
    op.execute("GRANT SELECT, INSERT, DELETE ON auth.invitations TO estafeta_admin")
    op.execute(
        "GRANT SELECT, DELETE, UPDATE (last_seen_at, enrollment_only) "
        "ON auth.sessions TO estafeta_admin"
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE knowledge.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT ON knowledge.{table} TO estafeta_app")
    op.execute("""
        CREATE POLICY read_space ON knowledge.spaces FOR SELECT TO estafeta_app
        USING (knowledge.access_relation(id) IN ('owner', 'handoff', 'position', 'department'))
    """)
    for table in ("materials", "records"):
        op.execute(f"""
            CREATE POLICY read_item ON knowledge.{table} FOR SELECT TO estafeta_app
            USING (CASE knowledge.access_relation(space_id)
                WHEN 'owner' THEN space_kind = 'personal'
                WHEN 'handoff' THEN space_kind = 'personal'
                WHEN 'supervisor' THEN space_kind = 'personal' AND status = 'in_review'
                WHEN 'position' THEN space_kind = 'position' AND status = 'published'
                WHEN 'department' THEN space_kind = 'position' AND status = 'published'
                ELSE false
            END)
        """)


def downgrade() -> None:
    for table in TABLES:
        policy = "read_space" if table == "spaces" else "read_item"
        op.execute(f"DROP POLICY {policy} ON knowledge.{table}")
        op.execute(f"ALTER TABLE knowledge.{table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT ON knowledge.{table} FROM estafeta_app")
    op.execute(
        "REVOKE SELECT (id, corporate_email, role) ON knowledge.employees FROM estafeta_admin"
    )
    op.execute("REVOKE SELECT, INSERT, UPDATE (failures) ON auth.credentials FROM estafeta_admin")
    op.execute("REVOKE SELECT, INSERT, DELETE ON auth.invitations FROM estafeta_admin")
    op.execute(
        "REVOKE SELECT, DELETE, UPDATE (last_seen_at, enrollment_only) "
        "ON auth.sessions FROM estafeta_admin"
    )
    op.execute("REVOKE USAGE ON SCHEMA auth, knowledge FROM estafeta_admin")
    op.execute("DROP FUNCTION knowledge.access_relation(uuid)")
    op.drop_constraint("ck_space_review_binding", "spaces", schema="knowledge", type_="check")
    op.drop_constraint(
        "fk_space_review_department", "spaces", schema="knowledge", type_="foreignkey"
    )
    op.drop_column("spaces", "review_until", schema="knowledge")
    op.drop_column("spaces", "review_department_id", schema="knowledge")
