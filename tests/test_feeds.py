"""JSON feed parser tests: max_bids and lot gallery."""
from __future__ import annotations

from auction_tracker.scrape import apply_bid_feed, parse_bid_feed, parse_gallery
from auction_tracker.scrape.detail_page import parse_detail_page
from auction_tracker.models import Auction, Lot


def test_parse_bid_feed_returns_115_entries(max_bids_14704):
    feed = parse_bid_feed(max_bids_14704)
    assert len(feed) == 115
    # Keys are the site's lot ids (strings).
    assert all(isinstance(k, str) for k in feed)
    first = feed["2206305"]
    assert first["max_bid"] == "24.00"
    assert first["num_bids"] == "14"
    assert first["highest"] == "134263"
    assert first["met_reserve"] == "N"
    assert first["bids_close_time"] == 1788172200


def test_parse_bid_feed_handles_dict_wrapper():
    # Some endpoints wrap the list under "lots" or "data".
    wrapped = {"lots": [{"id": 1, "max_bid": "10"}]}
    assert len(parse_bid_feed(wrapped)) == 1


def test_parse_bid_feed_rejects_garbage():
    assert parse_bid_feed(None) == {}
    assert parse_bid_feed("not a list") == {}
    assert parse_bid_feed([{"no_id": True}]) == {}


def test_apply_bid_feed_updates_lots(max_bids_14704):
    feed = parse_bid_feed(max_bids_14704)
    auction = Auction(url="x")
    lot = Lot(lot_number="1", site_id="2206305")
    auction.lots = [lot]
    updated = apply_bid_feed(auction, feed)
    assert updated == 1
    assert lot.current_bid == 24.0
    assert lot.bid_count == 14
    assert lot.highest_bidder_id == "134263"
    assert lot.met_reserve is False
    assert lot.closes_at is not None
    assert lot.opens_at is not None
    # The losing-bidder sequence plus the winner.
    assert lot.bidder_sequence[-1] == "134263"
    assert lot.unique_bidders >= 2


def test_apply_bid_feed_syncs_auction_window(max_bids_14704):
    feed = parse_bid_feed(max_bids_14704)
    auction = Auction(url="x")
    auction.lots = [Lot(lot_number="1", site_id="2206305")]
    apply_bid_feed(auction, feed)
    assert auction.end_at is not None
    assert auction.start_at is not None
    assert auction.end_at > auction.start_at


def test_parse_gallery_returns_eight_images(lot_gallery_2206305):
    images = parse_gallery(lot_gallery_2206305)
    assert len(images) == 8
    first = images[0]
    assert first["href"], "original URL must be present"
    assert "1500x1000" in first["href"]
    assert first["thumbnail"], "thumbnail URL must be present"
    assert first["title"], "title must be present"


def test_parse_gallery_handles_dict_wrapper():
    wrapped = {"images": [{"href": "x", "thumbnail": "t", "title": "y"}]}
    assert len(parse_gallery(wrapped)) == 1


def test_parse_gallery_skips_items_without_href():
    out = parse_gallery([{"href": ""}, {"thumbnail": "x"}, {"href": "ok"}])
    assert len(out) == 1
    assert out[0]["href"] == "ok"


def test_apply_bid_feed_total_bid_count(max_bids_14704, detail_page_it_html, config):
    """Smoke: applying the feed to the parsed detail page touches all 115 lots."""
    auction = parse_detail_page(detail_page_it_html, "https://x", config)
    feed = parse_bid_feed(max_bids_14704)
    updated = apply_bid_feed(auction, feed)
    assert updated == 115
    total_bids = sum(lot.bid_count for lot in auction.lots)
    assert total_bids == 2281
    no_bid = sum(1 for lot in auction.lots if not lot.bid_count)
    assert no_bid == 0
