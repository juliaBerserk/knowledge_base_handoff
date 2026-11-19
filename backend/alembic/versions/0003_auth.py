"""Учётные данные, приглашения, сессии и второй фактор.

Revision ID: 0003_auth
Revises: 0002_org_access
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_auth"
down_revision: str | None = "0002_org_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "password_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False, unique=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "purpose IN ('initial', 'recovery')",
            name="ck_invitations_purpose",
        ),
    )
    op.create_index("ix_invitations_user_created", "invitations", ["user_id", "created_at"])
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("totp_verified_at", sa.DateTime(timezone=True)),
        sa.Column("totp_failed_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_auth_sessions_user_expires", "auth_sessions", ["user_id", "expires_at"])
    op.create_table(
        "totp_credentials",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("secret_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_used_step", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("enabled_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "login_rate_limits",
        sa.Column("key_hash", sa.LargeBinary(length=32), primary_key=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute(
        """
        CREATE FUNCTION app_auth_user_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT id FROM public.users
            WHERE email = lower(NULLIF(current_setting('app.login_email', true), ''))
              AND is_active
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_invited_user_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT user_id FROM public.invitations
            WHERE token_hash = decode(
                    NULLIF(current_setting('app.invitation_hash', true), ''), 'hex'
                  )
              AND consumed_at IS NULL
              AND revoked_at IS NULL
              AND expires_at > now()
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_session_user_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT user_id FROM public.auth_sessions
            WHERE token_hash = decode(
                    NULLIF(current_setting('app.session_hash', true), ''), 'hex'
                  )
              AND revoked_at IS NULL
              AND expires_at > now()
              AND last_seen_at > now() - make_interval(
                    mins => current_setting('app.session_idle_minutes', true)::integer
                  )
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_rate_limit_hash() RETURNS bytea
        LANGUAGE sql STABLE
        AS $function$
            SELECT decode(NULLIF(current_setting('app.rate_limit_hash', true), ''), 'hex')
        $function$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_admin_unlock(target_user_id uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            IF public.app_current_role() IS DISTINCT FROM 'admin' THEN
                RAISE EXCEPTION 'admin role required' USING ERRCODE = '42501';
            END IF;
            UPDATE public.credentials
            SET failed_attempts = 0, locked_at = NULL
            WHERE user_id = target_user_id;
            RETURN FOUND;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT id FROM public.users
            WHERE id = COALESCE(
                NULLIF(current_setting('app.user_id', true), '')::uuid,
                public.app_session_user_id(),
                public.app_auth_user_id(),
                public.app_invited_user_id()
            )
              AND is_active
        $function$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_role() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT role FROM public.users
            WHERE id = public.app_current_user_id() AND is_active
        $function$
        """
    )
    for function_name in (
        "app_auth_user_id",
        "app_invited_user_id",
        "app_session_user_id",
        "app_rate_limit_hash",
        "app_current_user_id",
        "app_current_role",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function_name}() FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function_name}() TO estafeta_app")
    op.execute("REVOKE ALL ON FUNCTION app_admin_unlock(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_unlock(uuid) TO estafeta_app")

    for table in (
        "credentials",
        "invitations",
        "auth_sessions",
        "totp_credentials",
        "login_rate_limits",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY users_auth_lookup ON users
        FOR SELECT USING (id = public.app_auth_user_id())
        """
    )
    op.execute(
        """
        CREATE POLICY credentials_owner ON credentials
        FOR ALL USING (
            user_id = public.app_current_user_id()
            OR user_id = public.app_auth_user_id()
            OR user_id = public.app_invited_user_id()
        )
        WITH CHECK (
            user_id = public.app_current_user_id()
            OR user_id = public.app_auth_user_id()
            OR user_id = public.app_invited_user_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY invitations_token_or_admin ON invitations
        FOR SELECT USING (
            token_hash = decode(
                NULLIF(current_setting('app.invitation_hash', true), ''), 'hex'
            )
            OR created_by = public.app_current_user_id()
            OR public.app_current_role() = 'admin'
        )
        """
    )
    op.execute(
        """
        CREATE POLICY invitations_consume_token ON invitations
        FOR UPDATE USING (
            token_hash = decode(
                NULLIF(current_setting('app.invitation_hash', true), ''), 'hex'
            )
        )
        WITH CHECK (
            token_hash = decode(
                NULLIF(current_setting('app.invitation_hash', true), ''), 'hex'
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY invitations_admin_write ON invitations
        FOR ALL USING (public.app_current_role() = 'admin')
        WITH CHECK (public.app_current_role() = 'admin')
        """
    )
    op.execute(
        """
        CREATE POLICY auth_sessions_token_or_owner ON auth_sessions
        FOR SELECT USING (
            token_hash = decode(
                NULLIF(current_setting('app.session_hash', true), ''), 'hex'
            )
            OR user_id = public.app_current_user_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY auth_sessions_owner_write ON auth_sessions
        FOR ALL USING (
            user_id = public.app_current_user_id()
            OR user_id = public.app_auth_user_id()
        )
        WITH CHECK (
            user_id = public.app_current_user_id()
            OR user_id = public.app_auth_user_id()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY totp_owner ON totp_credentials
        FOR ALL USING (user_id = public.app_current_user_id())
        WITH CHECK (user_id = public.app_current_user_id())
        """
    )
    op.execute(
        """
        CREATE POLICY login_rate_limit_key ON login_rate_limits
        FOR ALL USING (key_hash = public.app_rate_limit_hash())
        WITH CHECK (key_hash = public.app_rate_limit_hash())
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY users_auth_lookup ON users")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
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
        CREATE OR REPLACE FUNCTION app_current_role() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT role FROM public.users
            WHERE id = public.app_current_user_id() AND is_active
        $function$
        """
    )
    op.execute("DROP FUNCTION app_admin_unlock(uuid)")
    op.drop_table("login_rate_limits")
    op.drop_table("totp_credentials")
    op.drop_table("auth_sessions")
    op.drop_table("invitations")
    op.drop_table("credentials")
    for function_name in (
        "app_rate_limit_hash",
        "app_session_user_id",
        "app_invited_user_id",
        "app_auth_user_id",
    ):
        op.execute(f"DROP FUNCTION {function_name}()")
