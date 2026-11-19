from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from estafeta.core.config import get_settings
from estafeta.core.db import set_local_context, set_local_user_id
from estafeta.core.security import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    hash_password,
    keyed_digest,
    matching_totp_counter,
    new_token,
    new_totp_secret,
    normalize_email,
    token_digest,
    totp_code,
    totp_uri,
)
from estafeta.domain.access import Role
from estafeta.models import (
    AuthSession,
    Credential,
    Department,
    EmploymentAssignment,
    Invitation,
    InvitationPurpose,
    LoginRateLimit,
    Position,
    TotpCredential,
    User,
)
from estafeta.schemas.audit import AuditEventType, AuditMetadata
from estafeta.services.audit import record_audit_event
from estafeta.services.auth_provider import AuthProvider, LocalAuthProvider

ACCOUNT_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20
IP_WINDOW = timedelta(minutes=15)
INVITATION_TTL = timedelta(hours=72)


class AuthenticationError(Exception):
    pass


class InvalidInvitation(AuthenticationError):
    pass


class InvalidLogin(AuthenticationError):
    pass


class RateLimitExceeded(AuthenticationError):
    pass


class TotpRequired(AuthenticationError):
    pass


class TotpInvalid(AuthenticationError):
    pass


class DuplicateUser(AuthenticationError):
    pass


