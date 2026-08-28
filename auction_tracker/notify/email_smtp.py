"""SMTP email notifier.

Credentials and the recipient list come from ``.env``:

* ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_SECURITY`` (``starttls``/``ssl``/``none``)
* ``SMTP_USERNAME``, ``SMTP_PASSWORD`` (a Gmail *App Password*, not the login)
* ``SMTP_FROM``
* ``SMTP_RECIPIENTS`` — comma-separated; **all** of them get every alert
* ``SMTP_REMINDER_RECIPIENTS`` — optional extra addresses that receive only the
  pre-close reminders

Set ``NOTIFY_DRY_RUN=1`` to log messages instead of sending them.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Sequence

from ..config import Config
from ..logging_setup import get_logger
from ..store import Store
from ..util import truncate
from .base import Notifier
from .messages import Message

log = get_logger(__name__)


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, config: Config, store: Store | None = None) -> None:
        super().__init__(config, store)
        self.settings = config.email

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def recipients_for(self, event: str) -> list[str]:
        return self.settings.recipients_for(event)

    # ------------------------------------------------------------------
    def _subject(self, message: Message) -> str:
        prefix = self.settings.subject_prefix.strip()
        if prefix and not message.subject.startswith(prefix):
            return f"{prefix} {message.subject}"
        return message.subject

    def _build(self, message: Message, recipients: Sequence[str]) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = self._subject(message)
        mail["From"] = self.settings.sender
        mail["To"] = ", ".join(recipients)
        mail["Date"] = formatdate(localtime=True)
        mail["Message-ID"] = make_msgid(domain="auction-tracker.local")
        mail["X-Auction-Event"] = message.event_type
        if message.auction_id:
            mail["X-Auction-Id"] = str(message.auction_id)
        # Reminders are time-critical; ask clients not to bundle them away.
        if message.event_type == "reminder":
            mail["X-Priority"] = "2"
            mail["Importance"] = "high"
        mail.set_content(message.text or "See the HTML version of this message.")
        mail.add_alternative(message.html, subtype="html")
        return mail

    def deliver(self, message: Message, recipients: Sequence[str]) -> bool:
        settings = self.settings
        if not settings.enabled:
            log.info("email disabled in config; not sending", extra={"event": message.event_type})
            return False
        if settings.dry_run:
            log.info(
                "NOTIFY_DRY_RUN: email not sent",
                extra={
                    "event": message.event_type,
                    "subject": self._subject(message),
                    "to": ", ".join(recipients),
                    "preview": truncate(message.text, 300),
                },
            )
            return True
        if not settings.host:
            log.error("SMTP_HOST is not set; cannot send email")
            return False

        mail = self._build(message, recipients)
        context = ssl.create_default_context()
        try:
            if settings.security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    settings.host,
                    settings.port,
                    timeout=settings.timeout_seconds,
                    context=context,
                )
            else:
                server = smtplib.SMTP(
                    settings.host, settings.port, timeout=settings.timeout_seconds
                )
            with server:
                server.ehlo()
                if settings.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                if settings.username and settings.password:
                    server.login(settings.username, settings.password)
                server.send_message(mail, to_addrs=list(recipients))
        except smtplib.SMTPAuthenticationError as exc:
            log.error(
                "SMTP authentication failed — for Gmail use a 16-character App "
                "Password, not the account password",
                extra={"error": str(exc)},
            )
            return False
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            log.error(
                "SMTP delivery failed",
                extra={
                    "error": f"{type(exc).__name__}: {exc}",
                    "host": settings.host,
                    "port": settings.port,
                    "security": settings.security,
                },
            )
            return False

        log.info(
            "email sent",
            extra={
                "event": message.event_type,
                "subject": self._subject(message),
                "recipients": len(recipients),
            },
        )
        return True
