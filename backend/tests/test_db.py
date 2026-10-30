import pytest
from sqlalchemy.exc import OperationalError

from estafeta.core.db import wait_for_db


class _FakeEngine:
    pass


def test_wait_for_db_raises_after_attempts(monkeypatch) -> None:
    calls = {"n": 0}

    def fail(_engine: object) -> tuple[bool, bool]:
        calls["n"] += 1
        raise OperationalError("select", {}, Exception("down"))

    monkeypatch.setattr("estafeta.core.db.ping", fail)
    with pytest.raises(RuntimeError, match="недоступна"):
        wait_for_db(_FakeEngine(), attempts=3, delay_seconds=0)
    assert calls["n"] == 3


def test_wait_for_db_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("estafeta.core.db.ping", lambda engine: (True, True))
    wait_for_db(_FakeEngine(), attempts=1, delay_seconds=0)
