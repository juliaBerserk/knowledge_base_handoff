import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from estafeta.core.security import hash_password, token_digest, totp_code
from estafeta.domain.access import Role
from estafeta.models import (
    AuthSession,
    Base,
    Credential,
    Department,
    Invitation,
    InvitationPurpose,
    Position,
    TotpCredential,
    User,
)
from estafeta.services import auth


@pytest.fixture
def auth_session(monkeypatch, tmp_path):
    password_file = tmp_path / "common-passwords.txt"
    password_file.write_text("Password123456\n", encoding="utf-8")
    settings = SimpleNamespace(
        app_secret_key="test-secret-key-not-for-production-use",
        common_passwords_file=password_file,
        session_max_hours=12,
    )
    monkeypatch.setattr("estafeta.core.security.get_settings", lambda: settings)
    monkeypatch.setattr("estafeta.services.auth.get_settings", lambda: settings)
    monkeypatch.setattr("estafeta.services.auth.set_local_context", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.auth.set_local_user_id", lambda *_args: None)
    monkeypatch.setattr("estafeta.services.auth._utcnow", lambda: datetime.now())
    monkeypatch.setattr("estafeta.services.auth_provider.set_local_context", lambda *_args: None)
    monkeypatch.setattr(auth, "_check_ip_limit", lambda *_args: None)
    monkeypatch.setattr(auth, "_record_ip_failure", lambda *_args: None)
    monkeypatch.setattr(auth, "_clear_ip_failures", lambda *_args: None)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        department = Department(name="Тестовый отдел")
        session.add(department)
        session.flush()
        position = Position(department_id=department.id, title="Инженер")
        session.add(position)
        session.flush()
        user = User(
            full_name="Тестовый пользователь",
            email="user@example.test",
            role=Role.EMPLOYEE,
            is_active=True,
        )
        admin = User(
            full_name="Администратор",
            email="admin@example.test",
            role=Role.ADMIN,
            is_active=True,
        )
        session.add_all((user, admin))
        session.flush()
        yield session, user, admin, position
        session.rollback()
    engine.dispose()


def test_invitation_hash_expiry_replay_protection_and_password_setup(auth_session) -> None:
    session, user, _, _ = auth_session
    invitation_url = auth.create_invitation(
        session,
        user,
        InvitationPurpose.INITIAL,
        created_by=None,
    )
    token = parse_qs(urlparse(invitation_url).query)["token"][0]
    invitation = session.scalar(select(Invitation))
    assert invitation is not None
    assert invitation.token_hash == token_digest(token)
    assert token.encode() not in invitation.token_hash
    assert invitation.expires_at - invitation.created_at == timedelta(hours=72)

    auth.accept_invitation(session, token, "A unique strong password")
    credential = session.get(Credential, user.id)
    assert credential is not None
    assert credential.password_hash.startswith("$argon2id$")
    assert invitation.consumed_at is not None
    with pytest.raises(auth.InvalidInvitation):
        auth.accept_invitation(session, token, "Another unique strong password")


def test_new_invitation_revokes_previous_active_link(auth_session) -> None:
    session, user, admin, _ = auth_session
    old_url = auth.create_invitation(
        session,
        user,
        InvitationPurpose.INITIAL,
        created_by=admin.id,
    )
    new_url = auth.create_invitation(
        session,
        user,
        InvitationPurpose.INITIAL,
        created_by=admin.id,
    )
    old_token = parse_qs(urlparse(old_url).query)["token"][0]
    new_token = parse_qs(urlparse(new_url).query)["token"][0]
    invitations = list(session.scalars(select(Invitation).order_by(Invitation.created_at)))

    assert invitations[0].revoked_at is not None
    assert invitations[1].revoked_at is None
    with pytest.raises(auth.InvalidInvitation):
        auth.accept_invitation(session, old_token, "A unique strong password")
    auth.accept_invitation(session, new_token, "A unique strong password")


def test_expired_invitation_cannot_set_a_password(auth_session) -> None:
    session, user, _, _ = auth_session
    invitation_url = auth.create_invitation(
        session,
        user,
        InvitationPurpose.INITIAL,
        created_by=None,
    )
    token = parse_qs(urlparse(invitation_url).query)["token"][0]
    invitation = session.scalar(select(Invitation))
    assert invitation is not None
    invitation.expires_at = datetime.now() - timedelta(seconds=1)

    with pytest.raises(auth.InvalidInvitation):
        auth.accept_invitation(session, token, "A unique strong password")
    assert session.get(Credential, user.id) is None


def test_fifth_failed_password_locks_account_until_admin_unlock(auth_session) -> None:
    session, user, admin, _ = auth_session
    session.add(
        Credential(
            user_id=user.id,
            password_hash=hash_password("Correct horse battery staple"),
        )
    )
    for _ in range(5):
        with pytest.raises(auth.InvalidLogin):
            auth.authenticate(session, user.email, "Wrong password candidate", "127.0.0.1")

    credential = session.get(Credential, user.id)
    assert credential is not None
    assert credential.failed_attempts == 5
    assert credential.locked_at is not None
    with pytest.raises(auth.InvalidLogin):
        auth.authenticate(
            session,
            user.email,
            "Correct horse battery staple",
            "127.0.0.1",
        )

    credential.locked_at = None
    credential.failed_attempts = 0
    _, issued = auth.authenticate(
        session,
        user.email,
        "Correct horse battery staple",
        "127.0.0.1",
    )
    assert credential.locked_at is None
    assert issued.session_token
    assert issued.csrf_token
    assert issued.expires_at > datetime.now()
    auth_session_row = session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
    assert auth_session_row is not None
    assert auth_session_row.token_hash == token_digest(issued.session_token)


def test_admin_unlock_uses_narrow_security_definer_function(auth_session) -> None:
    _, user, admin, _ = auth_session
    executed: dict[str, object] = {}

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _UnlockSession:
        def add(self, event) -> None:
            executed["event"] = event

        def execute(self, statement, parameters):
            executed["sql"] = str(statement)
            executed["parameters"] = parameters
            return _Result()

    auth.unlock_user(_UnlockSession(), user.id, admin.id)

    assert "app_admin_unlock" in executed["sql"]
    assert executed["parameters"] == {"user_id": str(user.id)}
    assert executed["event"].event_type == "admin.account_unlocked"


def test_admin_login_requires_totp_enrollment(auth_session) -> None:
    session, _, admin, _ = auth_session
    session.add(
        Credential(
            user_id=admin.id,
            password_hash=hash_password("Administrator secure password"),
        )
    )

    _, issued = auth.authenticate(
        session,
        admin.email,
        "Administrator secure password",
        "127.0.0.1",
    )

    assert issued.totp_setup_required is True
    assert issued.totp_required is False


def test_recovery_invitation_resets_password_and_revokes_sessions(auth_session) -> None:
    session, user, _, _ = auth_session
    old_password_hash = hash_password("Original secure password")
    session.add(Credential(user_id=user.id, password_hash=old_password_hash))
    session.flush()
    active_session = AuthSession(
        user_id=user.id,
        token_hash=token_digest("session-token"),
        csrf_hash=token_digest("csrf-token"),
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    session.add(active_session)
    session.flush()
    invitation_url = auth.create_invitation(
        session,
        user,
        InvitationPurpose.RECOVERY,
        created_by=None,
    )
    token = parse_qs(urlparse(invitation_url).query)["token"][0]

    auth.accept_invitation(session, token, "Replacement secure password")

    credential = session.get(Credential, user.id)
    assert credential is not None
    assert credential.password_hash != old_password_hash
    assert active_session.revoked_at is not None


def test_totp_enrollment_encrypts_secret_and_rejects_replayed_codes(auth_session) -> None:
    session, user, _, _ = auth_session
    session_record = AuthSession(
        user_id=user.id,
        token_hash=token_digest("session-token"),
        csrf_hash=token_digest("csrf-token"),
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    session.add(session_record)
    session.flush()
    secret_text, uri = auth.start_totp_setup(session, user)
    secret = base64.b32decode(secret_text + "=" * ((8 - len(secret_text) % 8) % 8))
    credential = session.get(TotpCredential, user.id)
    assert credential is not None
    assert credential.secret_ciphertext != secret
    assert uri.startswith("otpauth://totp/")

    current_code = totp_code(secret)
    assert auth.verify_totp_for_session(session, user, session_record, current_code) is True
    assert credential.is_enabled is True
    assert session_record.totp_verified_at is not None
    assert auth.verify_totp_for_session(session, user, session_record, current_code) is False
    assert session_record.totp_failed_attempts == 1


def test_totp_session_is_revoked_after_five_invalid_codes(auth_session) -> None:
    session, user, _, _ = auth_session
    session_record = AuthSession(
        user_id=user.id,
        token_hash=token_digest("session-token"),
        csrf_hash=token_digest("csrf-token"),
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    session.add(session_record)
    session.flush()
    auth.start_totp_setup(session, user)

    for _ in range(5):
        assert auth.verify_totp_for_session(session, user, session_record, "000000") is False
    assert session_record.revoked_at is not None
