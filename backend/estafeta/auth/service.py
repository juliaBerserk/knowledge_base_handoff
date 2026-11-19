from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from estafeta.auth import security
from estafeta.auth.provider import LocalPasswordProvider, PasswordProvider
from estafeta.core.config import Settings
from estafeta.models import Employee, Role
from estafeta.models.auth import AuthSession, Credential, Invitation, RateLimit

MANDATORY_TOTP = frozenset({Role.ADMIN, Role.DEPARTMENT_HEAD})
LOCK_DURATION = timedelta(minutes=15)
INVITATION_DURATION = timedelta(hours=72)
LOCAL_PROVIDER = LocalPasswordProvider()


@dataclass(frozen=True)
class Principal:
    employee_id: UUID
    role: Role
    corporate_email: str | None


@dataclass(frozen=True)
class ActiveSession:
    principal: Principal
    credential: Credential
    session: AuthSession
    token: str


@dataclass(frozen=True)
class LoginResult:
    token: str | None = None
    enrollment_only: bool = False
    totp_required: bool = False
    employee_id: UUID | None = None


def consume_limit(db: Session, scope: str, client: str, now: datetime, limit: int) -> bool:
    window = int(now.timestamp()) // 60
    key = security.token_hash(f"{scope}:{client}")
    statement = insert(RateLimit).values(key=key, window=window, hits=1)
    statement = statement.on_conflict_do_update(
        index_elements=[RateLimit.key],
        set_={
            "window": window,
            "hits": sa.case((RateLimit.window == window, RateLimit.hits + 1), else_=1),
        },
        where=sa.or_(
            RateLimit.window < window,
            sa.and_(RateLimit.window == window, RateLimit.hits < limit),
        ),
    ).returning(RateLimit.hits)
    return db.scalar(statement) is not None


def lock_credential(db: Session, employee_id: UUID) -> Credential | None:
    return db.get(Credential, employee_id, with_for_update=True, populate_existing=True)


def revoke_sessions(db: Session, employee_id: UUID) -> None:
    db.execute(sa.delete(AuthSession).where(AuthSession.employee_id == employee_id))


def block_account(db: Session, employee_id: UUID, now: datetime) -> None:
    db.execute(insert(Credential).values(employee_id=employee_id).on_conflict_do_nothing())
    credential = lock_credential(db, employee_id)
    if credential is None:
        raise RuntimeError("Не удалось заблокировать учётную запись")
    credential.blocked_at = now
    revoke_sessions(db, employee_id)
    db.execute(sa.delete(Invitation).where(Invitation.employee_id == employee_id))
    db.flush()


def issue_invitation(db: Session, employee_id: UUID, now: datetime) -> str | None:
    db.execute(insert(Credential).values(employee_id=employee_id).on_conflict_do_nothing())
    credential = lock_credential(db, employee_id)
    if credential is None or credential.blocked_at is not None:
        return None
    db.execute(sa.delete(Invitation).where(Invitation.employee_id == employee_id))
    token = security.new_token()
    db.add(
        Invitation(
            token_hash=security.token_hash(token),
            employee_id=employee_id,
            expires_at=now + INVITATION_DURATION,
        )
    )
    return token


def redeem_invitation(db: Session, token: str, password: str, now: datetime) -> UUID | None:
    digest = security.token_hash(token)
    employee_id = db.scalar(
        sa.select(Invitation.employee_id).where(Invitation.token_hash == digest)
    )
    if employee_id is None:
        return None
    credential = lock_credential(db, employee_id)
    invitation = db.get(Invitation, digest, populate_existing=True)
    if (
        credential is None
        or credential.blocked_at is not None
        or invitation is None
        or invitation.expires_at <= now
    ):
        return None
    credential.password_hash = security.hash_password(password)
    clear_failures(credential)
    db.delete(invitation)
    revoke_sessions(db, employee_id)
    return employee_id


def clear_failures(credential: Credential) -> None:
    credential.failures = 0
    credential.locked_until = None


def change_password(
    db: Session, active: ActiveSession, current: str, new: str, now: datetime
) -> bool:
    credential = active.credential
    if is_locked(credential, now):
        return False
    if not security.verify_password(current, credential.password_hash):
        record_failure(credential, now)
        return False
    credential.password_hash = security.hash_password(new)
    clear_failures(credential)
    db.execute(sa.delete(Invitation).where(Invitation.employee_id == credential.employee_id))
    revoke_sessions(db, credential.employee_id)
    return True


def record_failure(credential: Credential, now: datetime) -> None:
    credential.failures += 1
    if credential.failures >= 5:
        credential.locked_until = now + LOCK_DURATION


def is_locked(credential: Credential, now: datetime) -> bool:
    if credential.locked_until is None:
        return False
    if credential.locked_until > now:
        return True
    clear_failures(credential)
    return False


