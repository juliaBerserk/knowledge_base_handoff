import smtplib
import ssl
from collections.abc import Sequence
from email.message import EmailMessage
from time import monotonic

from estafeta.core.config import Settings


def send_invitation(settings: Settings, recipient: str, url: str) -> bool:
    return 0 in send_invitations(settings, [(recipient, url)])


def send_invitations(settings: Settings, items: Sequence[tuple[str, str]]) -> set[int]:
    delivered: set[int] = set()
    if not items or not settings.smtp_host or not settings.smtp_from:
        return delivered
    deadline = monotonic() + 20
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            for index, (recipient, url) in enumerate(items):
                if monotonic() >= deadline:
                    break
                try:
                    smtp.send_message(invitation_message(settings.smtp_from, recipient, url))
                    delivered.add(index)
                except smtplib.SMTPRecipientsRefused:
                    continue
    except (OSError, ValueError, smtplib.SMTPException):
        pass
    return delivered


def invitation_message(sender: str, recipient: str, url: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Одноразовая ссылка для «Эстафеты»"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(f"Задайте пароль по одноразовой ссылке. Она действует 72 часа.\n\n{url}")
    return message
