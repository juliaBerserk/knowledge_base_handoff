from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from estafeta.models.indexing import BuildJob

ACTIVE = ("queued", "running")
LEASE_TIME = timedelta(minutes=10)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Claim:
    id: UUID
    owner_id: UUID
    lease: UUID


class LeaseLost(RuntimeError):
    pass


def enqueue(db: Session, owner: UUID) -> UUID:
    stmt = insert(BuildJob.__table__).values(id=uuid4(), owner_id=owner)
    created = db.scalar(stmt.on_conflict_do_nothing().returning(BuildJob.id))
    if created is not None:
        return created
    existing = db.scalar(
        sa.select(BuildJob.id).where(
            BuildJob.owner_id == owner,
            BuildJob.status.in_(ACTIVE),
        )
    )
    if existing is None:
        return enqueue(db, owner)
    return existing


def claim_next(db: Session) -> Claim | None:
    while True:
        now = datetime.now(UTC)
        stmt = (
            sa.select(BuildJob)
            .where(
                sa.or_(
                    BuildJob.status == "queued",
                    (BuildJob.status == "running") & (BuildJob.leased_until <= now),
                )
            )
            .order_by(BuildJob.created_at, BuildJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        job = db.scalar(stmt)
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.stage = "failed", "finished"
            job.lease = job.leased_until = None
            job.progress = {**job.progress, "error": "interrupted"}
            db.flush()
            continue
        lease = uuid4()
        job.status, job.stage = "running", "planning"
        job.lease, job.leased_until = lease, now + LEASE_TIME
        job.attempts += 1
        return Claim(job.id, job.owner_id, lease)


def set_actor(db: Session, actor: UUID) -> None:
    db.execute(sa.text("SELECT set_config('app.user_id', :id, true)"), {"id": str(actor)})


def guard(db: Session, claim: Claim) -> BuildJob:
    now = datetime.now(UTC)
    job = db.scalar(
        sa.select(BuildJob)
        .where(
            BuildJob.id == claim.id,
            BuildJob.lease == claim.lease,
            BuildJob.status == "running",
            BuildJob.leased_until > now,
        )
        .with_for_update()
    )
    if job is None:
        raise LeaseLost
    job.leased_until = now + LEASE_TIME
    set_actor(db, claim.owner_id)
    return job
