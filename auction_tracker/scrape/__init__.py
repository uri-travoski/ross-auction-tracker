"""Scraping layer: pure parsers plus the crawl helpers around them.

* ``index_page`` — auction cards and exhaustive pagination.
* ``detail_page`` — auction metadata and every lot.
* ``feeds``      — the site's JSON caches (authoritative bids, original photos).
* ``classify``   — the IT filter (keywords + AI).

Selectors live in ``auction_tracker/selectors.py``; nothing here hard-codes a
CSS selector.
"""

from .classify import Decision, ITClassifier
from .detail_page import detail_validator, parse_detail_page
from .feeds import (
    apply_bid_feed,
    enrich_lot_images,
    fetch_bid_feed,
    fetch_lot_gallery,
    parse_bid_feed,
    parse_gallery,
)
from .index_page import IndexPage, crawl_index, index_validator, parse_index_page

__all__ = [
    "Decision",
    "ITClassifier",
    "IndexPage",
    "crawl_index",
    "index_validator",
    "parse_index_page",
    "detail_validator",
    "parse_detail_page",
    "apply_bid_feed",
    "enrich_lot_images",
    "fetch_bid_feed",
    "fetch_lot_gallery",
    "parse_bid_feed",
    "parse_gallery",
]
