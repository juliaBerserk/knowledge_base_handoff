import smtplib
import ssl
from email.message import EmailMessage

from estafeta.core.config import Settings


def send_invitation(settings: Settings, recipient: str, url: str) -> bool:
    if not settings.smtp_host or not settings.smtp_from:
        return False
    try:
        message = EmailMessage()
        message["Subject"] = "Одноразовая ссылка для «Эстафеты»"
        message["From"] = settings.smtp_from
        message["To"] = recipient
        message.set_content(
            f"Задайте пароль по одноразовой ссылке. Она действует 72 часа.\n\n{url}"
        )
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, ValueError, smtplib.SMTPException):
        return False
    return True
