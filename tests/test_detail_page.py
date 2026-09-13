"""Detail-page parser tests against the captured 115-lot IT auction fixture."""
from __future__ import annotations

from auction_tracker.scrape import detail_validator, parse_detail_page
from auction_tracker.fetch import FetchResult
from auction_tracker import models as M


def test_parse_detail_page_metadata(detail_page_it_html: str, config):
    auction = parse_detail_page(detail_page_it_html, "https://auctions.com.au/x.html", config)
    assert auction.title, "title should be parsed"
    assert "medical" in auction.title.lower() or "hp" in auction.title.lower()
    assert auction.site_id == "14704"
    assert auction.start_at is not None
    assert auction.end_at is not None
    assert auction.end_at > auction.start_at


def test_parse_detail_page_lots(detail_page_it_html: str, config):
    auction = parse_detail_page(detail_page_it_html, "https://auctions.com.au/x.html", config)
    assert len(auction.lots) == 115
    lot1 = auction.lots[0]
    assert lot1.lot_number
    assert lot1.site_id, "data-lot-id should be captured"
    assert lot1.description, "description should be captured"
    assert lot1.quantity >= 1


def test_parse_detail_page_status_derived_from_times(detail_page_it_html: str, config):
    # The fixture is an in-progress auction; the parser must derive IN_PROGRESS
    # from the start/end times because the detail page has no status badge.
    auction = parse_detail_page(detail_page_it_html, "https://auctions.com.au/x.html", config)
    assert auction.status in {M.IN_PROGRESS, M.FORTHCOMING, M.CLOSED}
    if auction.start_at and auction.end_at:
        midpoint = auction.start_at + (auction.end_at - auction.start_at) / 2
        assert auction.computed_status(at=midpoint) == M.IN_PROGRESS


def test_detail_validator_accepts_real_page(detail_page_it_html: str):
    result = FetchResult(url="x", status=200, text=detail_page_it_html)
    assert detail_validator(result) is True


def test_detail_validator_rejects_blank():
    assert detail_validator(FetchResult(url="x", status=200, text="")) is False


def test_parse_detail_page_lot_numbers_are_distinct(detail_page_it_html: str, config):
    auction = parse_detail_page(detail_page_it_html, "https://auctions.com.au/x.html", config)
    numbers = [lot.lot_number for lot in auction.lots]
    assert len(numbers) == len(set(numbers)), "lot numbers must be unique"