def find_login(db: Session, login: str) -> tuple[Principal, Credential] | None:
    login = login.strip().lower()
    try:
        key = Employee.id == UUID(login)
    except ValueError:
        key = Employee.corporate_email == login
    row = db.execute(
        sa.select(Employee.id, Employee.role, Employee.corporate_email, Credential)
        .join(Credential, Credential.employee_id == Employee.id)
        .where(key)
        .with_for_update(of=Credential)
    ).one_or_none()
    if row is None:
        return None
    return Principal(row.id, row.role, row.corporate_email), row.Credential


def create_session(
    db: Session,
    employee_id: UUID,
    now: datetime,
    enrollment_only: bool,
    *,
    created_at: datetime | None = None,
) -> str:
    token = security.new_token()
    db.add(
        AuthSession(
            token_hash=security.token_hash(token),
            employee_id=employee_id,
            created_at=created_at or now,
            last_seen_at=now,
            enrollment_only=enrollment_only,
        )
    )
    return token


def login(
    db: Session,
    settings: Settings,
    identifier: str,
    password: str,
    code: str | None,
    now: datetime,
    provider: PasswordProvider = LOCAL_PROVIDER,
) -> LoginResult:
    entry = find_login(db, identifier)
    credential = entry[1] if entry else None
    locked = credential is None or credential.blocked_at is not None or is_locked(credential, now)
    password_hash = credential.password_hash if credential is not None and not locked else None
    valid_password = provider.verify(password, password_hash)
    if entry is None or locked:
        return LoginResult()
    principal, credential = entry
    if not valid_password:
        record_failure(credential, now)
        return LoginResult()
    if credential.totp_secret is not None:
        if code is None:
            return LoginResult(totp_required=True)
        secret = security.totp_cipher(settings.app_secret_key).decrypt(credential.totp_secret)
        step = security.totp_step(secret, code, now, credential.totp_last_step)
        if step is None:
            record_failure(credential, now)
            return LoginResult()
        credential.totp_last_step = step
    clear_failures(credential)
    enroll = principal.role in MANDATORY_TOTP and credential.totp_secret is None
    return LoginResult(
        token=create_session(db, principal.employee_id, now, enroll),
        enrollment_only=enroll,
        employee_id=principal.employee_id,
    )


def session_expired(session: AuthSession, settings: Settings, now: datetime) -> bool:
    return now >= session.created_at + timedelta(
        hours=settings.session_max_hours
    ) or now >= session.last_seen_at + timedelta(minutes=settings.session_idle_minutes)


def prune_expired(factory: sessionmaker[Session], settings: Settings, now: datetime) -> None:
    session_cutoff = now - timedelta(hours=settings.session_max_hours)
    idle_cutoff = now - timedelta(minutes=settings.session_idle_minutes)
    expired_sessions = sa.delete(AuthSession).where(
        sa.or_(AuthSession.created_at <= session_cutoff, AuthSession.last_seen_at <= idle_cutoff)
    )
    expired_invitations = sa.delete(Invitation).where(Invitation.expires_at <= now)
    old_limits = sa.delete(RateLimit).where(RateLimit.window < int(now.timestamp()) // 60 - 1440)
    for statement in (expired_sessions, expired_invitations, old_limits):
        with factory.begin() as db:
            db.execute(statement)


def load_session(
    db: Session, settings: Settings, token: str, now: datetime
) -> ActiveSession | None:
    digest = security.token_hash(token)
    employee_id = db.scalar(
        sa.select(AuthSession.employee_id).where(AuthSession.token_hash == digest)
    )
    if employee_id is None:
        return None
    credential = lock_credential(db, employee_id)
    session = db.get(AuthSession, digest, populate_existing=True)
    if credential is None or credential.blocked_at is not None or session is None:
        return None
    if session_expired(session, settings, now):
        db.delete(session)
        return None
    employee = db.execute(
        sa.select(Employee.id, Employee.role, Employee.corporate_email).where(
            Employee.id == employee_id
        )
    ).one()
    if employee.role in MANDATORY_TOTP and credential.totp_secret is None:
        session.enrollment_only = True
    session.last_seen_at = max(now, session.last_seen_at)
    return ActiveSession(
        Principal(employee.id, employee.role, employee.corporate_email), credential, session, token
    )


def start_totp(active: ActiveSession, settings: Settings) -> str:
    secret = security.new_totp_secret()
    active.session.pending_totp = security.totp_cipher(settings.app_secret_key).encrypt(secret)
    principal = active.principal
    return security.totp_uri(secret, principal.corporate_email or str(principal.employee_id))


def confirm_totp(
    db: Session, active: ActiveSession, settings: Settings, code: str, now: datetime
) -> str | None:
    pending = active.session.pending_totp
    if pending is None or active.credential.totp_secret is not None:
        return None
    secret = security.totp_cipher(settings.app_secret_key).decrypt(pending)
    step = security.totp_step(secret, code, now)
    if step is None:
        return None
    active.credential.totp_secret = pending
    active.credential.totp_last_step = step
    revoke_sessions(db, active.principal.employee_id)
    return create_session(
        db, active.principal.employee_id, now, False, created_at=active.session.created_at
    )
