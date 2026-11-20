from logging import getLogger
from threading import Event

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

logger = getLogger(__name__)


def process_due(factory: sessionmaker[Session], limit: int = 25) -> int:
    processed = 0
    for _ in range(limit):
        with factory.begin() as db:
            if not db.scalar(sa.select(sa.func.knowledge.process_handoff())):
                break
        processed += 1
    return processed


def scheduler(factory: sessionmaker[Session], stop: Event) -> None:
    while not stop.is_set():
        try:
            processed = process_due(factory)
        except sa.exc.DBAPIError as exc:
            logger.error(
                "Ошибка планировщика эстафет (SQLSTATE %s)",
                getattr(exc.orig, "sqlstate", "unknown"),
            )
            processed = 0
        if processed < 25:
            stop.wait(2)
