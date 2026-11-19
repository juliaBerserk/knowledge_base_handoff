import argparse
from urllib.parse import urljoin

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from estafeta.core.config import get_settings
from estafeta.domain.access import Role
from estafeta.models import InvitationPurpose, User
from estafeta.schemas.audit import AuditEventType
from estafeta.services.audit import record_audit_event
from estafeta.services.auth import create_invitation
from estafeta.services.mail import send_invitation_email_safely


def bootstrap_admin(name: str) -> None:
    settings = get_settings()
    if not settings.bootstrap_admin_email:
        raise RuntimeError("BOOTSTRAP_ADMIN_EMAIL не задан")
    try:
        email = str(TypeAdapter(EmailStr).validate_python(settings.bootstrap_admin_email))
    except ValidationError as exc:
        raise RuntimeError("BOOTSTRAP_ADMIN_EMAIL имеет неверный формат") from exc

    engine = create_engine(settings.database_owner_url, pool_pre_ping=True)
    try:
        with Session(engine) as session, session.begin():
            if session.scalar(select(User.id).where(User.role == Role.ADMIN).limit(1)) is not None:
                raise RuntimeError("Администратор уже создан")
            if session.scalar(select(User.id).where(User.email == email)) is not None:
                raise RuntimeError("Учётная запись с BOOTSTRAP_ADMIN_EMAIL уже существует")
            user = User(
                full_name=name.strip(),
                email=email,
                role=Role.ADMIN,
                is_active=True,
            )
            session.add(user)
            session.flush()
            invitation_path = create_invitation(
                session,
                user,
                InvitationPurpose.INITIAL,
                created_by=None,
            )
            record_audit_event(
                session,
                AuditEventType.BOOTSTRAP_ADMIN_CREATED,
                actor_user_id=None,
                actor_role=None,
                subject_type="user",
                subject_id=user.id,
            )
    finally:
        engine.dispose()

    invitation_url = urljoin(
        settings.public_base_url.rstrip("/") + "/",
        invitation_path.lstrip("/"),
    )
    send_invitation_email_safely(settings, email, invitation_path)
    print(f"Одноразовая ссылка для первого администратора (72 часа): {invitation_url}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="estafeta")
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = subparsers.add_parser(
        "bootstrap-admin",
        help="создать первого администратора и одноразовую ссылку",
    )
    bootstrap_parser.add_argument("--name", required=True, help="ФИО администратора")
    arguments = parser.parse_args()
    if arguments.command == "bootstrap-admin":
        bootstrap_admin(arguments.name)


if __name__ == "__main__":
    main()
