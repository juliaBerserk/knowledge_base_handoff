from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from estafeta.admin.accounts import AccountFields, CsvAccount, CsvError, RowError
from estafeta.core.administration import issue_admin_invitation
from estafeta.core.audit import append_event
from estafeta.models import Department, Employee, Position, PositionAssignment
from estafeta.models.audit import Action, Target


@dataclass(frozen=True)
class ProposedAccount:
    row: int
    fields: AccountFields
    position_id: UUID


@dataclass(frozen=True)
class ProvisionedAccount:
    row: int
    id: UUID
    email: str | None
    token: str


def active_assignment_conditions() -> sa.ColumnElement[bool]:
    now = sa.func.statement_timestamp()
    return (PositionAssignment.started_at <= now) & (
        PositionAssignment.ended_at.is_(None) | (PositionAssignment.ended_at > now)
    )


def key(value: str) -> str:
    return " ".join(value.casefold().split())


def check_duplicates(db: Session, proposed: list[ProposedAccount]) -> None:
    if not proposed:
        return
    emails = {item.fields.corporate_email for item in proposed if item.fields.corporate_email}
    seen_emails = set(
        db.scalars(sa.select(Employee.corporate_email).where(Employee.corporate_email.in_(emails)))
    )
    seen_names: set[tuple[str, UUID]] = set()
    if any(item.fields.corporate_email is None for item in proposed):
        positions = {item.position_id for item in proposed}
        seen_names = {
            (key(name), position)
            for name, position in db.execute(
                sa.select(Employee.full_name, PositionAssignment.position_id)
                .join(PositionAssignment, PositionAssignment.employee_id == Employee.id)
                .where(
                    active_assignment_conditions(), PositionAssignment.position_id.in_(positions)
                )
            )
        }
    errors = []
    batch_names = {
        (key(item.fields.full_name), item.position_id)
        for item in proposed
        if item.fields.corporate_email is not None
    }
    for item in proposed:
        account = item.fields
        if account.corporate_email:
            if account.corporate_email in seen_emails:
                errors.append(RowError(item.row, "Корпоративная почта уже используется"))
            seen_emails.add(account.corporate_email)
        else:
            identity = (key(account.full_name), item.position_id)
            if identity in seen_names or identity in batch_names:
                errors.append(
                    RowError(item.row, "Учётка без почты с этим ФИО и должностью уже есть")
                )
            seen_names.add(identity)
    if errors:
        raise CsvError(errors)


def resolve_csv(db: Session, rows: list[tuple[int, CsvAccount]]) -> list[ProposedAccount]:
    departments: dict[str, list[UUID]] = defaultdict(list)
    positions: dict[tuple[UUID, str], list[UUID]] = defaultdict(list)
    for department in db.scalars(sa.select(Department)):
        for alias in {key(department.name), str(department.id)}:
            departments[alias].append(department.id)
    for position in db.scalars(sa.select(Position)):
        for alias in {key(position.title), str(position.id)}:
            positions[position.department_id, alias].append(position.id)
    proposed = []
    errors = []
    for row, account in rows:
        units = departments.get(key(account.department), [])
        if len(units) != 1:
            errors.append(RowError(row, "Отдел не найден или неоднозначен. Укажите его UUID"))
            continue
        slots = positions.get((units[0], key(account.position)), [])
        if len(slots) != 1:
            errors.append(
                RowError(row, "Должность не найдена в отделе или неоднозначна. Укажите UUID")
            )
            continue
        proposed.append(ProposedAccount(row, account, slots[0]))
    try:
        check_duplicates(db, proposed)
    except CsvError as error:
        errors.extend(error.errors)
    if errors:
        raise CsvError(sorted(errors, key=lambda item: item.row))
    return proposed


def provision(db: Session, actor: UUID, proposed: ProposedAccount) -> ProvisionedAccount:
    account = proposed.fields
    member = uuid4()
    db.execute(
        sa.insert(Employee.__table__)
        .inline()
        .values(
            id=member,
            full_name=account.full_name,
            corporate_email=account.corporate_email,
            role=account.role,
        )
    )
    db.execute(
        sa.insert(PositionAssignment.__table__)
        .inline()
        .values(
            id=uuid4(),
            employee_id=member,
            position_id=proposed.position_id,
            started_at=datetime.now(UTC),
        )
    )
    token = issue_admin_invitation(db, member)
    if token is None:
        raise RuntimeError("Для новой учётной записи не удалось выпустить приглашение")
    for action in (Action.ACCOUNT_CREATED, Action.INVITATION_ISSUED):
        append_event(db, action, actor_id=actor, target=(Target.EMPLOYEE, member))
    return ProvisionedAccount(proposed.row, member, account.corporate_email, token)
