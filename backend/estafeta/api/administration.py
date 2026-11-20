from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, field_validator

from estafeta.admin.accounts import CSV_MAX_BYTES, CsvError, ManualAccount, label, read_csv
from estafeta.admin.directory import (
    ProposedAccount,
    ProvisionedAccount,
    active_assignment_conditions,
    check_duplicates,
    key,
    provision,
    resolve_csv,
)
from estafeta.api.auth import AuthRoute, csrf_valid, reply, session_token
from estafeta.auth.mail import send_invitations
from estafeta.auth.security import invitation_link
from estafeta.core.administration import admin_error, admin_transaction
from estafeta.core.audit import append_event
from estafeta.core.config import get_settings
from estafeta.models import Department, Employee, Position, PositionAssignment, PublicationSettings
from estafeta.models.audit import Action, Target

router = APIRouter(prefix="/admin", tags=["administration"], route_class=AuthRoute)
PAGE_SIZE = 50


def authorized_token(request: Request) -> str:
    token = session_token(request)
    if not token:
        raise admin_error(401, "Войдите в систему")
    if request.method == "POST" and not csrf_valid(request, get_settings(), token):
        raise admin_error(403, "Не удалось проверить подлинность запроса")
    return token


class DepartmentBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return label(value)


class PositionBody(BaseModel):
    department_id: UUID
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        return label(value)


class ImportBody(BaseModel):
    csv_text: str = Field(min_length=1, max_length=CSV_MAX_BYTES)
    send_email: bool = True


class AdminSettingsBody(BaseModel):
    review_required: StrictBool
    handoff_retention: Literal["archive", "delete"]


@router.get("/org")
def org_directory(request: Request) -> JSONResponse:
    with admin_transaction(authorized_token(request)) as (db, _):
        departments = db.execute(
            sa.select(Department.id, Department.name).order_by(Department.name, Department.id)
        ).mappings()
        positions = db.execute(
            sa.select(Position.id, Position.department_id, Position.title).order_by(
                Position.title, Position.id
            )
        ).mappings()
        return reply(
            departments=jsonable_encoder([dict(item) for item in departments]),
            positions=jsonable_encoder([dict(item) for item in positions]),
        )


@router.post("/departments")
def create_department(request: Request, body: DepartmentBody) -> JSONResponse:
    with admin_transaction(authorized_token(request), serialize=True) as (db, actor):
        if any(key(name) == key(body.name) for name in db.scalars(sa.select(Department.name))):
            return reply(409, detail="Отдел с таким названием уже существует")
        row_id = uuid4()
        db.add(Department(id=row_id, name=body.name))
        append_event(
            db, Action.DEPARTMENT_CREATED, actor_id=actor, target=(Target.DEPARTMENT, row_id)
        )
    return reply(201, id=str(row_id))


@router.post("/positions")
def create_position(request: Request, body: PositionBody) -> JSONResponse:
    with admin_transaction(authorized_token(request)) as (db, actor):
        if db.get(Department, body.department_id) is None:
            return reply(404, detail="Отдел не найден")
        row_id = uuid4()
        db.add(Position(id=row_id, department_id=body.department_id, title=body.title))
        append_event(db, Action.POSITION_CREATED, actor_id=actor, target=(Target.POSITION, row_id))
    return reply(201, id=str(row_id))


@router.get("/accounts")
def list_accounts(
    request: Request,
    before: UUID | None = None,
    q: Annotated[str, Query(max_length=120)] = "",
) -> JSONResponse:
    with admin_transaction(authorized_token(request)) as (db, _):
        assignment = (
            sa.select(PositionAssignment.position_id)
            .where(PositionAssignment.employee_id == Employee.id, active_assignment_conditions())
            .order_by(PositionAssignment.started_at.desc(), PositionAssignment.id.desc())
            .limit(1)
            .lateral()
        )
        status = sa.func.auth.admin_account_status(Employee.id).label("status")
        stmt = (
            sa.select(
                Employee.id,
                Employee.full_name,
                Employee.corporate_email,
                Employee.role,
                Position.title.label("position"),
                Department.name.label("department"),
                status,
            )
            .select_from(Employee)
            .outerjoin(assignment, sa.true())
            .outerjoin(Position, Position.id == assignment.c.position_id)
            .outerjoin(Department, Department.id == Position.department_id)
        )
        if q.strip():
            stmt = stmt.where(
                Employee.full_name.icontains(q.strip(), autoescape=True)
                | Employee.corporate_email.icontains(q.strip(), autoescape=True)
            )
        if before is not None:
            cursor = db.get(Employee, before)
            if cursor is None:
                return reply(404, detail="Учётная запись не найдена")
            stmt = stmt.where(
                sa.tuple_(Employee.full_name, Employee.id) > (cursor.full_name, cursor.id)
            )
        rows = db.execute(stmt.order_by(Employee.full_name, Employee.id).limit(PAGE_SIZE + 1))
        records = rows.mappings().all()
        return reply(
            items=jsonable_encoder([dict(item) for item in records[:PAGE_SIZE]]),
            next_before=str(records[PAGE_SIZE - 1].id) if len(records) > PAGE_SIZE else None,
        )


