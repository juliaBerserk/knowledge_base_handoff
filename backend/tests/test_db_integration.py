import os

import pytest
from fastapi.testclient import TestClient

from estafeta.core.config import clear_settings_cache
from estafeta.core.db import ping, reset_engine
from estafeta.main import app

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="нужен PostgreSQL с pgvector (RUN_DB_TESTS=1)",
)


def test_ping_postgres_and_vector() -> None:
    reset_engine()
    clear_settings_cache()
    from estafeta.core.db import get_engine

    database_ok, vector_ok = ping(get_engine())
    assert database_ok is True
    assert vector_ok is True


def test_health_against_postgres() -> None:
    reset_engine()
    clear_settings_cache()
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["vector_extension"] is True
