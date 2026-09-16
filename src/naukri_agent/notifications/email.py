"""
EmailSender abstraction: file / console (Stage A) + SMTP (2026-09-12).

`build_email_sender` returns a FileEmailSender (default) or
ConsoleEmailSender when explicitly selected. `email_sender="smtp"`
now actually sends real email via SmtpEmailSender IF smtp_host,
smtp_username, smtp_password, and notify_email_to are all set;
otherwise it raises EmailConfigError rather than silently falling
back to a file -- a misconfigured schedule should fail visibly, not
quietly write a digest nobody is watching. The default is still
"file", unchanged, so nothing about existing behavior changes unless
EMAIL_SENDER=smtp is explicitly set.

SMTP credentials (smtp_password especially) are read only from
Settings (env vars / .env) -- never hard-coded, never logged. Every
exception raised by SmtpEmailSender reports only the exception TYPE
(matching FileEmailSender's existing convention), never str(exc),
since some SMTP server error responses can echo back parts of the
failed request.
"""

from __future__ import annotations

import datetime
import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from pydantic import BaseModel

from naukri_agent.config import Settings
from naukri_agent.notifications.exceptions import EmailConfigError, EmailSendError

logger = logging.getLogger(__name__)


class EmailMessage(BaseModel):
    to: str | None = None
    subject: str
    text_body: str
    html_body: str | None = None


class SendResult(BaseModel):
    sender: str  # "file" | "console"
    status: str  # "written" | "printed"
    path: str | None = None


class EmailSender(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult:
        """Deliver the message. Raises EmailSendError on failure."""


class FileEmailSender(EmailSender):
    name = "file"

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)

    def send(self, message: EmailMessage) -> SendResult:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"digest_{stamp}.txt"
            header = f"Subject: {message.subject}\n"
            if message.to:
                header += f"To: {message.to}\n"
            path.write_text(header + "\n" + message.text_body, encoding="utf-8")
            if message.html_body:
                path.with_suffix(".html").write_text(message.html_body, encoding="utf-8")
            logger.info("digest written to %s", path)
            return SendResult(sender="file", status="written", path=str(path))
        except Exception as exc:  # noqa: BLE001
            raise EmailSendError(f"FileEmailSender failed: {type(exc).__name__}") from exc


class ConsoleEmailSender(EmailSender):
    name = "console"

    def send(self, message: EmailMessage) -> SendResult:
        try:
            print(f"Subject: {message.subject}\n")
            print(message.text_body)
            return SendResult(sender="console", status="printed")
        except Exception as exc:  # noqa: BLE001
            raise EmailSendError(f"ConsoleEmailSender failed: {type(exc).__name__}") from exc


class SmtpEmailSender(EmailSender):
    """
    Real SMTP delivery via stdlib smtplib + STARTTLS -- works with
    Gmail SMTP (smtp.gmail.com:587, an App Password as smtp_password,
    since Google requires one for SMTP login rather than the account
    password) or any other standard STARTTLS SMTP provider (Outlook,
    a transactional-email relay, etc.); nothing here is Gmail-specific.
    """

    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str, to_addr: str) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.to_addr = to_addr

    def send(self, message: EmailMessage) -> SendResult:
        to_addr = message.to or self.to_addr
        if not to_addr:
            raise EmailSendError("SmtpEmailSender: no recipient address configured")

        mime_msg = MIMEMultipart("alternative")
        mime_msg["Subject"] = message.subject
        mime_msg["From"] = self.username
        mime_msg["To"] = to_addr
        mime_msg.attach(MIMEText(message.text_body, "plain", "utf-8"))
        if message.html_body:
            mime_msg.attach(MIMEText(message.html_body, "html", "utf-8"))

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(mime_msg)
        except Exception as exc:  # noqa: BLE001 -- never log str(exc), see module docstring
            raise EmailSendError(f"SmtpEmailSender failed: {type(exc).__name__}") from exc

        logger.info("digest emailed via SMTP to %s", to_addr)
        return SendResult(sender="smtp", status="sent", path=None)


def build_email_sender(settings: Settings) -> EmailSender:
    choice = (settings.email_sender or "file").strip().lower()
    if choice == "console":
        return ConsoleEmailSender()
    if choice == "smtp":
        missing = [
            env_name
            for env_name, value in (
                ("SMTP_HOST", settings.smtp_host),
                ("SMTP_USERNAME", settings.smtp_username),
                ("SMTP_PASSWORD", settings.smtp_password),
                ("NOTIFY_EMAIL_TO", settings.notify_email_to),
            )
            if not value
        ]
        if missing:
            raise EmailConfigError(
                "email_sender=smtp requires " + ", ".join(missing) + " to be set in .env"
            )
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            to_addr=settings.notify_email_to,
        )
    return FileEmailSender(settings.email_output_dir)
