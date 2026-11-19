from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from estafeta.auth.security import invitation_link
from estafeta.auth.service import issue_invitation, lock_credential
from estafeta.core.config import MigrationSettings, get_settings
from estafeta.models import Employee, Role


def bootstrap(db: Session, email: str, now: datetime) -> str | None:
    db.execute(sa.select(sa.func.pg_advisory_xact_lock(317001, 3)))
    admins = db.scalars(sa.select(Employee).where(Employee.role == Role.ADMIN)).all()
    if not admins:
        employee = Employee(full_name="Администратор", corporate_email=email, role=Role.ADMIN)
        db.add(employee)
        db.flush()
    elif len(admins) == 1 and admins[0].corporate_email == email:
        employee = admins[0]
        credential = lock_credential(db, employee.id)
        if credential is not None and credential.password_hash is not None:
            return None
    else:
        return None
    return issue_invitation(db, employee.id, now)


def main() -> None:
    settings = get_settings()
    email = settings.bootstrap_admin_email.strip().lower()
    if len(email) > 254 or "@" not in email or any(ch.isspace() for ch in email):
        raise SystemExit("Укажите корпоративную почту в BOOTSTRAP_ADMIN_EMAIL")
    engine = sa.create_engine(MigrationSettings().database_owner_url, hide_parameters=True)
    try:
        with Session(engine) as db, db.begin():
            token = bootstrap(db, email, datetime.now(UTC))
    finally:
        engine.dispose()
    if token is None:
        raise SystemExit("Первый администратор уже создан или его учётная запись отключена")
    print(invitation_link(settings.auth_public_origin, token))


if __name__ == "__main__":
    main()
