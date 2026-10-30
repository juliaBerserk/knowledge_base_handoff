from estafeta.workers import runner


def test_worker_module_exposes_run() -> None:
    assert callable(runner.run)
