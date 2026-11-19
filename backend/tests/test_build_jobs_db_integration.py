import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from estafeta.core.config import clear_settings_cache, get_settings
from estafeta.core.db import set_local_user_id

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="нужен PostgreSQL с pgvector (RUN_DB_TESTS=1)",
)


def test_worker_claim_is_atomic_and_job_rows_are_owner_scoped() -> None:
    clear_settings_cache()
    settings = get_settings()
    owner_engine = create_engine(settings.database_owner_url)
    app_engine = create_engine(settings.database_url)
    user_id = uuid4()
    outsider_id = uuid4()
    job_ids = sorted([uuid4(), uuid4()])

    try:
        with owner_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO users (id, full_name, email, role) VALUES "
                     "(:id, 'Build owner', :email, 'employee')"),
                {"id": user_id, "email": f"build-{user_id}@example.test"},
            )
            connection.execute(
                text("INSERT INTO users (id, full_name, email, role) VALUES "
                     "(:id, 'Build outsider', :email, 'employee')"),
                {"id": outsider_id, "email": f"build-{outsider_id}@example.test"},
            )
            connection.execute(
                text(
                    "INSERT INTO build_jobs (id, requested_by, status, stage) "
                    "VALUES (:id, :owner, 'pending', 'queued')"
                ),
                [
                    {"id": job_id, "owner": user_id}
                    for job_id in job_ids
                ],
            )

        with Session(app_engine) as lock_session, lock_session.begin():
            set_local_user_id(lock_session, user_id)
            lock_session.execute(
                text("SELECT id FROM build_jobs WHERE id = :id FOR UPDATE"),
                {"id": job_ids[0]},
            ).one()
            with Session(app_engine) as session, session.begin():
                row = session.execute(
                    text("SELECT job_id, requested_by FROM app_claim_build_job()")
                ).one()
                assert (row.job_id, row.requested_by) == (job_ids[1], user_id)

        with Session(app_engine) as session, session.begin():
            row = session.execute(
                text("SELECT job_id FROM app_claim_build_job()")
            ).one()
            assert row.job_id == job_ids[0]

        with Session(app_engine) as session, session.begin():
            set_local_user_id(session, user_id)
            jobs = session.execute(
                text("SELECT id, status, attempt_count FROM build_jobs ORDER BY id")
            ).all()
            assert [
                (job.id, job.status, job.attempt_count) for job in jobs
            ] == [(job_id, "running", 1) for job_id in job_ids]

        with Session(app_engine) as session, session.begin():
            set_local_user_id(session, outsider_id)
            assert session.execute(
                text("SELECT id FROM build_jobs WHERE id = :id"),
                {"id": job_ids[0]},
            ).first() is None

        with Session(app_engine) as session, session.begin():
            assert session.execute(
                text("SELECT job_id FROM app_claim_build_job()")
            ).first() is None
    finally:
        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM build_jobs WHERE id IN (:first, :second)"),
                {"first": job_ids[0], "second": job_ids[1]},
            )
            connection.execute(
                text("DELETE FROM users WHERE id IN (:owner, :outsider)"),
                {"owner": user_id, "outsider": outsider_id},
            )
        app_engine.dispose()
        owner_engine.dispose()
        clear_settings_cache()
