import logging
import time
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from estafeta.auth.service import prune_expired
from estafeta.core.config import get_settings
from estafeta.core.db import wait_for_db
from estafeta.indexing.build import Builder
from estafeta.indexing.providers import BgeM3Embedder, OpenAICompatibleClient
from estafeta.indexing.queue import LeaseLost, claim_next

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("estafeta.worker")


def run() -> None:
    settings = get_settings()
    if not settings.database_worker_url:
        raise RuntimeError("Задайте DATABASE_WORKER_URL")
    engine = create_engine(settings.database_worker_url, pool_pre_ping=True, hide_parameters=True)
    wait_for_db(engine)
    factory = sessionmaker(engine, autoflush=False)
    embedder = BgeM3Embedder(settings.embedding_model, settings.embedding_revision)
    client = OpenAICompatibleClient(
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key,
        settings.llm_timeout_seconds,
    )
    builder = Builder(factory, settings, embedder, client)
    logger.info("Воркер «%s» запущен", settings.app_name)
    next_cleanup = 0.0
    while True:
        try:
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + 900
                prune_expired(factory, settings, datetime.now(UTC))
            with factory.begin() as db:
                claim = claim_next(db)
            if claim is not None:
                builder.run(claim)
                continue
        except LeaseLost:
            logger.info("Срок владения задачей истёк")
        except SQLAlchemyError:
            logger.error("Ошибка БД в фоновом воркере")
        except Exception:
            logger.error("Не удалось завершить фоновое задание")
        time.sleep(2)


if __name__ == "__main__":
    run()
