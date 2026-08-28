"""Notifier interface.

Add a channel (Slack, Telegram, webhook) by subclassing :class:`Notifier`,
implementing ``deliver``, and registering it in ``notify/__init__.py``. The
event methods and the rate limiting are inherited, so a new channel only has to
know how to send one message.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from ..config import Config
from ..logging_setup import get_logger
from ..models import Auction, Change, Lot
from ..store import Store
from . import messages
from .messages import Message

log = get_logger(__name__)


class Notifier:
    """Base class: decides *whether* to send, subclasses decide *how*."""

    name = "base"

    def __init__(self, config: Config, store: Store | None = None) -> None:
        self.config = config
        self.store = store
        self.tz = config.timezone
        self.rules = config.section("notify")
        self.max_per_hour = int(self.rules.get("max_emails_per_hour", 20))
        self.report_url = self._report_url()
        self.sent = 0

    def _report_url(self) -> str:
        """Best-effort link back to the report UI for use inside messages."""
        import os

        explicit = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if explicit:
            return explicit
        port = os.environ.get("WEB_PORT", "").strip()
        return f"http://localhost:{port}" if port else ""

    # -- to implement -----------------------------------------------------
    def deliver(self, message: Message, recipients: Sequence[str]) -> bool:
        raise NotImplementedError

    def recipients_for(self, event: str) -> list[str]:
        return []

    @property
    def configured(self) -> bool:
        return True

    # -- shared -----------------------------------------------------------
    def _rate_limited(self, event: str) -> bool:
        """Never let a runaway loop empty an inbox. Reminders are exempt."""
        if event == "reminder" or self.store is None or self.max_per_hour <= 0:
            return False
        recent = self.store.emails_sent_since(60)
        if recent >= self.max_per_hour:
            log.warning(
                "notification suppressed by hourly rate limit",
                extra={"event": event, "sent_last_hour": recent, "cap": self.max_per_hour},
            )
            return True
        return False

    def send(self, message: Message) -> bool:
        """Deliver a built message, honouring the rate limit and audit log."""
        recipients = self.recipients_for(message.event_type)
        if not recipients:
            log.warning(
                "no recipients configured; message not sent",
                extra={"event": message.event_type, "subject": message.subject},
            )
            return False
        if self._rate_limited(message.event_type):
            return False
        try:
            ok = self.deliver(message, recipients)
            error = ""
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            log.error(
                "notification delivery failed",
                extra={"event": message.event_type, "error": error},
            )
        if ok:
            self.sent += 1
        if self.store is not None:
            self.store.log_email(
                event_type=message.event_type,
                subject=message.subject,
                recipients=recipients,
                auction_id=message.auction_id,
                status="SENT" if ok else "FAILED",
                error=error,
            )
        return ok

    def enabled(self, key: str) -> bool:
        return bool(self.rules.get(key, True))

    # -- events -----------------------------------------------------------
    def new_auction(self, auction: Auction) -> bool:
        if not self.enabled("on_new_auction"):
            return False
        return self.send(
            messages.new_auction_message(auction, self.tz, self.report_url)
        )

    def final_stretch_entry(self, auction: Auction) -> bool:
        if not self.enabled("on_final_stretch_entry"):
            return False
        return self.send(
            messages.final_stretch_message(auction, self.tz, self.report_url)
        )

    def extension(
        self, auction: Auction, old_end: datetime | None, new_end: datetime | None
    ) -> bool:
        if not self.enabled("on_extension"):
            return False
        return self.send(messages.extension_message(auction, old_end, new_end, self.tz))

    def reminder(
        self,
        auction: Auction,
        lead_minutes: int,
        hot_lots: Sequence[tuple[Lot, float, int]] = (),
    ) -> tuple[bool, Message]:
        """Reminders always send: they are the operator's headline requirement."""
        message = messages.reminder_message(
            auction,
            lead_minutes,
            hot_lots=hot_lots,
            tz=self.tz,
            report_url=self.report_url,
        )
        return self.send(message), message

    def significant_changes(self, auction: Auction, changes: Sequence[Change]) -> bool:
        if not self.enabled("on_significant_change") or not changes:
            return False
        return self.send(
            messages.changes_message(auction, changes, self.tz, self.report_url)
        )

    def finalized(
        self,
        auction: Auction,
        *,
        sold_lots: int,
        total_value: float,
        report_url: str = "",
    ) -> bool:
        if not self.enabled("on_finalize"):
            return False
        return self.send(
            messages.finalized_message(
                auction,
                sold_lots=sold_lots,
                total_value=total_value,
                report_url=report_url or self.report_url,
                tz=self.tz,
            )
        )

    def digest(
        self, lines: Sequence[str], headline: str = "", summary: str = ""
    ) -> bool:
        if not lines:
            return False
        return self.send(
            messages.digest_message(
                lines, headline=headline, summary=summary, report_url=self.report_url
            )
        )

    def test(self) -> bool:
        recipients = self.recipients_for("test") or self.recipients_for("generic")
        return self.send(messages.test_message(recipients))
