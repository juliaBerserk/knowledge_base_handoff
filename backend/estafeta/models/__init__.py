from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "knowledge"


class Base(DeclarativeBase):
    metadata = sa.MetaData(schema=SCHEMA)


class Role(StrEnum):
    EMPLOYEE = "employee"
    DEPARTMENT_HEAD = "department_head"
    HR = "hr"
    ADMIN = "admin"
    SECURITY_AUDITOR = "security_auditor"


class SpaceKind(StrEnum):
    PERSONAL = "personal"
    POSITION = "position"
    DEPARTMENT = "department"


class KnowledgeStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


def enum_type(enum: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Identified:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Department(Identified, Base):
    __tablename__ = "departments"
    __table_args__ = (sa.CheckConstraint("btrim(name) <> ''", name="ck_department_name"),)

    name: Mapped[str] = mapped_column(sa.String(200), unique=True)


class Position(Identified, Base):
    __tablename__ = "positions"
    __table_args__ = (sa.CheckConstraint("btrim(title) <> ''", name="ck_position_title"),)

    department_id: Mapped[UUID] = mapped_column(sa.ForeignKey("departments.id"), index=True)
    title: Mapped[str] = mapped_column(sa.String(200))


class Employee(Identified, Base):
    __tablename__ = "employees"
    __table_args__ = (
        sa.CheckConstraint("btrim(full_name) <> ''", name="ck_employee_full_name"),
        sa.CheckConstraint(
            "corporate_email IS NULL OR "
            "(corporate_email = lower(btrim(corporate_email)) AND corporate_email <> '')",
            name="ck_employee_normalized_email",
        ),
    )

    full_name: Mapped[str] = mapped_column(sa.String(255))
    corporate_email: Mapped[str | None] = mapped_column(sa.String(254), unique=True)
    role: Mapped[Role] = mapped_column(enum_type(Role, "ck_employee_role"))


class PositionAssignment(Identified, Base):
    __tablename__ = "position_assignments"
    __table_args__ = (
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at", name="ck_assignment_interval"
        ),
        sa.Index(
            "uq_assignment_open_employee",
            "employee_id",
            unique=True,
            postgresql_where=sa.text("ended_at IS NULL"),
        ),
    )

    employee_id: Mapped[UUID] = mapped_column(sa.ForeignKey("employees.id"), index=True)
    position_id: Mapped[UUID] = mapped_column(sa.ForeignKey("positions.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class KnowledgeSpace(Identified, Base):
    __tablename__ = "spaces"
    __table_args__ = (
        sa.UniqueConstraint("id", "kind", name="uq_space_id_kind"),
        sa.CheckConstraint(
            "(review_department_id IS NULL AND review_until IS NULL) OR "
            "(kind = 'personal' AND review_department_id IS NOT NULL AND review_until IS NOT NULL)",
            name="ck_space_review_binding",
        ),
        sa.CheckConstraint(
            "(kind = 'personal' AND employee_id IS NOT NULL AND position_id IS NULL "
            "AND department_id IS NULL) OR "
            "(kind = 'position' AND employee_id IS NULL AND position_id IS NOT NULL "
            "AND department_id IS NULL) OR "
            "(kind = 'department' AND employee_id IS NULL AND position_id IS NULL "
            "AND department_id IS NOT NULL)",
            name="ck_space_binding",
        ),
    )

    kind: Mapped[SpaceKind] = mapped_column(enum_type(SpaceKind, "ck_space_kind"))
    employee_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("employees.id"), unique=True)
    position_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("positions.id"), unique=True)
    department_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("departments.id"), unique=True)
    review_department_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("departments.id", name="fk_space_review_department")
    )
    review_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class KnowledgeItem(Identified):
    space_id: Mapped[UUID] = mapped_column()
    space_kind: Mapped[SpaceKind]
    author_id: Mapped[UUID] = mapped_column(sa.ForeignKey("employees.id"), index=True)
    status: Mapped[KnowledgeStatus]

    @classmethod
    def constraints(cls, name: str) -> tuple[sa.Constraint | sa.Index, ...]:
        return (
            sa.ForeignKeyConstraint(
                ["space_id", "space_kind"],
                ["spaces.id", "spaces.kind"],
                name=f"fk_{name}_space_binding",
            ),
            sa.CheckConstraint(
                "(space_kind = 'personal' AND status IN ('draft', 'in_review', 'archived')) "
                "OR (space_kind = 'position' AND status IN ('published', 'archived'))",
                name=f"ck_{name}_space_status",
            ),
            sa.Index(f"ix_{name}_space_status", "space_id", "status"),
        )


class Material(KnowledgeItem, Base):
    __tablename__ = "materials"
    __table_args__ = KnowledgeItem.constraints("material")

    space_kind: Mapped[SpaceKind] = mapped_column(enum_type(SpaceKind, "ck_material_space_kind"))
    status: Mapped[KnowledgeStatus] = mapped_column(
        enum_type(KnowledgeStatus, "ck_material_status")
    )


class KnowledgeRecord(KnowledgeItem, Base):
    __tablename__ = "records"
    __table_args__ = KnowledgeItem.constraints("record")

    space_kind: Mapped[SpaceKind] = mapped_column(enum_type(SpaceKind, "ck_record_space_kind"))
    status: Mapped[KnowledgeStatus] = mapped_column(enum_type(KnowledgeStatus, "ck_record_status"))