class InvalidOrganizationReference(AuthenticationError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_token: str
    csrf_token: str
    expires_at: datetime
    totp_required: bool
    totp_setup_required: bool


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _rate_hash(ip_address: str) -> bytes:
    return keyed_digest(ip_address, "login-ip")


def _check_ip_limit(session: Session, ip_address: str, now: datetime) -> None:
    digest = _rate_hash(ip_address)
    set_local_context(session, "app.rate_limit_hash", digest.hex())
    record = session.get(LoginRateLimit, digest, with_for_update=True)
    if record is None:
        return
    if record.blocked_until is not None and record.blocked_until > now:
        raise RateLimitExceeded
    if record.window_started_at + IP_WINDOW <= now:
        record.failed_attempts = 0
        record.blocked_until = None
        record.window_started_at = now


def _record_ip_failure(session: Session, ip_address: str, now: datetime) -> None:
    digest = _rate_hash(ip_address)
    set_local_context(session, "app.rate_limit_hash", digest.hex())
    statement = insert(LoginRateLimit).values(
        key_hash=digest,
        failed_attempts=1,
        window_started_at=now,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[LoginRateLimit.key_hash],
        set_={
            "failed_attempts": LoginRateLimit.failed_attempts + 1,
            "updated_at": now,
        },
        where=LoginRateLimit.window_started_at > now - IP_WINDOW,
    )
    session.execute(statement)
    record = session.get(LoginRateLimit, digest, with_for_update=True)
    if record is not None and record.failed_attempts >= IP_FAILURE_LIMIT:
        record.blocked_until = now + IP_WINDOW


def _clear_ip_failures(session: Session, ip_address: str) -> None:
    digest = _rate_hash(ip_address)
    set_local_context(session, "app.rate_limit_hash", digest.hex())
    record = session.get(LoginRateLimit, digest, with_for_update=True)
    if record is not None:
        record.failed_attempts = 0
        record.blocked_until = None
        record.window_started_at = _utcnow()


def _mandatory_totp(user: User) -> bool:
    return user.role in {Role.ADMIN, Role.DEPARTMENT_MANAGER}


def _new_session(session: Session, user: User, totp_enabled: bool) -> IssuedSession:
    now = _utcnow()
    session_token = new_token()
    csrf_token = new_token()
    expires_at = now + timedelta(hours=get_settings().session_max_hours)
    totp_required = totp_enabled
    totp_setup_required = _mandatory_totp(user) and not totp_enabled
    set_local_user_id(session, user.id)
    session.add(
        AuthSession(
            id=uuid4(),
            user_id=user.id,
            token_hash=token_digest(session_token),
            csrf_hash=token_digest(csrf_token),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            totp_verified_at=now if not totp_enabled and not totp_setup_required else None,
        )
    )
    return IssuedSession(
        session_token,
        csrf_token,
        expires_at,
        totp_required,
        totp_setup_required,
    )


def _active_totp(session: Session, user_id: UUID) -> TotpCredential | None:
    set_local_user_id(session, user_id)
    return session.scalar(
        select(TotpCredential).where(
            TotpCredential.user_id == user_id,
            TotpCredential.is_enabled.is_(True),
        )
    )


def authenticate(
    session: Session,
    email: str,
    password: str,
    ip_address: str,
    provider: AuthProvider | None = None,
) -> tuple[User, IssuedSession]:
    now = _utcnow()
    try:
        _check_ip_limit(session, ip_address, now)
    except RateLimitExceeded:
        record_audit_event(
            session,
            AuditEventType.LOGIN_FAILED,
            actor_user_id=None,
            actor_role=None,
        )
        raise
    normalized_email = normalize_email(email)
    set_local_context(session, "app.login_email", normalized_email)
    user = session.scalar(select(User).where(User.email == normalized_email))
    credential = (
        session.scalar(select(Credential).where(Credential.user_id == user.id).with_for_update())
        if user is not None
        else None
    )
    authenticated_user = (provider or LocalAuthProvider()).authenticate(
        session, normalized_email, password
    )
    if (
        user is None
        or credential is None
        or credential.locked_at is not None
        or authenticated_user is None
        or authenticated_user.id != user.id
    ):
        if credential is not None and credential.locked_at is None:
            credential.failed_attempts += 1
            if credential.failed_attempts >= ACCOUNT_FAILURE_LIMIT:
                credential.locked_at = now
        _record_ip_failure(session, ip_address, now)
        record_audit_event(
            session,
            AuditEventType.LOGIN_FAILED,
            actor_user_id=user.id if user is not None else None,
            actor_role=user.role.value if user is not None else None,
        )
        raise InvalidLogin

    credential.failed_attempts = 0
    _clear_ip_failures(session, ip_address)
    totp_credential = _active_totp(session, user.id)
    issued = _new_session(session, user, totp_credential is not None)
    record_audit_event(
        session,
        AuditEventType.LOGIN_SUCCEEDED,
        actor_user_id=user.id,
        actor_role=user.role.value,
    )
    return user, issued


def create_invitation(
    session: Session,
    user: User,
    purpose: InvitationPurpose,
    created_by: UUID | None,
) -> str:
    now = _utcnow()
    if created_by is not None:
        set_local_user_id(session, created_by)
    session.scalar(select(User.id).where(User.id == user.id).with_for_update())
    for existing in session.scalars(
        select(Invitation)
        .where(
            Invitation.user_id == user.id,
            Invitation.consumed_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .with_for_update()
    ):
        existing.revoked_at = now
    token = new_token()
    session.add(
        Invitation(
            id=uuid4(),
            user_id=user.id,
            token_hash=token_digest(token),
            purpose=purpose.value,
            created_by=created_by,
            created_at=now,
            expires_at=now + INVITATION_TTL,
        )
    )
    return f"/invite/accept?token={token}"


def accept_invitation(session: Session, token: str, password: str) -> None:
    digest = token_digest(token)
    set_local_context(session, "app.invitation_hash", digest.hex())
    invitation = session.scalar(
        select(Invitation).where(Invitation.token_hash == digest).with_for_update()
    )
    now = _utcnow()
    if (
        invitation is None
        or invitation.consumed_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise InvalidInvitation

    set_local_user_id(session, invitation.user_id)
    user = session.get(User, invitation.user_id)
    if user is None or not user.is_active:
        raise InvalidInvitation
    credential = session.get(Credential, invitation.user_id, with_for_update=True)
    if (invitation.purpose == InvitationPurpose.INITIAL.value and credential is not None) or (
        invitation.purpose == InvitationPurpose.RECOVERY.value and credential is None
    ):
        raise InvalidInvitation

    encoded = hash_password(password)
    if credential is None:
        credential = Credential(
            user_id=user.id,
            password_hash=encoded,
            failed_attempts=0,
            password_updated_at=now,
        )
        session.add(credential)
    else:
        credential.password_hash = encoded
        credential.failed_attempts = 0
        credential.locked_at = None
        credential.password_updated_at = now
        for auth_session in session.scalars(
            select(AuthSession)
            .where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
            )
            .with_for_update()
        ):
            auth_session.revoked_at = now
    invitation.consumed_at = now
    record_audit_event(
        session,
        AuditEventType.INVITATION_ACCEPTED,
        actor_user_id=user.id,
        actor_role=user.role.value,
    )


def admin_create_user(
    session: Session,
    *,
    full_name: str,
    email: str,
    role: Role,
    position_id: UUID,
    created_by: UUID,
) -> tuple[User, str]:
    normalized_email = normalize_email(email)
    set_local_user_id(session, created_by)
    if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
        raise DuplicateUser
    position = session.get(Position, position_id)
    if position is None:
        raise InvalidOrganizationReference
    user = User(
        id=uuid4(),
        full_name=full_name.strip(),
        email=normalized_email,
        role=role,
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(
        EmploymentAssignment(
            id=uuid4(),
            user_id=user.id,
            position_id=position_id,
            starts_at=_utcnow(),
        )
    )
    invitation_url = create_invitation(session, user, InvitationPurpose.INITIAL, created_by)
    record_audit_event(
        session,
        AuditEventType.USER_CREATED,
        actor_user_id=created_by,
        actor_role=Role.ADMIN.value,
        subject_type="user",
        subject_id=user.id,
        metadata=AuditMetadata(target_role=role),
    )
    return user, invitation_url


def admin_create_recovery_invitation(
    session: Session,
    user_id: UUID,
    created_by: UUID,
) -> str:
    set_local_user_id(session, created_by)
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise InvalidOrganizationReference
    invitation_url = create_invitation(session, user, InvitationPurpose.RECOVERY, created_by)
    record_audit_event(
        session,
        AuditEventType.RECOVERY_INVITATION_CREATED,
        actor_user_id=created_by,
        actor_role=Role.ADMIN.value,
        subject_type="user",
        subject_id=user.id,
    )
    return invitation_url


def unlock_user(session: Session, user_id: UUID, admin_id: UUID) -> None:
    set_local_user_id(session, admin_id)
    unlocked = session.execute(
        text("SELECT app_admin_unlock(:user_id)"),
        {"user_id": str(user_id)},
    ).scalar_one()
    if not unlocked:
        raise InvalidOrganizationReference
    record_audit_event(
        session,
        AuditEventType.ACCOUNT_UNLOCKED,
        actor_user_id=admin_id,
        actor_role=Role.ADMIN.value,
        subject_type="user",
        subject_id=user_id,
    )


def start_totp_setup(session: Session, user: User) -> tuple[str, str]:
    set_local_user_id(session, user.id)
    secret = new_totp_secret()
    nonce, ciphertext = encrypt_totp_secret(secret)
    credential = session.get(TotpCredential, user.id)
    if credential is None:
        credential = TotpCredential(
            user_id=user.id,
            secret_nonce=nonce,
            secret_ciphertext=ciphertext,
            is_enabled=False,
        )
        session.add(credential)
    else:
        credential.secret_nonce = nonce
        credential.secret_ciphertext = ciphertext
        credential.is_enabled = False
        credential.enabled_at = None
        credential.last_used_step = None
    record_audit_event(
        session,
        AuditEventType.TOTP_SETUP_STARTED,
        actor_user_id=user.id,
        actor_role=user.role.value,
        subject_type="user",
        subject_id=user.id,
    )
    return (
        base32_secret(secret),
        totp_uri(secret, user.email),
    )


def verify_totp_for_session(
    session: Session,
    user: User,
    auth_session: AuthSession,
    code: str,
) -> bool:
    set_local_user_id(session, user.id)
    credential = session.get(TotpCredential, user.id)
    if credential is None:
        return False
    secret = decrypt_totp_secret(
        credential.secret_nonce,
        credential.secret_ciphertext,
    )
    matched_step = matching_totp_counter(secret, code)
    if matched_step is None or (
        credential.last_used_step is not None and matched_step <= credential.last_used_step
    ):
        auth_session.totp_failed_attempts += 1
        if auth_session.totp_failed_attempts >= ACCOUNT_FAILURE_LIMIT:
            auth_session.revoked_at = _utcnow()
        record_audit_event(
            session,
            AuditEventType.TOTP_VERIFICATION_FAILED,
            actor_user_id=user.id,
            actor_role=user.role.value,
            subject_type="user",
            subject_id=user.id,
        )
        return False
    now = _utcnow()
    was_enabled = credential.is_enabled
    if not credential.is_enabled:
        credential.is_enabled = True
        credential.enabled_at = now
    credential.last_used_step = matched_step
    auth_session.totp_verified_at = now
    auth_session.totp_failed_attempts = 0
    record_audit_event(
        session,
        AuditEventType.TOTP_ENABLED if not was_enabled else AuditEventType.TOTP_VERIFIED,
        actor_user_id=user.id,
        actor_role=user.role.value,
        subject_type="user",
        subject_id=user.id,
    )
    return True


def base32_secret(secret: bytes) -> str:
    import base64

    return base64.b32encode(secret).decode("ascii").rstrip("=")


def create_totp_challenge(secret: bytes, timestamp: int | None = None) -> str:
    return totp_code(secret, timestamp)


def serialize_auth_user(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.value,
        "is_active": user.is_active,
    }


def get_totp_secret(session: Session, user_id: UUID) -> bytes | None:
    credential = _active_totp(session, user_id)
    if credential is None:
        return None
    return decrypt_totp_secret(credential.secret_nonce, credential.secret_ciphertext)


def get_department(session: Session, department_name: str) -> Department | None:
    return session.scalar(select(Department).where(Department.name == department_name.strip()))
