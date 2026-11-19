from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from estafeta.core.db import set_local_context
from estafeta.core.security import normalize_email, verify_password
from estafeta.models import Credential, User


class AuthProvider(Protocol):
    def authenticate(self, session: Session, email: str, password: str) -> User | None:
        """Аутентифицирует пользователя внешним или локальным провайдером."""


class LocalAuthProvider:
    """Локальный провайдер; блокировки и лимиты ведёт прикладной сервис."""

    def authenticate(self, session: Session, email: str, password: str) -> User | None:
        normalized_email = normalize_email(email)
        set_local_context(session, "app.login_email", normalized_email)
        user = session.scalar(select(User).where(User.email == normalized_email))
        if user is None:
            verify_password(password, None)
            return None
        credential = session.get(Credential, user.id)
        if credential is None:
            verify_password(password, None)
            return None
        if not verify_password(password, credential.password_hash):
            return None
        return user
