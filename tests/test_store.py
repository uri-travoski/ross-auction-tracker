"""Store round-trip tests: insert, update, retrieve."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from auction_tracker import models as M
from auction_tracker.models import Auction, Lot, Change, Cycle


def _auction() -> Auction:
    return Auction(
        url="https://auctions.com.au/x.html", title="Test IT Auction",
        site_id="14704", is_it=True, lot_count=2,
        start_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def test_upsert_auction_insert_then_update(store):
    auction = _auction()
    stored, is_new, changes = store.upsert_auction(auction)
    assert is_new is True
    assert stored.id is not None
    assert changes == []

    # Update with a new title.
    auction.title = "Updated Title"
    stored2, is_new2, changes2 = store.upsert_auction(auction)
    assert is_new2 is False
    assert any(c.change_type == M.DESCRIPTION_CHANGED and c.field_name == "title" for c in changes2)


def test_upsert_auction_detects_extension(store):
    auction = _auction()
    store.upsert_auction(auction)
    # Extend the close time by 2 hours.
    auction.end_at = auction.end_at + timedelta(hours=2)
    _, _, changes = store.upsert_auction(auction)
    ext = [c for c in changes if c.change_type == M.CLOSE_TIME_CHANGED]
    assert len(ext) == 1
    assert ext[0].note == "extended"
    assert ext[0].significant is True


def test_insert_and_retrieve_lot(store):
    auction = _auction()
    store.upsert_auction(auction)
    lot = Lot(lot_number="1", site_id="1001", description="HP ZBook", current_bid=100.0, bid_count=3)
    store.insert_lot(auction.id, lot)
    assert lot.id is not None
    retrieved = store.get_lots(auction.id)
    assert len(retrieved) == 1
    assert retrieved[0].description == "HP ZBook"
    assert retrieved[0].current_bid == 100.0


def test_record_and_retrieve_changes(store):
    auction = _auction()
    stored, _, _ = store.upsert_auction(auction)
    lot = Lot(lot_number="1", site_id="1001", description="HP ZBook")
    store.insert_lot(stored.id, lot)
    change = Change(
        change_type=M.BID_CHANGED, lot_number="1", lot_id=lot.id,
        auction_id=stored.id, previous=100.0, current=200.0,
        observed_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        significant=True, cycle_id="c1",
    )
    store.record_changes([change], cycle_id="c1")
    retrieved = store.changes_for_auction(stored.id)
    assert len(retrieved) == 1
    assert retrieved[0].change_type == M.BID_CHANGED
    assert retrieved[0].significant is True


def test_finalize_lots_freezes_current_bid(store):
    auction = _auction()
    stored, _, _ = store.upsert_auction(auction)
    lot = Lot(lot_number="1", site_id="1001", description="HP ZBook", current_bid=250.0, bid_count=5)
    store.insert_lot(stored.id, lot)
    count = store.finalize_lots(stored.id)
    assert count == 1
    retrieved = store.get_lot(lot.id)
    assert retrieved.final_bid == 250.0
    assert retrieved.bidding_status == M.LOT_CLOSED


def test_mark_finalized_sets_status(store):
    auction = _auction()
    stored, _, _ = store.upsert_auction(auction)
    store.mark_finalized(stored.id)
    retrieved = store.get_auction(stored.id)
    assert retrieved.status == M.FINALIZED
    assert retrieved.finalized_at is not None


def test_auctions_due_for_finalize(store):
    # Closed 4 hours ago — beyond the 3h finalize delay.
    end = datetime.now(timezone.utc) - timedelta(hours=4)
    auction = _auction()
    auction.end_at = end
    store.upsert_auction(auction)
    due = store.auctions_due_for_finalize(delay_minutes=180)
    assert any(a.id == auction.id for a in due)


def test_auctions_not_due_for_finalize_too_recent(store):
    # Closed 1 hour ago — within the 3h finalize delay.
    end = datetime.now(timezone.utc) - timedelta(hours=1)
    auction = _auction()
    auction.end_at = end
    store.upsert_auction(auction)
    due = store.auctions_due_for_finalize(delay_minutes=180)
    assert not any(a.id == auction.id for a in due)


def test_cycle_round_trip(store):
    cycle = Cycle(cycle_type=M.CHANGE, id="abc123", started_at=datetime.now(timezone.utc))
    store.start_cycle(cycle)
    cycle.auctions_scraped = 1
    cycle.lots_seen = 5
    cycle.finished_at = datetime.now(timezone.utc)
    store.finish_cycle(cycle)
    recent = store.recent_cycles(5)
    assert len(recent) == 1
    assert recent[0]["id"] == "abc123"
    assert recent[0]["auctions_scraped"] == 1


def test_stats_empty_store(store):
    stats = store.stats()
    assert stats["auctions_tracked"] == 0
    assert stats["lots"] == 0
    assert stats["changes"] == 0


def test_search_auctions_only_it(store):
    it = _auction()
    it.title = "IT Auction"
    store.upsert_auction(it)
    non_it = Auction(url="https://y", title="Cattle", is_it=False)
    store.upsert_auction(non_it)
    rows, total = store.search_auctions(only_it=True)
    assert total == 1
    assert rows[0].title == "IT Auction"


def test_upsert_auction_can_demote_is_it(store):
    """Ensure an auction initially marked is_it=True can be updated to is_it=False."""
    auction = _auction()
    auction.is_it = True
    stored, _, _ = store.upsert_auction(auction)
    assert stored.is_it is True
    assert store.get_auction(stored.id).is_it is True

    # Update with is_it=False
    auction.is_it = False
    updated, _, _ = store.upsert_auction(auction)
    assert updated.is_it is False
    assert store.get_auction(stored.id).is_it is False


def test_reclassify_auction_updates_status(store):
    """Test store.reclassify_auction explicitly."""
    auction = _auction()
    auction.is_it = True
    stored, _, _ = store.upsert_auction(auction)

    store.reclassify_auction(
        stored.id,
        is_it=False,
        confidence=0.92,
        reason="Diesel engines, not computing",
        source="ai",
        categories=[],
    )
    reloaded = store.get_auction(stored.id)
    assert reloaded.is_it is False
    assert reloaded.it_confidence == 0.92
    assert reloaded.it_reason == "Diesel engines, not computing"
    assert reloaded.it_source == "ai"

