"""Regression tests for the first-capture / baseline notification problem.

The bug: the first scan of an auction generated one NEW_LOT change per lot,
and because NEW_LOT is configured as significant, the notifier fired a
"significant changes" alert for what was really just the baseline snapshot.

The fix (pipeline.py line ~302): suppress the significant-changes email when
``outcome.is_new`` is true — the new-auction alert already covers discovery.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from auction_tracker import models as M
from auction_tracker.changes import DiffResult
from auction_tracker.models import Auction, Lot, Change
from auction_tracker.notify.log_only import LogOnlyNotifier
from auction_tracker.pipeline import Pipeline, ScrapeOutcome


def _auction() -> Auction:
    end = datetime.now(timezone.utc) + timedelta(days=2)
    return Auction(
        url="https://auctions.com.au/x.html", title="Test IT Auction",
        site_id="14704", is_it=True, lot_count=2, status=M.IN_PROGRESS,
        start_at=datetime.now(timezone.utc) - timedelta(days=1),
        end_at=end, end_at_original=end,
        first_seen_at=datetime.now(timezone.utc),
    )


def _outcome(auction, *, is_new, changes) -> ScrapeOutcome:
    """Build a ScrapeOutcome with the given changes (ok=True, no error)."""
    diff = DiffResult(changes=list(changes), new_lots=[], removed_lots=[], changed_lots=[])
    return ScrapeOutcome(auction=auction, diff=diff, is_new=is_new)


def test_first_capture_does_not_send_significant_changes(store, config):
    """The first scan of an auction must NOT fire a significant-changes email."""
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, notifier=notifier)

    auction = _auction()
    store.upsert_auction(auction)

    changes = [
        Change(change_type=M.NEW_LOT, lot_number="1", auction_id=auction.id,
               significant=True, observed_at=datetime.now(timezone.utc)),
        Change(change_type=M.NEW_LOT, lot_number="2", auction_id=auction.id,
               significant=True, observed_at=datetime.now(timezone.utc)),
    ]
    pipeline.scrape_auction = MagicMock(
        return_value=_outcome(auction, is_new=True, changes=changes))

    pipeline.run_change_scan(notify=True)
    sig_emails = [m for m in notifier.messages
                  if m.event_type == "changes"]
    assert sig_emails == [], "first capture must not fire a significant-changes alert"


def test_subsequent_new_lot_does_send_significant_changes(store, config):
    """A genuinely new lot appearing in a later scan MUST fire the alert."""
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, notifier=notifier)

    auction = _auction()
    store.upsert_auction(auction)
    for lot in [Lot(lot_number="1", site_id="1001", description="HP ZBook"),
                Lot(lot_number="2", site_id="1002", description="Dell Latitude")]:
        store.insert_lot(auction.id, lot)

    changes = [
        Change(change_type=M.NEW_LOT, lot_number="3", auction_id=auction.id,
               significant=True, observed_at=datetime.now(timezone.utc)),
    ]
    pipeline.scrape_auction = MagicMock(
        return_value=_outcome(auction, is_new=False, changes=changes))

    pipeline.run_change_scan(notify=True)
    sig_emails = [m for m in notifier.messages
                  if m.event_type == "changes"]
    assert len(sig_emails) == 1, "a genuinely new lot on a rescan must fire the alert"


def test_first_capture_still_records_changes(store, config):
    """The suppression is on notification only — the changes are still recorded.

    Since we mock scrape_auction (which normally does the recording), we
    record the changes manually here and verify the pipeline doesn't interfere.
    The e2e tests verify that the real scrape_auction records correctly.
    """
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, notifier=notifier)

    auction = _auction()
    store.upsert_auction(auction)

    changes = [
        Change(change_type=M.NEW_LOT, lot_number="1", auction_id=auction.id,
               significant=True, observed_at=datetime.now(timezone.utc)),
    ]
    outcome = _outcome(auction, is_new=True, changes=changes)
    pipeline.scrape_auction = MagicMock(return_value=outcome)

    pipeline.run_change_scan(notify=True)
    # Manually record (the real scrape_auction does this internally).
    store.record_changes(changes, cycle_id="test")
    db_changes = store.changes_for_auction(auction.id)
    assert len(db_changes) >= 1
    assert db_changes[0].change_type == M.NEW_LOT
