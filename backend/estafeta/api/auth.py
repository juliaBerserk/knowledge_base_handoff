import csv
import io
from datetime import UTC, datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from estafeta.api.dependencies import (
    Principal,
    clear_session_cookies,
    get_db_session,
    get_principal,
    request_client_ip,
    require_admin,
    set_session_cookies,
    verify_csrf,
)
from estafeta.core.config import get_settings
from estafeta.core.db import set_local_user_id
from estafeta.core.security import PasswordPolicyError, normalize_email
from estafeta.domain.access import Role
from estafeta.models import Department, Position, User
from estafeta.schemas.audit import AuditEventType, AuditMetadata
from estafeta.schemas.auth import (
    AuthenticatedUser,
    CsvImportResponse,
    InvitationAcceptance,
    InvitationResponse,
    LoginRequest,
    LoginResponse,
    TotpCodeRequest,
    TotpSetupResponse,
    UserCreate,
)
from estafeta.services.audit import record_audit_event
from estafeta.services.auth import (
    DuplicateUser,
    InvalidInvitation,
    InvalidLogin,
    InvalidOrganizationReference,
    IssuedSession,
    RateLimitExceeded,
    accept_invitation,
    admin_create_recovery_invitation,
    admin_create_user,
    authenticate,
    serialize_auth_user,
    start_totp_setup,
    unlock_user,
    verify_totp_for_session,
)
from estafeta.services.mail import send_invitation_email_safely

router = APIRouter(tags=["auth"])
admin_router = APIRouter(prefix="/admin/users", tags=["admin-users"])
_CSV_LIMIT = 1024 * 1024
_CSV_COLUMNS = {"full_name", "email", "department", "position", "role"}


def _set_login_cookies(response: Response, issued: IssuedSession) -> None:
    set_session_cookies(
        response,
        issued.session_token,
        issued.csrf_token,
        issued.expires_at,
    )


@router.post("/auth/invitations/accept", status_code=204)
def accept_invitation_route(
    payload: InvitationAcceptance,
    session: Session = Depends(get_db_session),
) -> Response:
    try:
        accept_invitation(session, payload.token, payload.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Проверка пароля временно недоступна",
        ) from exc
    except InvalidInvitation as exc:
        raise HTTPException(status_code=400, detail="Приглашение недействительно") from exc
    return Response(status_code=204)


@router.post("/auth/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db_session),
) -> LoginResponse | Response:
    try:
        user, issued = authenticate(
            session,
            str(payload.email),
            payload.password,
            request_client_ip(request),
        )
    except RateLimitExceeded:
        return JSONResponse(
            status_code=429,
            content={"detail": "Слишком много попыток входа. Повторите позже."},
            headers={"Retry-After": "900"},
        )
    except InvalidLogin:
        return JSONResponse(
            status_code=401,
            content={"detail": "Неверный логин или пароль"},
        )

    _set_login_cookies(response, issued)
    return LoginResponse(
        authenticated=not issued.totp_required and not issued.totp_setup_required,
        totp_required=issued.totp_required,
        totp_setup_required=issued.totp_setup_required,
        csrf_token=issued.csrf_token,
    )


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db_session),
) -> Response:
    verify_csrf(request, principal)
    principal.auth_session.revoked_at = datetime.now(UTC)
    record_audit_event(
        session,
        AuditEventType.LOGOUT,
        actor_user_id=principal.user.id,
        actor_role=principal.user.role.value,
        subject_type="session",
        subject_id=principal.auth_session.id,
    )
    clear_session_cookies(response)
    response.status_code = 204
    return response


@router.get("/auth/me")
def current_user(principal: Principal = Depends(get_principal)) -> dict[str, object]:
    needs_totp_setup = (
        principal.user.role
        in {
            Role.ADMIN,
            Role.DEPARTMENT_MANAGER,
        }
        and not principal.totp_enabled
    )
    return {
        "user": serialize_auth_user(principal.user),
        "authenticated": (
            principal.auth_session.totp_verified_at is not None
            or not principal.totp_enabled
            and not needs_totp_setup
        ),
        "totp_setup_required": needs_totp_setup,
        "totp_required": principal.totp_enabled and principal.auth_session.totp_verified_at is None,
    }


@router.post("/auth/totp/setup", response_model=TotpSetupResponse)
def totp_setup(
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db_session),
) -> TotpSetupResponse:
    verify_csrf(request, principal)
    if principal.totp_enabled and principal.auth_session.totp_verified_at is None:
        raise HTTPException(status_code=403, detail="Сначала подтвердите текущий TOTP")
    secret, uri = start_totp_setup(session, principal.user)
    principal.auth_session.totp_verified_at = None
    return TotpSetupResponse(secret=secret, provisioning_uri=uri)


@router.post("/auth/totp/verify", response_model=None)
def totp_verify(
    payload: TotpCodeRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_db_session),
) -> dict[str, bool] | Response:
    verify_csrf(request, principal)
    if principal.auth_session.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Сессия отозвана")
    if not verify_totp_for_session(
        session,
        principal.user,
        principal.auth_session,
        payload.code,
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "Неверный код подтверждения"},
        )
    return {"verified": True}


