"""Notification channels.

``build_notifier`` picks the channel: SMTP email when it is configured,
otherwise a log-only stub so the agent still runs (and still records what it
*would* have sent) on a fresh install.

Override with ``AUCTION_TRACKER_NOTIFIER=email|log_only``.
"""

from __future__ import annotations

import os

from ..config import Config
from ..logging_setup import get_logger
from ..store import Store
from .base import Notifier
from .email_smtp import EmailNotifier
from .log_only import LogOnlyNotifier
from .messages import Message

log = get_logger(__name__)

__all__ = [
    "Notifier",
    "EmailNotifier",
    "LogOnlyNotifier",
    "Message",
    "build_notifier",
]

_CHANNELS = {"email": EmailNotifier, "smtp": EmailNotifier, "log_only": LogOnlyNotifier}


def build_notifier(config: Config, store: Store | None = None) -> Notifier:
    requested = os.environ.get("AUCTION_TRACKER_NOTIFIER", "").strip().lower()
    if requested:
        channel = _CHANNELS.get(requested)
        if channel is None:
            log.warning("unknown notifier %r; falling back to auto-detect", requested)
        else:
            return channel(config, store)

    email = EmailNotifier(config, store)
    if email.configured:
        return email
    log.warning(
        "SMTP is not configured (need SMTP_HOST, SMTP_FROM and SMTP_RECIPIENTS "
        "in .env); notifications will be logged only"
    )
    return LogOnlyNotifier(config, store)
