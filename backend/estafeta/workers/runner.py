import logging
import time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from estafeta.core.config import get_settings
from estafeta.core.db import get_engine, set_local_user_id, wait_for_db
from estafeta.services.build_jobs import claim_build_job, run_claimed_job
from estafeta.services.embeddings import BgeM3Embedder, Embedder
from estafeta.services.extraction import LLMClient, OpenAICompatibleLLMClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("estafeta.worker")


def run_once(
    engine: Engine,
    *,
    embedder: Embedder,
    llm: LLMClient,
) -> bool:
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    claimed = claim_build_job(session_factory)
    if claimed is None:
        return False
    job_id, requested_by = claimed
    logger.info("Начата сборка задания %s", job_id)
    try:
        run_claimed_job(
            session_factory,
            job_id=job_id,
            requested_by=requested_by,
            embedder=embedder,
            llm=llm,
        )
    except Exception as exc:
        logger.error(
            "Сбой задания сборки %s (тип ошибки %s)",
            job_id,
            type(exc).__name__,
        )
        _mark_job_failed(
            session_factory,
            job_id=job_id,
            requested_by=requested_by,
        )
    return True


def _mark_job_failed(
    session_factory: sessionmaker[Session],
    *,
    job_id: UUID,
    requested_by: UUID,
) -> None:
    with session_factory() as session, session.begin():
        set_local_user_id(session, requested_by)
        session.execute(
            text(
                """
                UPDATE build_jobs
                SET status = 'failed',
                    stage = 'failed',
                    completed_at = now(),
                    lease_started_at = NULL
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        )


def run() -> None:
    settings = get_settings()
    engine = get_engine()
    wait_for_db(engine)
    logger.info("Воркер «%s» запущен", settings.app_name)
    embedder = BgeM3Embedder()
    llm = OpenAICompatibleLLMClient()
    while True:
        if not run_once(engine, embedder=embedder, llm=llm):
            time.sleep(settings.build_job_poll_seconds)


if __name__ == "__main__":
    run()
