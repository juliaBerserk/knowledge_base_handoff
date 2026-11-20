from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from estafeta.auth.security import new_token, token_hash
from estafeta.core.config import get_settings
from estafeta.core.db import get_admin_session_factory


def admin_error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail, headers={"Cache-Control": "no-store"})


@contextmanager
def admin_transaction(token: str, *, serialize: bool = False) -> Iterator[tuple[Session, UUID]]:
    settings = get_settings()
    if not token:
        raise admin_error(401, "Войдите в систему")
    if not settings.database_admin_url:
        raise admin_error(503, "Подключение для администрирования не настроено")
    with get_admin_session_factory().begin() as db:
        correct_role = db.scalar(
            sa.text("SELECT current_user = 'estafeta_admin' AND session_user = 'estafeta_admin'")
        )
        if not correct_role:
            raise admin_error(503, "Ошибка настройки роли администрирования")
        if serialize:
            # Приглашения могут блокировать учётку другого администратора.
            db.execute(sa.select(sa.func.pg_advisory_xact_lock(317001, 3)))
        actor = db.scalar(
            sa.select(
                sa.func.auth.check_admin_session(
                    token_hash(token),
                    settings.session_idle_minutes * 60,
                    settings.session_max_hours * 3600,
                    type_=sa.Uuid(),
                )
            )
        )
        if actor is None:
            raise admin_error(403, "Операция доступна администратору с настроенной 2FA")
        yield db, actor


def issue_admin_invitation(db: Session, member: UUID) -> str | None:
    token = new_token()
    issued = db.scalar(sa.select(sa.func.auth.issue_admin_invitation(member, token_hash(token))))
    return token if issued else None
