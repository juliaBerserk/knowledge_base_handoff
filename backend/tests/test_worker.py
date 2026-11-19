from uuid import uuid4

from sqlalchemy import create_engine

from estafeta.workers import runner


def test_worker_module_exposes_run() -> None:
    assert callable(runner.run)


def test_run_once_returns_false_when_queue_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(runner, "claim_build_job", lambda _factory: None)

    assert runner.run_once(
        create_engine("sqlite://"),
        embedder=object(),
        llm=object(),
    ) is False


def test_run_once_marks_unhandled_job_failure(monkeypatch) -> None:
    job_id, user_id = uuid4(), uuid4()
    marked = []

    def fail_job(*_args, **_kwargs) -> None:
        raise RuntimeError("failure")

    monkeypatch.setattr(
        runner,
        "claim_build_job",
        lambda _factory: (job_id, user_id),
    )
    monkeypatch.setattr(
        runner,
        "run_claimed_job",
        fail_job,
    )
    monkeypatch.setattr(
        runner,
        "_mark_job_failed",
        lambda _factory, **kwargs: marked.append((kwargs["job_id"], kwargs["requested_by"])),
    )

    result = runner.run_once(
        create_engine("sqlite://"),
        embedder=object(),
        llm=object(),
    )

    assert result is True
    assert marked == [(job_id, user_id)]
