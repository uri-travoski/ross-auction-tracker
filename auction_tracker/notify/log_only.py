"""Notifier that writes to the log instead of sending anything.

The default when SMTP is not configured, and what the tests use. It keeps the
whole pipeline exercisable without credentials.
"""

from __future__ import annotations

from typing import Sequence

from ..logging_setup import get_logger
from ..util import truncate
from .base import Notifier
from .messages import Message

log = get_logger(__name__)


class LogOnlyNotifier(Notifier):
    name = "log_only"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.messages: list[Message] = []

    def recipients_for(self, event: str) -> list[str]:
        # A pseudo-recipient so ``send`` does not bail out as unconfigured.
        return ["log"]

    def deliver(self, message: Message, recipients: Sequence[str]) -> bool:
        self.messages.append(message)
        log.info(
            "NOTIFICATION (log only)",
            extra={
                "event": message.event_type,
                "subject": message.subject,
                "body": truncate(message.text, 800),
            },
        )
        return True