def invitations(accounts: list[ProvisionedAccount], send_email: bool) -> list[dict[str, object]]:
    settings = get_settings()
    links = [invitation_link(settings.auth_public_origin, row.token) for row in accounts]
    recipients = [
        (index, account.email, links[index])
        for index, account in enumerate(accounts)
        if send_email and account.email is not None
    ]
    sent = send_invitations(settings, [(email, link) for _, email, link in recipients])
    delivered = {recipients[index][0] for index in sent}
    return [
        {
            "row": account.row,
            "id": str(account.id),
            "sent": index in delivered,
            "url": None if index in delivered else links[index],
            "expires_in_hours": 72,
        }
        for index, account in enumerate(accounts)
    ]


def errors(error: CsvError) -> JSONResponse:
    return reply(422, detail=str(error), errors=[asdict(item) for item in error.errors])


@router.post("/accounts")
def create_account(request: Request, body: ManualAccount) -> JSONResponse:
    try:
        with admin_transaction(authorized_token(request), serialize=True) as (db, actor):
            if db.get(Position, body.position_id) is None:
                return reply(404, detail="Должность не найдена")
            proposed = ProposedAccount(1, body, body.position_id)
            check_duplicates(db, [proposed])
            result = provision(db, actor, proposed)
    except CsvError as error:
        return reply(409, detail=error.errors[0].detail)
    except sa.exc.IntegrityError:
        return reply(409, detail="Учётная запись конфликтует с существующими данными")
    return reply(201, **invitations([result], body.send_email)[0])


@router.post("/accounts/import/preview")
def preview_import(request: Request, body: ImportBody) -> JSONResponse:
    try:
        with admin_transaction(authorized_token(request)) as (db, _):
            return reply(count=len(resolve_csv(db, read_csv(body.csv_text))))
    except CsvError as error:
        return errors(error)


@router.post("/accounts/import")
def import_accounts(request: Request, body: ImportBody) -> JSONResponse:
    try:
        with admin_transaction(authorized_token(request), serialize=True) as (db, actor):
            proposed = resolve_csv(db, read_csv(body.csv_text))
            result = [provision(db, actor, item) for item in proposed]
            append_event(db, Action.ACCOUNTS_IMPORTED, actor_id=actor)
    except CsvError as error:
        return errors(error)
    except sa.exc.IntegrityError:
        return reply(409, detail="Справочник изменился. Повторите проверку CSV")
    return reply(201, created=len(result), accounts=invitations(result, body.send_email))


@router.get("/settings")
def settings(request: Request) -> JSONResponse:
    with admin_transaction(authorized_token(request)) as (db, _):
        current = db.get(PublicationSettings, 1)
        return reply(
            review_required=current.review_required,
            handoff_retention=current.handoff_retention or get_settings().handoff_retention,
        )


@router.post("/settings")
def change_settings(request: Request, body: AdminSettingsBody) -> JSONResponse:
    with admin_transaction(authorized_token(request)) as (db, actor):
        changed = db.execute(
            sa.update(PublicationSettings)
            .where(
                PublicationSettings.id == 1,
                (PublicationSettings.review_required != body.review_required)
                | PublicationSettings.handoff_retention.is_distinct_from(body.handoff_retention),
            )
            .values(**body.model_dump())
        ).rowcount
        if changed:
            append_event(db, Action.ADMIN_SETTINGS_CHANGED, actor_id=actor)
    return reply(**body.model_dump())