@admin_router.post("", response_model=InvitationResponse, status_code=201)
def create_user(
    payload: UserCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> InvitationResponse:
    verify_csrf(request, principal)
    try:
        user, invitation_url = admin_create_user(
            session,
            full_name=payload.full_name,
            email=str(payload.email),
            role=payload.role,
            position_id=payload.position_id,
            created_by=principal.user.id,
        )
    except DuplicateUser as exc:
        raise HTTPException(status_code=409, detail="Учётная запись уже существует") from exc
    except InvalidOrganizationReference as exc:
        raise HTTPException(status_code=422, detail="Не найдена должность") from exc
    background_tasks.add_task(
        send_invitation_email_safely,
        get_settings(),
        user.email,
        invitation_url,
    )
    return InvitationResponse(
        user=AuthenticatedUser(**serialize_auth_user(user)),
        invitation_url=invitation_url,
    )


@admin_router.post("/import-csv", response_model=CsvImportResponse, status_code=201)
async def import_users_csv(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    principal: Principal = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> CsvImportResponse:
    verify_csrf(request, principal)
    raw = await file.read(_CSV_LIMIT + 1)
    if len(raw) > _CSV_LIMIT:
        raise HTTPException(status_code=413, detail="CSV-файл превышает 1 МБ")
    try:
        contents = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV должен быть в UTF-8") from exc
    reader = csv.DictReader(io.StringIO(contents, newline=""))
    if reader.fieldnames is None or set(reader.fieldnames) != _CSV_COLUMNS:
        raise HTTPException(
            status_code=422,
            detail="CSV должен содержать столбцы full_name,email,department,position,role",
        )
    rows = list(reader)
    if not rows or len(rows) > 500:
        raise HTTPException(status_code=422, detail="CSV должен содержать от 1 до 500 строк")

    invitations: list[InvitationResponse] = []
    seen_emails: set[str] = set()
    set_local_user_id(session, principal.user.id)
    for row_number, row in enumerate(rows, start=2):
        if any(row.get(column) is None for column in _CSV_COLUMNS):
            raise HTTPException(
                status_code=422,
                detail=f"Некорректная строка CSV: {row_number}",
            )
        try:
            data = UserCreate(
                full_name=row["full_name"],
                email=row["email"],
                role=row["role"],
                position_id=_position_for_csv(
                    session,
                    row["department"],
                    row["position"],
                ),
            )
        except (ValidationError, ValueError, InvalidOrganizationReference) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Некорректная строка CSV: {row_number}",
            ) from exc
        normalized_email = normalize_email(str(data.email))
        if normalized_email in seen_emails:
            raise HTTPException(
                status_code=422,
                detail=f"Повторяющийся адрес в строке CSV: {row_number}",
            )
        seen_emails.add(normalized_email)
        try:
            user, invitation_url = admin_create_user(
                session,
                full_name=data.full_name,
                email=str(data.email),
                role=data.role,
                position_id=data.position_id,
                created_by=principal.user.id,
            )
        except DuplicateUser as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Учётная запись из строки CSV уже существует: {row_number}",
            ) from exc
        invitations.append(
            InvitationResponse(
                user=AuthenticatedUser(**serialize_auth_user(user)),
                invitation_url=invitation_url,
            )
        )
        background_tasks.add_task(
            send_invitation_email_safely,
            get_settings(),
            user.email,
            invitation_url,
        )
    record_audit_event(
        session,
        AuditEventType.CSV_IMPORT_COMPLETED,
        actor_user_id=principal.user.id,
        actor_role=principal.user.role.value,
        metadata=AuditMetadata(imported_count=len(invitations)),
    )
    return CsvImportResponse(invitations=invitations)


@admin_router.post("/{user_id}/recovery-invitation", status_code=201)
def recovery_invitation(
    user_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> dict[str, str]:
    verify_csrf(request, principal)
    try:
        invitation_url = admin_create_recovery_invitation(
            session,
            user_id,
            principal.user.id,
        )
    except InvalidOrganizationReference as exc:
        raise HTTPException(status_code=404, detail="Учётная запись не найдена") from exc
    user = session.get(User, user_id)
    if user is not None:
        background_tasks.add_task(
            send_invitation_email_safely,
            get_settings(),
            user.email,
            invitation_url,
        )
    return {"invitation_url": invitation_url}


@admin_router.post("/{user_id}/unlock", status_code=204)
def unlock_account(
    user_id: UUID,
    request: Request,
    principal: Principal = Depends(require_admin),
    session: Session = Depends(get_db_session),
) -> Response:
    verify_csrf(request, principal)
    try:
        unlock_user(session, user_id, principal.user.id)
    except InvalidOrganizationReference as exc:
        raise HTTPException(status_code=404, detail="Учётные данные не найдены") from exc
    return Response(status_code=204)


def _position_for_csv(
    session: Session,
    department_name: str,
    position_title: str,
) -> UUID:
    department = session.scalar(
        select(Department).where(Department.name == department_name.strip())
    )
    if department is None:
        raise InvalidOrganizationReference
    position = session.scalar(
        select(Position).where(
            Position.department_id == department.id,
            Position.title == position_title.strip(),
        )
    )
    if position is None:
        raise InvalidOrganizationReference
    return position.id
