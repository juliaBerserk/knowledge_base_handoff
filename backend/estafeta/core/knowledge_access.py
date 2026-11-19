from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from estafeta.auth.service import load_session
from estafeta.core.config import get_settings
from estafeta.core.db import get_session_factory
from estafeta.core.permissions import Relation, can_read
from estafeta.models import KnowledgeRecord, Material, Role


@contextmanager
def knowledge_transaction(token: str) -> Iterator[Session | None]:
    with get_session_factory().begin() as db:
        active = load_session(db, get_settings(), token, datetime.now(UTC))
        if (
            active is None
            or active.session.enrollment_only
            or active.principal.role not in {Role.EMPLOYEE, Role.DEPARTMENT_HEAD}
        ):
            yield None
            return
        db.execute(
            sa.text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": str(active.principal.employee_id)},
        )
        yield db


def read_knowledge[T: (Material, KnowledgeRecord)](
    db: Session, model: type[T], item_id: UUID
) -> T | None:
    relation_fn = sa.func.knowledge.access_relation(model.space_id)
    row = db.execute(sa.select(model, relation_fn).where(model.id == item_id)).one_or_none()
    if row is None:
        return None
    item, relation = row
    if not can_read(Relation(relation), item.space_kind, item.status):
        return None
    return item
