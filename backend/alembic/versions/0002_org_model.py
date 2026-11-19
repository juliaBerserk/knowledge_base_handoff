"""Создать оргмодель и метаданные знаний.

Revision ID: 0002_org_model
Revises: 0001_baseline
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_org_model"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Схема изолирована от выдачи прав по умолчанию в public.
    op.execute(sa.schema.CreateSchema("knowledge"))
    op.create_table(
        "departments",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_department_name"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        schema="knowledge",
    )
    op.create_table(
        "employees",
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("corporate_email", sa.String(length=254), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "employee",
                "department_head",
                "hr",
                "admin",
                "security_auditor",
                name="ck_employee_role",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(full_name) <> ''", name="ck_employee_full_name"),
        sa.CheckConstraint(
            "corporate_email IS NULL OR "
            "(corporate_email = lower(btrim(corporate_email)) AND corporate_email <> '')",
            name="ck_employee_normalized_email",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("corporate_email"),
        schema="knowledge",
    )
    op.create_table(
        "positions",
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_position_title"),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["knowledge.departments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="knowledge",
    )
    op.create_index(
        op.f("ix_knowledge_positions_department_id"),
        "positions",
        ["department_id"],
        unique=False,
        schema="knowledge",
    )
    op.create_table(
        "position_assignments",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("position_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at", name="ck_assignment_interval"
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["knowledge.employees.id"],
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["knowledge.positions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="knowledge",
    )
    op.create_index(
        op.f("ix_knowledge_position_assignments_employee_id"),
        "position_assignments",
        ["employee_id"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        op.f("ix_knowledge_position_assignments_position_id"),
        "position_assignments",
        ["position_id"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "uq_assignment_open_employee",
        "position_assignments",
        ["employee_id"],
        unique=True,
        schema="knowledge",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "spaces",
        sa.Column(
            "kind",
            sa.Enum(
                "personal",
                "position",
                "department",
                name="ck_space_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("position_id", sa.Uuid(), nullable=True),
        sa.Column("department_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'personal' AND employee_id IS NOT NULL AND position_id IS NULL "
            "AND department_id IS NULL) OR "
            "(kind = 'position' AND employee_id IS NULL AND position_id IS NOT NULL "
            "AND department_id IS NULL) OR "
            "(kind = 'department' AND employee_id IS NULL AND position_id IS NULL "
            "AND department_id IS NOT NULL)",
            name="ck_space_binding",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["knowledge.departments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["knowledge.employees.id"],
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["knowledge.positions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("department_id"),
        sa.UniqueConstraint("employee_id"),
        sa.UniqueConstraint("id", "kind", name="uq_space_id_kind"),
        sa.UniqueConstraint("position_id"),
        schema="knowledge",
    )
    op.create_table(
        "materials",
        sa.Column(
            "space_kind",
            sa.Enum(
                "personal",
                "position",
                "department",
                name="ck_material_space_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "in_review",
                "published",
                "archived",
                name="ck_material_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(space_kind = 'personal' AND status IN ('draft', 'in_review', 'archived')) "
            "OR (space_kind = 'position' AND status IN ('published', 'archived'))",
            name="ck_material_space_status",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["knowledge.employees.id"],
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "space_kind"],
            ["knowledge.spaces.id", "knowledge.spaces.kind"],
            name="fk_material_space_binding",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="knowledge",
    )
    op.create_index(
        op.f("ix_knowledge_materials_author_id"),
        "materials",
        ["author_id"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "ix_material_space_status",
        "materials",
        ["space_id", "status"],
        unique=False,
        schema="knowledge",
    )
    op.create_table(
        "records",
        sa.Column(
            "space_kind",
            sa.Enum(
                "personal",
                "position",
                "department",
                name="ck_record_space_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "in_review",
                "published",
                "archived",
                name="ck_record_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(space_kind = 'personal' AND status IN ('draft', 'in_review', 'archived')) "
            "OR (space_kind = 'position' AND status IN ('published', 'archived'))",
            name="ck_record_space_status",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["knowledge.employees.id"],
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "space_kind"],
            ["knowledge.spaces.id", "knowledge.spaces.kind"],
            name="fk_record_space_binding",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="knowledge",
    )
    op.create_index(
        op.f("ix_knowledge_records_author_id"),
        "records",
        ["author_id"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "ix_record_space_status",
        "records",
        ["space_id", "status"],
        unique=False,
        schema="knowledge",
    )


def downgrade() -> None:
    op.drop_table("records", schema="knowledge")
    op.drop_table("materials", schema="knowledge")
    op.drop_table("spaces", schema="knowledge")
    op.drop_table("position_assignments", schema="knowledge")
    op.drop_table("positions", schema="knowledge")
    op.drop_table("employees", schema="knowledge")
    op.drop_table("departments", schema="knowledge")
    op.execute(sa.schema.DropSchema("knowledge"))
