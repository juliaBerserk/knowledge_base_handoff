"""Authentication tables.

Revision ID: 0003_auth
Revises: 0002_org_model
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_auth"
down_revision = "0002_org_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSchema("auth"))
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.LargeBinary(), nullable=False),
        sa.Column("window", sa.BigInteger(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        schema="auth",
    )
    op.create_index(
        op.f("ix_auth_rate_limits_window"), "rate_limits", ["window"], unique=False, schema="auth"
    )
    op.create_table(
        "credentials",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("totp_secret", sa.LargeBinary(), nullable=True),
        sa.Column("totp_last_step", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["knowledge.employees.id"],
        ),
        sa.PrimaryKeyConstraint("employee_id"),
        schema="auth",
    )
    op.create_table(
        "invitations",
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["auth.credentials.employee_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token_hash"),
        sa.UniqueConstraint("employee_id"),
        schema="auth",
    )
    op.create_index(
        op.f("ix_auth_invitations_expires_at"),
        "invitations",
        ["expires_at"],
        unique=False,
        schema="auth",
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enrollment_only", sa.Boolean(), nullable=False),
        sa.Column("pending_totp", sa.LargeBinary(), nullable=True),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["auth.credentials.employee_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token_hash"),
        schema="auth",
    )
    op.create_index(
        op.f("ix_auth_sessions_employee_id"),
        "sessions",
        ["employee_id"],
        unique=False,
        schema="auth",
    )

    op.execute("GRANT USAGE ON SCHEMA auth, knowledge TO estafeta_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO estafeta_app")
    op.execute("GRANT SELECT (id, corporate_email, role) ON knowledge.employees TO estafeta_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT (id, corporate_email, role) ON knowledge.employees FROM estafeta_app")
    op.execute("REVOKE USAGE ON SCHEMA knowledge FROM estafeta_app")
    op.drop_table("sessions", schema="auth")
    op.drop_table("invitations", schema="auth")
    op.drop_table("credentials", schema="auth")
    op.drop_table("rate_limits", schema="auth")
    op.execute(sa.schema.DropSchema("auth"))
