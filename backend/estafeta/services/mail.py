import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin

from estafeta.core.config import Settings

logger = logging.getLogger("estafeta.mail")


def send_invitation_email(
    settings: Settings,
    recipient: str,
    invitation_path: str,
) -> None:
    if not settings.smtp_host:
        return
    if not settings.smtp_from:
        raise ValueError("SMTP_FROM обязателен при настроенном SMTP_HOST")

    message = EmailMessage()
    message["Subject"] = "Приглашение в систему «Эстафета»"
    message["From"] = settings.smtp_from
    message["To"] = recipient
    link = urljoin(settings.public_base_url.rstrip("/") + "/", invitation_path.lstrip("/"))
    message.set_content(
        "Для создания пароля откройте одноразовую ссылку. "
        "Она действует 72 часа и может быть использована только один раз:\n\n"
        f"{link}\n\n"
        "Если вы не ожидали это письмо, обратитесь к администратору."
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)
    logger.info("Отправлено приглашение")


def send_invitation_email_safely(
    settings: Settings,
    recipient: str,
    invitation_path: str,
) -> None:
    try:
        send_invitation_email(settings, recipient, invitation_path)
    except (OSError, smtplib.SMTPException, ValueError):
        logger.exception("Не удалось отправить приглашение")
