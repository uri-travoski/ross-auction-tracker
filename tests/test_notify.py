"""Notifier tests: log-only channel, SMTP dry-run, rate limiting, recipients."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auction_tracker import models as M
from auction_tracker.models import Auction, Lot, Change
from auction_tracker.notify.log_only import LogOnlyNotifier


def _auction(end_at=None) -> Auction:
    return Auction(
        url="https://x", title="Test IT Auction", is_it=True,
        end_at=end_at or datetime(2026, 8, 31, 16, tzinfo=timezone.utc),
        first_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def test_log_only_notifier_sends(store, config):
    notifier = LogOnlyNotifier(config, store)
    assert notifier.configured is True
    ok = notifier.new_auction(_auction())
    assert ok is True
    assert len(notifier.messages) == 1
    assert notifier.messages[0].event_type == "new_auction"
    # The email log should record the send.
    emails = store.recent_emails(10)
    assert len(emails) == 1
    assert emails[0]["status"] == "SENT"


def test_log_only_notifier_reminder(store, config):
    notifier = LogOnlyNotifier(config, store)
    auction = _auction()
    store.upsert_auction(auction)
    ok, msg = notifier.reminder(auction, lead_minutes=180)
    assert ok is True
    assert msg.event_type == "reminder"
    # The subject says "3 hours" not "180" (humanized).
    assert "3 hour" in msg.subject


def test_log_only_notifier_significant_changes(store, config):
    notifier = LogOnlyNotifier(config, store)
    auction = _auction()
    store.upsert_auction(auction)
    changes = [
        Change(change_type=M.BID_CHANGED, lot_number="1", auction_id=auction.id,
               previous=100.0, current=200.0, significant=True, observed_at=datetime.now(timezone.utc)),
    ]
    ok = notifier.significant_changes(auction, changes)
    assert ok is True
    assert len(notifier.messages) == 1


def test_log_only_notifier_empty_changes_skipped(store, config):
    notifier = LogOnlyNotifier(config, store)
    ok = notifier.significant_changes(_auction(), [])
    assert ok is False
    assert notifier.messages == []


def test_rate_limit_suppresses_after_cap(store, config):
    config._tree["notify"]["max_emails_per_hour"] = 2
    notifier = LogOnlyNotifier(config, store)
    # Send 2 messages (the cap), the 3rd should be suppressed.
    for i in range(3):
        notifier.new_auction(_auction())
    # Only 2 should have been delivered.
    assert len(notifier.messages) == 2


def test_rate_limit_does_not_apply_to_reminders(store, config):
    config._tree["notify"]["max_emails_per_hour"] = 1
    notifier = LogOnlyNotifier(config, store)
    auction = _auction()
    store.upsert_auction(auction)
    # Send one non-reminder to hit the cap.
    notifier.new_auction(auction)
    # A reminder should still go through.
    ok, _ = notifier.reminder(auction, lead_minutes=180)
    assert ok is True
    assert len(notifier.messages) == 2


def test_email_notifier_dry_run(config, store, monkeypatch):
    """SMTP dry-run mode builds the message but does not connect."""
    from auction_tracker.notify.email_smtp import EmailNotifier
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "from@example.com")
    monkeypatch.setenv("SMTP_RECIPIENTS", "a@x.com,b@x.com")
    monkeypatch.setenv("NOTIFY_DRY_RUN", "1")
    notifier = EmailNotifier(config, store)
    assert notifier.configured is True
    assert notifier.settings.dry_run is True
    ok = notifier.new_auction(_auction())
    assert ok is True  # dry-run reports success without connecting


def test_email_notifier_recipients_for_reminder(config, store, monkeypatch):
    from auction_tracker.notify.email_smtp import EmailNotifier
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "from@example.com")
    monkeypatch.setenv("SMTP_RECIPIENTS", "a@x.com,b@x.com")
    monkeypatch.setenv("SMTP_REMINDER_RECIPIENTS", "c@x.com")
    notifier = EmailNotifier(config, store)
    primary = notifier.recipients_for("new_auction")
    assert primary == ["a@x.com", "b@x.com"]
    reminder = notifier.recipients_for("reminder")
    assert "c@x.com" in reminder
    assert "a@x.com" in reminder
    assert "b@x.com" in reminder


def test_email_notifier_unconfigured_falls_back(config, store):
    from auction_tracker.notify.email_smtp import EmailNotifier
    # No SMTP env vars set -> not configured.
    notifier = EmailNotifier(config, store)
    assert notifier.configured is False
    assert notifier.recipients_for("new_auction") == []
