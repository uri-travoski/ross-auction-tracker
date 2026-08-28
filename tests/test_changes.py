"""Change-detection tests: the pure diff function and significance rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auction_tracker import models as M
from auction_tracker.changes import DiffResult, SignificanceRules, diff_lots, summarize_changes
from auction_tracker.models import Auction, Lot


def _lot(number="1", *, bid=None, count=0, desc="HP ZBook", site_id="1001") -> Lot:
    return Lot(
        lot_number=number, site_id=site_id, description=desc,
        current_bid=bid, bid_count=count,
    )


def _config(config):
    return config


def test_diff_new_lot_is_significant(config):
    auction = Auction(url="x", id=1)
    diff = diff_lots([], [_lot("1")], config=config, auction=auction, cycle_id="c1")
    assert len(diff.new_lots) == 1
    assert diff.changes[0].change_type == M.NEW_LOT
    assert diff.changes[0].significant is True


def test_diff_bid_changed(config):
    auction = Auction(url="x", id=1)
    before = _lot("1", bid=100.0, count=5)
    after = _lot("1", bid=200.0, count=8)
    diff = diff_lots([before], [after], config=config, auction=auction, cycle_id="c1")
    types = {c.change_type for c in diff.changes}
    assert M.BID_CHANGED in types
    assert M.BID_COUNT_CHANGED in types
    bid_change = next(c for c in diff.changes if c.change_type == M.BID_CHANGED)
    assert bid_change.previous == 100.0
    assert bid_change.current == 200.0
    # 100% jump is significant (>= 20%).
    assert bid_change.significant is True


def test_diff_small_bid_change_not_significant(config):
    auction = Auction(url="x", id=1)
    before = _lot("1", bid=1000.0, count=5)
    after = _lot("1", bid=1010.0, count=5)  # 1% jump, $10 < $20 min
    diff = diff_lots([before], [after], config=config, auction=auction, cycle_id="c1")
    bid_change = next(c for c in diff.changes if c.change_type == M.BID_CHANGED)
    assert bid_change.significant is False


def test_diff_first_bid_is_significant(config):
    auction = Auction(url="x", id=1)
    before = _lot("1", bid=None, count=0)
    after = _lot("1", bid=50.0, count=1)
    diff = diff_lots([before], [after], config=config, auction=auction, cycle_id="c1")
    bid_change = next(c for c in diff.changes if c.change_type == M.BID_CHANGED)
    assert bid_change.note == "first bid"
    assert bid_change.significant is True


def test_diff_removed_lot(config):
    auction = Auction(url="x", id=1)
    before = _lot("1", bid=100.0)
    diff = diff_lots([before], [], config=config, auction=auction, cycle_id="c1")
    assert len(diff.removed_lots) == 1
    assert diff.changes[0].change_type == M.REMOVED
    assert diff.changes[0].significant is True


def test_diff_close_time_extension(config):
    auction = Auction(url="x", id=1)
    old_close = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
    new_close = old_close + timedelta(hours=2)
    before = _lot("1", bid=100.0)
    before.closes_at = old_close
    after = _lot("1", bid=100.0)
    after.closes_at = new_close
    diff = diff_lots([before], [after], config=config, auction=auction, cycle_id="c1")
    close_change = next(c for c in diff.changes if c.change_type == M.CLOSE_TIME_CHANGED)
    assert close_change.note == "extended"
    assert close_change.significant is True


def test_diff_description_changed(config):
    auction = Auction(url="x", id=1)
    before = _lot("1", desc="HP ZBook G8")
    after = _lot("1", desc="HP ZBook G8, 32GB RAM, 1TB SSD")
    diff = diff_lots([before], [after], config=config, auction=auction, cycle_id="c1")
    desc_change = next(c for c in diff.changes if c.change_type == M.DESCRIPTION_CHANGED)
    assert desc_change.previous == "HP ZBook G8"
    assert "32GB" in str(desc_change.current)


def test_diff_no_changes_when_identical(config):
    auction = Auction(url="x", id=1)
    lot = _lot("1", bid=100.0, count=5)
    diff = diff_lots([lot], [_lot("1", bid=100.0, count=5)], config=config, auction=auction, cycle_id="c1")
    assert diff.changes == []
    assert diff.new_lots == []
    assert diff.removed_lots == []
    assert diff.changed_lots == []


def test_summarize_changes_counts_by_type(config):
    auction = Auction(url="x", id=1)
    before = _lot("1", bid=100.0, count=5)
    after = _lot("1", bid=200.0, count=8)
    diff = diff_lots([before], [after], config=config, auction=auction, cycle_id="c1")
    counts = summarize_changes(diff.changes)
    assert counts[M.BID_CHANGED] == 1
    assert counts[M.BID_COUNT_CHANGED] == 1


def test_significance_rules_from_config(config):
    rules = SignificanceRules.from_config(config)
    assert rules.bid_jump_pct == 20.0
    assert rules.bid_jump_min_dollars == 20.0
    assert M.NEW_LOT in rules.always
    assert M.REMOVED in rules.always


def test_significance_rules_bid_jump():
    rules = SignificanceRules(bid_jump_pct=20, bid_jump_min_dollars=20)
    assert rules.bid_jump_is_significant(100, 200) is True   # 100%, $100
    assert rules.bid_jump_is_significant(1000, 1010) is False  # 1%, $10
    assert rules.bid_jump_is_significant(None, 50) is True   # first bid
    assert rules.bid_jump_is_significant(100, 110) is False  # 10%, $10 < $20
