"""End-to-end pipeline test against captured fixtures (no network).

This wires the real scrapers, classifier, store, and notifier together using
the FakeFetcher seeded with the captured index/detail/feed fixtures, then
runs a discovery + change scan and asserts the database ends up with the
expected auctions, lots, changes, and images metadata.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auction_tracker import models as M
from auction_tracker.config import Config
from auction_tracker.fetch import FetchResult
from auction_tracker.notify.log_only import LogOnlyNotifier
from auction_tracker.pipeline import Pipeline


def _seed_fetcher(fetcher, *, index_html, detail_html, bids_json, gallery_json):
    """Seed the fake fetcher with fixtures for every URL the pipeline will hit.

    The index fixture has 10 auction cards, but we only have one detail page.
    For any auction detail URL we return the IT detail fixture — the
    classifier will then classify all 10 as IT (since the fixture is IT),
    which is fine for testing the pipeline mechanics.
    """
    base = "https://auctions.com.au"
    static = "https://static.auctions.com.au"
    fetcher.add(f"{base}/auctions/online", text=index_html)
    # Bid feed and gallery for auction 14704.
    fetcher.add(f"{static}/cache/max_bids_14704.json", text=json.dumps(bids_json))
    fetcher.add(f"{static}/cache/auction_lot_gallery_2206305.json",
                text=json.dumps(gallery_json))
    fetcher.add(f"{static}/cache/auction_gallery_14704.json",
                text=json.dumps(gallery_json))


def _make_detail_fetcher(fake_fetcher, detail_html, bids_json, gallery_json):
    """Wrap _fetch_once to return the detail HTML for any auction URL."""
    original = fake_fetcher._fetch_once
    static = "https://static.auctions.com.au"
    bids_text = json.dumps(bids_json)
    gallery_text = json.dumps(gallery_json)

    def patched(url, *, binary=False):
        # Return the detail HTML for any /auctions/ URL that isn't the index.
        if "/auctions/" in url and "/auctions/online" not in url and not binary:
            return FetchResult(url=url, status=200, text=detail_html,
                               content_type="text/html", fetcher="fake")
        # Return the bid feed for any max_bids URL.
        if "max_bids_" in url and "14704" in url:
            return FetchResult(url=url, status=200, text=bids_text,
                               content_type="application/json", fetcher="fake")
        # Return the gallery for any lot_gallery URL.
        if "auction_lot_gallery_" in url:
            return FetchResult(url=url, status=200, text=gallery_text,
                               content_type="application/json", fetcher="fake")
        if "auction_gallery_" in url and "14704" in url:
            return FetchResult(url=url, status=200, text=gallery_text,
                               content_type="application/json", fetcher="fake")
        return original(url, binary=binary)

    fake_fetcher._fetch_once = patched


@pytest.fixture()
def seeded_fetcher(fake_fetcher, index_page_1_html, detail_page_it_html,
                   max_bids_14704, lot_gallery_2206305):
    _seed_fetcher(fake_fetcher, index_html=index_page_1_html,
                  detail_html=detail_page_it_html, bids_json=max_bids_14704,
                  gallery_json=lot_gallery_2206305)
    _make_detail_fetcher(fake_fetcher, detail_page_it_html,
                         max_bids_14704, lot_gallery_2206305)
    return fake_fetcher


def test_discovery_finds_and_classifies_auction(
    seeded_fetcher, store, config,
):
    config._tree["images"]["download"] = False
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, fetcher=seeded_fetcher, notifier=notifier)

    cycle = pipeline.run_discovery(notify=True)

    assert cycle.auctions_seen >= 1
    assert cycle.auctions_new >= 1
    assert cycle.auctions_scraped >= 1
    tracked = store.tracked_auctions()
    assert len(tracked) >= 1
    assert tracked[0].is_it is True


def test_change_scan_records_bids(
    seeded_fetcher, store, config,
):
    config._tree["images"]["download"] = False
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, fetcher=seeded_fetcher, notifier=notifier)

    pipeline.run_discovery(notify=False)
    auction = store.tracked_auctions()[0]
    lots = store.get_lots(auction.id)
    assert len(lots) == 115

    cycle = pipeline.run_change_scan(notify=True)
    assert cycle.auctions_scraped >= 1
    # All tracked auctions are scanned; each has 115 lots from the fixture.
    assert cycle.lots_seen >= 115


def test_repeat_scan_does_not_duplicate_lots(
    seeded_fetcher, store, config,
):
    config._tree["images"]["download"] = False
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, fetcher=seeded_fetcher, notifier=notifier)

    pipeline.run_discovery(notify=False)
    auction = store.tracked_auctions()[0]
    lots_before = store.get_lots(auction.id)

    pipeline.run_change_scan(notify=False)
    lots_after = store.get_lots(auction.id)

    assert len(lots_after) == len(lots_before) == 115


def test_bid_feed_applies_final_prices(
    seeded_fetcher, store, config,
):
    config._tree["images"]["download"] = False
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, fetcher=seeded_fetcher, notifier=notifier)

    pipeline.run_discovery(notify=False)
    auction = store.tracked_auctions()[0]
    lots = store.get_lots(auction.id)
    with_bids = [lot for lot in lots if lot.current_bid and lot.current_bid > 0]
    assert len(with_bids) == 115
    total = sum(lot.bid_count for lot in lots)
    assert total == 2281


def test_first_capture_no_significant_changes_email(
    seeded_fetcher, store, config,
):
    """The first capture must not fire a significant-changes alert."""
    config._tree["images"]["download"] = False
    notifier = LogOnlyNotifier(config, store)
    pipeline = Pipeline(config, store, fetcher=seeded_fetcher, notifier=notifier)

    pipeline.run_discovery(notify=True)
    sig = [m for m in notifier.messages if m.event_type == "changes"]
    assert sig == []
    new_auction_emails = [m for m in notifier.messages if m.event_type == "new_auction"]
    assert len(new_auction_emails) >= 1
