import hmac
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from estafeta.core.config import get_settings
from estafeta.core.db import get_session_factory, set_local_context, set_local_user_id
from estafeta.core.security import token_digest
from estafeta.domain.access import Role
from estafeta.models import AuthSession, TotpCredential, User


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    auth_session: AuthSession
    totp_enabled: bool


def get_db_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_principal(
    request: Request,
    session: Session = Depends(get_db_session),
) -> Principal:
    raw_token = request.cookies.get("estafeta_session")
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")

    settings = get_settings()
    digest = token_digest(raw_token)
    set_local_context(session, "app.session_hash", digest.hex())
    set_local_context(
        session,
        "app.session_idle_minutes",
        str(settings.session_idle_minutes),
    )
    auth_session = session.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
    now = datetime.now(UTC)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= now
        or auth_session.last_seen_at + timedelta(minutes=settings.session_idle_minutes) <= now
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия истекла")

    set_local_user_id(session, auth_session.user_id)
    user = session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Учётная запись отключена",
        )

    auth_session.last_seen_at = now
    totp = session.scalar(select(TotpCredential).where(TotpCredential.user_id == user.id))
    return Principal(user, auth_session, bool(totp and totp.is_enabled))


def require_full_auth(principal: Principal = Depends(get_principal)) -> Principal:
    needs_totp = principal.totp_enabled or principal.user.role in {
        Role.ADMIN,
        Role.DEPARTMENT_MANAGER,
    }
    if needs_totp and principal.auth_session.totp_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Требуется настройка или подтверждение TOTP",
        )
    return principal


def require_admin(principal: Principal = Depends(require_full_auth)) -> Principal:
    if principal.user.role is not Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return principal


def require_security_auditor(
    principal: Principal = Depends(require_full_auth),
) -> Principal:
    if principal.user.role is not Role.SECURITY_AUDITOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return principal


def require_knowledge_user(
    principal: Principal = Depends(require_full_auth),
) -> Principal:
    if principal.user.role in {Role.ADMIN, Role.SECURITY_AUDITOR}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Нет доступа к содержимому знаний",
        )
    return principal


def verify_csrf(request: Request, principal: Principal) -> None:
    cookie_token = request.cookies.get("estafeta_csrf")
    header_token = request.headers.get("X-CSRF-Token")
    if (
        cookie_token is None
        or header_token is None
        or cookie_token != header_token
        or not hmac.compare_digest(
            token_digest(cookie_token),
            principal.auth_session.csrf_hash,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Проверка CSRF не пройдена",
        )


def request_client_ip(request: Request) -> str:
    forwarded_ip = request.headers.get("X-Real-IP")
    if forwarded_ip:
        import ipaddress

        try:
            return str(ipaddress.ip_address(forwarded_ip))
        except ValueError:
            pass
    if request.client is None:
        return "unknown"
    return request.client.host


def set_session_cookies(response, session_token: str, csrf_token: str, expires_at) -> None:
    settings = get_settings()
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        "estafeta_session",
        session_token,
        max_age=max_age,
        expires=expires_at,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "estafeta_csrf",
        csrf_token,
        max_age=max_age,
        expires=expires_at,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response) -> None:
    settings = get_settings()
    response.delete_cookie(
        "estafeta_session",
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        "estafeta_csrf",
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=False,
        samesite="lax",
    )


def db_session_for_user(session: Session, user_id: UUID) -> None:
    set_local_user_id(session, user_id)
