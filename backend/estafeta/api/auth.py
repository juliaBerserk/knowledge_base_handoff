import hmac
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Annotated
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.responses import Response

from estafeta.auth import security, service
from estafeta.auth.mail import send_invitation
from estafeta.core.audit import append_event
from estafeta.core.config import Settings, get_settings
from estafeta.core.db import get_admin_session_factory, get_session_factory
from estafeta.models import Employee, Role
from estafeta.models.audit import Action, Outcome, Target

COOKIE = "__Host-estafeta_session"
CSRF_HEADER = "X-CSRF-Token"
Code = Annotated[str, Field(pattern=r"^[0-9]{6}$")]
Token = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
NewPassword = Annotated[
    str, Field(min_length=security.MIN_PASSWORD_LENGTH, max_length=security.MAX_PASSWORD_LENGTH)
]


def reply(status: int = 200, **body: object) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


class AuthRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return reply(403, detail="Запрос с другого сайта отклонён")
            if request.method == "POST":
                origin = request.headers.get("origin")
                if origin is not None and origin != get_settings().auth_public_origin:
                    return reply(403, detail="Запрос с другого сайта отклонён")
                media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
                if media_type != "application/json":
                    return reply(415, detail="Ожидается запрос в формате JSON")
            try:
                return await handler(request)
            except RequestValidationError:
                return reply(422, detail="Проверьте формат введённых данных")

        return handle


router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuthRoute)


class LoginBody(BaseModel):
    login: Annotated[str, Field(min_length=1, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=256)]
    code: Code | None = None


class RedeemBody(BaseModel):
    token: Token
    password: NewPassword


class TotpBody(BaseModel):
    code: Code


class InviteBody(BaseModel):
    send_email: bool = True


