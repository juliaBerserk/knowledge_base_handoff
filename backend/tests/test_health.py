from fastapi.testclient import TestClient

from estafeta.main import app


def test_health_ok(monkeypatch) -> None:
    monkeypatch.setattr("estafeta.api.health.ping", lambda engine: (True, True))
    monkeypatch.setattr("estafeta.api.health.get_engine", lambda: object())
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "estafeta"
    assert body["database"] is True
    assert body["vector_extension"] is True


def test_health_database_unavailable(monkeypatch) -> None:
    def fail(_engine: object) -> tuple[bool, bool]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr("estafeta.api.health.ping", fail)
    monkeypatch.setattr("estafeta.api.health.get_engine", lambda: object())
    response = TestClient(app).get("/api/health")
    assert response.status_code == 503
    assert response.json()["detail"] == "База данных недоступна"


def test_health_database_flag_false(monkeypatch) -> None:
    monkeypatch.setattr("estafeta.api.health.ping", lambda engine: (False, False))
    monkeypatch.setattr("estafeta.api.health.get_engine", lambda: object())
    response = TestClient(app).get("/api/health")
    assert response.status_code == 503
