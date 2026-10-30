import logging
import time

from estafeta.core.config import get_settings
from estafeta.core.db import get_engine, wait_for_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("estafeta.worker")


def run() -> None:
    settings = get_settings()
    wait_for_db(get_engine())
    logger.info(
        "Воркер «%s» запущен, очередь jobs появится на этапе сборки базы",
        settings.app_name,
    )
    while True:
        time.sleep(30)


if __name__ == "__main__":
    run()
