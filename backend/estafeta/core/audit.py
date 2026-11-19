from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from estafeta.ingest.policy import RedactionKind
from estafeta.models.audit import Action, AuditEvent, Outcome, Target


def append_event(
    db: Session,
    action: Action,
    outcome: Outcome = Outcome.SUCCESS,
    *,
    actor_id: UUID | None = None,
    target: tuple[Target, UUID] | None = None,
    redaction: tuple[RedactionKind, int] | None = None,
) -> None:
    values: dict[str, object] = {
        "action": action,
        "outcome": outcome,
        "actor_id": actor_id,
        "target_kind": target[0] if target else None,
        "target_id": target[1] if target else None,
    }
    if redaction is not None:
        values["redaction_kind"], values["redaction_count"] = redaction
    db.execute(sa.insert(AuditEvent.__table__).inline(), values)
