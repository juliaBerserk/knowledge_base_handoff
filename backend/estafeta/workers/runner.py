import logging
import time
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from estafeta.auth.service import prune_expired
from estafeta.core.config import get_settings
from estafeta.core.db import get_engine, get_session_factory, wait_for_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("estafeta.worker")


def run() -> None:
    settings = get_settings()
    wait_for_db(get_engine())
    logger.info("Воркер «%s» запущен", settings.app_name)
    while True:
        try:
            prune_expired(get_session_factory(), settings, datetime.now(UTC))
        except SQLAlchemyError:
            logger.exception("Не удалось удалить просроченные данные аутентификации")
        time.sleep(900)


if __name__ == "__main__":
    run()