class ChangePasswordBody(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: NewPassword


def client_key(request: Request, settings: Settings) -> str:
    if settings.auth_trust_proxy_ip:
        forwarded = request.headers.get("x-real-ip")
        if forwarded:
            try:
                return str(ip_address(forwarded))
            except ValueError:
                pass
    return request.client.host if request.client else "unknown"


def session_reply(settings: Settings, token: str, enrollment_only: bool) -> JSONResponse:
    response = reply(
        csrf_token=security.csrf_token(settings.app_secret_key, token),
        enrollment_only=enrollment_only,
    )
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_max_hours * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


def rate_exceeded() -> JSONResponse:
    response = reply(429, detail="Слишком много запросов. Повторите попытку через минуту")
    response.headers["Retry-After"] = "60"
    return response


def session_token(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    return token if len(token) == 43 else ""


def csrf_valid(request: Request, settings: Settings, token: str) -> bool:
    supplied = request.headers.get(CSRF_HEADER, "")
    expected = security.csrf_token(settings.app_secret_key, token)
    return bool(token) and hmac.compare_digest(supplied.encode(), expected.encode())


@router.post("/login")
def login(request: Request, body: LoginBody) -> JSONResponse:
    settings = get_settings()
    now = datetime.now(UTC)
    client = client_key(request, settings)
    with get_session_factory().begin() as db:
        if not service.consume_limit(db, "login", client, now, 30):
            if service.consume_limit(db, "login-audit-throttle", client, now, 1):
                append_event(db, Action.LOGIN, Outcome.THROTTLED)
            return rate_exceeded()
        result = service.login(db, settings, body.login, body.password, body.code, now)
        if result.totp_required:
            append_event(db, Action.LOGIN, Outcome.CHALLENGE)
            return reply(
                401, detail="Введите код из приложения-аутентификатора", totp_required=True
            )
        if result.token is None:
            append_event(db, Action.LOGIN, Outcome.DENIED)
            return reply(401, detail="Не удалось войти. Проверьте данные или повторите позже")
        outcome = Outcome.CHALLENGE if result.enrollment_only else Outcome.SUCCESS
        append_event(db, Action.LOGIN, outcome, actor_id=result.employee_id)
        return session_reply(settings, result.token, result.enrollment_only)


@router.post("/redeem")
def redeem(request: Request, body: RedeemBody) -> JSONResponse:
    settings = get_settings()
    now = datetime.now(UTC)
    with get_session_factory().begin() as db:
        if not service.consume_limit(db, "redeem", client_key(request, settings), now, 15):
            return rate_exceeded()
        if not security.password_is_allowed(body.password):
            return reply(422, detail="Этот пароль слишком распространён. Придумайте другой")
        employee_id = service.redeem_invitation(db, body.token, body.password, now)
        if employee_id is None:
            return reply(400, detail="Ссылка недействительна или срок её действия истёк")
        append_event(db, Action.PASSWORD_SET, actor_id=employee_id)
        return reply(login_id=str(employee_id))


@router.get("/session")
def inspect_session(request: Request) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not token:
        return reply(401, detail="Войдите в систему")
    with get_session_factory().begin() as db:
        active = service.load_session(db, settings, token, datetime.now(UTC))
        if active is None:
            return reply(401, detail="Сессия завершена. Войдите снова")
        return reply(
            employee_id=str(active.principal.employee_id),
            role=active.principal.role,
            csrf_token=security.csrf_token(settings.app_secret_key, token),
            enrollment_only=active.session.enrollment_only,
            totp_enabled=active.credential.totp_secret is not None,
        )


@router.post("/password/change")
def change_password(request: Request, body: ChangePasswordBody) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    now = datetime.now(UTC)
    with get_session_factory().begin() as db:
        if not service.consume_limit(db, "password-change", client_key(request, settings), now, 15):
            return rate_exceeded()
        active = service.load_session(db, settings, token, now)
        if active is None:
            return reply(401, detail="Сессия завершена. Войдите снова")
        if active.session.enrollment_only:
            return reply(403, detail="Сначала настройте двухфакторную защиту")
        if body.current_password == body.new_password:
            return reply(422, detail="Новый пароль должен отличаться от текущего")
        if not security.password_is_allowed(body.new_password):
            return reply(422, detail="Этот пароль слишком распространён. Придумайте другой")
        if not service.change_password(db, active, body.current_password, body.new_password, now):
            return reply(403, detail="Текущий пароль неверен или вход временно заблокирован")
        append_event(db, Action.PASSWORD_CHANGED, actor_id=active.principal.employee_id)
    response = reply()
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if token and not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    if token:
        with get_session_factory().begin() as db:
            active = service.load_session(db, settings, token, datetime.now(UTC))
            if active is not None:
                db.delete(active.session)
                append_event(db, Action.LOGOUT, actor_id=active.principal.employee_id)
    response = reply()
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.post("/totp/start")
def begin_totp(request: Request) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    with get_session_factory().begin() as db:
        active = service.load_session(db, settings, token, datetime.now(UTC))
        if active is None:
            return reply(401, detail="Сессия завершена. Войдите снова")
        if active.credential.totp_secret is not None:
            return reply(409, detail="Двухфакторная аутентификация уже настроена")
        return reply(uri=service.start_totp(active, settings))


@router.post("/totp/confirm")
def finish_totp(request: Request, body: TotpBody) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    now = datetime.now(UTC)
    with get_session_factory().begin() as db:
        if not service.consume_limit(db, "totp", client_key(request, settings), now, 10):
            return rate_exceeded()
        active = service.load_session(db, settings, token, now)
        if active is None:
            return reply(401, detail="Сессия завершена. Войдите снова")
        new_token = service.confirm_totp(db, active, settings, body.code, now)
        if new_token is None:
            return reply(400, detail="Код неверен или настройка двухфакторной защиты не начата")
        append_event(db, Action.TOTP_ENABLED, actor_id=active.principal.employee_id)
        if active.session.enrollment_only:
            append_event(db, Action.LOGIN, actor_id=active.principal.employee_id)
        return session_reply(settings, new_token, False)


@router.post("/invitations/{employee_id}")
def invite(request: Request, employee_id: UUID, body: InviteBody) -> JSONResponse:
    settings = get_settings()
    token = session_token(request)
    if not csrf_valid(request, settings, token):
        return reply(403, detail="Не удалось проверить подлинность запроса")
    if not settings.database_admin_url:
        return reply(503, detail="Подключение к базе для администрирования не настроено")
    now = datetime.now(UTC)
    with get_admin_session_factory().begin() as db:
        role_matches = db.scalar(
            sa.text("SELECT current_user = 'estafeta_admin' AND session_user = 'estafeta_admin'")
        )
        if not role_matches:
            return reply(503, detail="Ошибка настройки роли администрирования")
        # Сериализуем выдачу ссылок: два администратора могут выбрать друг друга.
        db.execute(sa.select(sa.func.pg_advisory_xact_lock(317001, 3)))
        active = service.load_session(db, settings, token, now)
        if active is None:
            return reply(401, detail="Сессия завершена. Войдите снова")
        if active.session.enrollment_only or active.principal.role is not Role.ADMIN:
            return reply(403, detail="Выпустить ссылку может администратор с настроенной 2FA")
        target = db.execute(
            sa.select(Employee.corporate_email).where(Employee.id == employee_id)
        ).one_or_none()
        if target is None:
            return reply(404, detail="Сотрудник не найден")
        invitation = service.issue_invitation(db, employee_id, now)
        if invitation is None:
            return reply(409, detail="Учётная запись сотрудника отключена")
        append_event(
            db,
            Action.INVITATION_ISSUED,
            actor_id=active.principal.employee_id,
            target=(Target.EMPLOYEE, employee_id),
        )
    link = security.invitation_link(settings.auth_public_origin, invitation)
    delivered = (
        body.send_email
        and target.corporate_email is not None
        and send_invitation(settings, target.corporate_email, link)
    )
    return reply(201, url=link, expires_in_hours=72, delivered=delivered)
