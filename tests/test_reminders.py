"""Reminder timing and deduplication tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auction_tracker import models as M
from auction_tracker.models import Auction, ReminderRecord
from auction_tracker.notify.log_only import LogOnlyNotifier


def _auction(end_at: datetime, *, auction_id: int = 1) -> Auction:
    return Auction(
        url="https://x", id=auction_id, title="Test IT Auction",
        is_it=True, end_at=end_at, end_at_original=end_at,
        first_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def test_reminder_already_sent_dedup(store):
    end = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    auction = _auction(end)
    store.upsert_auction(auction)
    # Log a SENT reminder for the 180-minute lead.
    store.log_reminder(ReminderRecord(
        auction_id=auction.id, lead_minutes=180, scheduled_for=end,
        sent_at=datetime(2026, 8, 31, 13, tzinfo=timezone.utc),
        delivery_status="SENT", recipients=["x@y.com"], message_preview="p",
    ))
    assert store.reminder_already_sent(auction.id, 180, end) is True
    assert store.reminder_already_sent(auction.id, 30, end) is False


def test_reminder_failed_can_be_retried(store):
    end = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    auction = _auction(end)
    store.upsert_auction(auction)
    store.log_reminder(ReminderRecord(
        auction_id=auction.id, lead_minutes=180, scheduled_for=end,
        sent_at=None, delivery_status="FAILED", recipients=[], message_preview="",
    ))
    # A FAILED attempt is NOT considered sent, so it can be retried.
    assert store.reminder_already_sent(auction.id, 180, end) is False


def test_reminder_extension_creates_new_key(store):
    """An extension changes scheduled_for, so the old reminder does not block."""
    original_end = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    extended_end = original_end + timedelta(hours=2)
    auction = _auction(original_end)
    store.upsert_auction(auction)
    store.log_reminder(ReminderRecord(
        auction_id=auction.id, lead_minutes=180, scheduled_for=original_end,
        sent_at=datetime(2026, 8, 31, 13, tzinfo=timezone.utc),
        delivery_status="SENT", recipients=["x@y.com"], message_preview="p",
    ))
    # After extension, the new scheduled_for is the new end time.
    assert store.reminder_already_sent(auction.id, 180, extended_end) is False


def test_pipeline_reminders_due_only_inside_lead_window(store, config):
    from auction_tracker.pipeline import Pipeline
    from auction_tracker.notify.log_only import LogOnlyNotifier

    now = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)  # 2.5h before close
    end = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    auction = _auction(end)
    store.upsert_auction(auction)

    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, notifier=notifier)
    # 180-minute lead triggers at 13:00; we are at 13:30, so it is due.
    cycle = pipeline.run_reminders(at=now)
    assert cycle is not None
    # The 30-minute lead triggers at 15:30; we are at 13:30, so it is NOT due.
    # Only one reminder should have been sent.
    sent = [m for m in notifier.messages if m.event_type == "reminder"]
    assert len(sent) == 1
    # Dedup: a second call at the same time does not re-send.
    notifier2 = LogOnlyNotifier(config, store)
    pipeline2 = Pipeline(config, store, notifier=notifier2)
    cycle2 = pipeline2.run_reminders(at=now)
    assert cycle2 is None or not [m for m in notifier2.messages if m.event_type == "reminder"]


def test_pipeline_reminders_skipped_after_close(store, config):
    from auction_tracker.pipeline import Pipeline
    from auction_tracker.notify.log_only import LogOnlyNotifier

    now = datetime(2026, 8, 31, 17, tzinfo=timezone.utc)  # 1h after close
    end = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    auction = _auction(end)
    store.upsert_auction(auction)
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, notifier=notifier)
    cycle = pipeline.run_reminders(at=now)
    assert cycle is None
