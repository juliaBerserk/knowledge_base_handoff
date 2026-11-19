"""Оргструктура, назначения и ограничения доступа.

Revision ID: 0002_org_access
Revises: 0001_baseline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_org_access"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False, unique=True),
        sa.Column(
            "publication_review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_departments_name_not_empty"),
    )
    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "department_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("department_id", "title", name="uq_positions_department_title"),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_positions_title_not_empty"),
    )
    op.create_index("ix_positions_department_id", "positions", ["department_id"])
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
            "role IN ('employee', 'department_manager', 'hr', 'admin', 'security_auditor')",
            name="ck_users_role",
        ),
        sa.CheckConstraint("length(trim(full_name)) > 0", name="ck_users_name_not_empty"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )
    op.create_table(
        "employment_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at",
            name="ck_assignments_end_after_start",
        ),
    )
    op.create_index("ix_employment_assignments_user_id", "employment_assignments", ["user_id"])
    op.create_index(
        "ix_employment_assignments_position_id",
        "employment_assignments",
        ["position_id"],
    )
    op.create_index(
        "uq_assignments_one_current_per_user",
        "employment_assignments",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("ends_at IS NULL"),
    )

    op.execute(
        """
        CREATE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT id FROM public.users
            WHERE id = NULLIF(current_setting('app.user_id', true), '')::uuid
              AND is_active
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_current_role() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT role FROM public.users
            WHERE id = public.app_current_user_id() AND is_active
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_current_department_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT p.department_id
            FROM public.employment_assignments ea
            JOIN public.positions p ON p.id = ea.position_id
            WHERE ea.user_id = public.app_current_user_id()
              AND ea.ends_at IS NULL
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_current_position_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT ea.position_id
            FROM public.employment_assignments ea
            WHERE ea.user_id = public.app_current_user_id()
              AND ea.ends_at IS NULL
        $function$
        """
    )
    for function_name in (
        "app_current_user_id",
        "app_current_role",
        "app_current_department_id",
        "app_current_position_id",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function_name}() FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function_name}() TO estafeta_app")

    op.execute("ALTER TABLE departments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE positions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE employment_assignments ENABLE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY departments_read ON departments
        FOR SELECT USING (public.app_current_role() IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE POLICY departments_admin_write ON departments
        FOR ALL USING (public.app_current_role() = 'admin')
        WITH CHECK (public.app_current_role() = 'admin')
        """
    )
    op.execute(
        """
        CREATE POLICY positions_read ON positions
        FOR SELECT USING (public.app_current_role() IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE POLICY positions_admin_write ON positions
        FOR ALL USING (public.app_current_role() = 'admin')
        WITH CHECK (public.app_current_role() = 'admin')
        """
    )
    op.execute(
        """
        CREATE POLICY users_read_scoped ON users
        FOR SELECT USING (
            id = public.app_current_user_id()
            OR public.app_current_role() IN ('admin', 'hr')
            OR (
                public.app_current_role() = 'department_manager'
                AND EXISTS (
                    SELECT 1
                    FROM public.employment_assignments ea
                    JOIN public.positions p ON p.id = ea.position_id
                    WHERE ea.user_id = users.id
                      AND ea.ends_at IS NULL
                      AND p.department_id = public.app_current_department_id()
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY users_admin_write ON users
        FOR ALL USING (public.app_current_role() = 'admin')
        WITH CHECK (public.app_current_role() = 'admin')
        """
    )
    op.execute(
        """
        CREATE POLICY assignments_read_scoped ON employment_assignments
        FOR SELECT USING (
            user_id = public.app_current_user_id()
            OR public.app_current_role() IN ('admin', 'hr')
            OR (
                public.app_current_role() = 'department_manager'
                AND EXISTS (
                    SELECT 1 FROM public.positions p
                    WHERE p.id = employment_assignments.position_id
                      AND p.department_id = public.app_current_department_id()
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY assignments_admin_write ON employment_assignments
        FOR ALL USING (public.app_current_role() = 'admin')
        WITH CHECK (public.app_current_role() = 'admin')
        """
    )


def downgrade() -> None:
    op.drop_table("employment_assignments")
    op.drop_table("users")
    op.drop_table("positions")
    op.drop_table("departments")
    for function_name in (
        "app_current_position_id",
        "app_current_department_id",
        "app_current_role",
        "app_current_user_id",
    ):
        op.execute(f"DROP FUNCTION {function_name}()")
