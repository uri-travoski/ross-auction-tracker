"""The site's own JSON caches — the authoritative source for bids and photos.

Two endpoints, both public and unauthenticated (discovered by reading the
inline JavaScript on the detail page; see ``selectors.py`` for the notes):

``max_bids_<auctionId>.json``
    What the site's front-end polls every five seconds. Gives the exact
    current bid, bid count, the winning bidder id, the full losing-bidder
    sequence (``under``), whether the reserve is met, and epoch open/close
    times. It keeps serving data for years after an auction closes, so this is
    also how final sale prices are captured.

``auction_lot_gallery_<lotId>.json``
    Every photo for one lot: ``href`` is the 1500x1000 original, ``thumbnail``
    is 60x60, and ``title`` is the untruncated lot title.

Using these instead of scraping bid text is both kinder to the site and far
more accurate.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..config import Config
from ..fetch import Fetcher
from ..logging_setup import get_logger
from ..models import Auction, Lot
from ..util import clean_text, from_epoch, parse_int, parse_money

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Bid feed
# ---------------------------------------------------------------------------


def bid_feed_url(config: Config, auction_site_id: str) -> str:
    template = str(config.get("site.bids_feed_url", ""))
    return template.format(auction_site_id=auction_site_id) if template else ""


def fetch_bid_feed(
    fetcher: Fetcher, config: Config, auction_site_id: str
) -> dict[str, dict[str, Any]]:
    """Fetch the bid feed, keyed by lot site id. Empty dict on any failure."""
    if not auction_site_id:
        return {}
    url = bid_feed_url(config, auction_site_id)
    if not url:
        return {}
    payload = fetcher.get_json(url)
    return parse_bid_feed(payload)


def parse_bid_feed(payload: Any) -> dict[str, dict[str, Any]]:
    """Normalise the feed into ``{lot_site_id: entry}``."""
    if isinstance(payload, dict):
        payload = payload.get("lots") or payload.get("data") or []
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in payload:
        if isinstance(entry, dict) and entry.get("id") is not None:
            out[str(entry["id"])] = entry
    return out


def _met_reserve(value: Any) -> bool | None:
    """The feed uses "Y"/"N" or 0/1 inconsistently."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"y", "yes", "true", "1"}:
            return True
        if low in {"n", "no", "false", "0"}:
            return False
        return None
    return bool(value)


def apply_bid_feed(
    auction: Auction, feed: dict[str, dict[str, Any]], *, trust_feed: bool = True
) -> int:
    """Overlay feed values onto the scraped lots. Returns lots updated."""
    if not feed:
        return 0
    updated = 0
    for lot in auction.lots:
        entry = feed.get(lot.site_id)
        if not entry:
            continue
        updated += 1
        bid = parse_money(entry.get("max_bid"))
        count = parse_int(entry.get("num_bids"), 0)
        if trust_feed or lot.current_bid is None:
            lot.current_bid = bid
        if trust_feed or not lot.bid_count:
            lot.bid_count = count

        highest = clean_text(entry.get("highest") or "")
        lot.highest_bidder_id = highest
        under = entry.get("under") or []
        if isinstance(under, list):
            sequence = [clean_text(str(b)) for b in under if clean_text(str(b))]
            # ``under`` is oldest-first; the current winner tops the sequence.
            if highest:
                sequence = sequence + [highest]
            lot.bidder_sequence = sequence

        reserve = _met_reserve(entry.get("met_reserve"))
        if reserve is not None:
            lot.met_reserve = reserve

        closes = from_epoch(entry.get("bids_close_time"))
        if closes:
            lot.closes_at = closes
        opens = from_epoch(entry.get("bids_start_time"))
        if opens:
            lot.opens_at = opens

    # Keep the auction's own window consistent with its lots.
    closes = [lot.closes_at for lot in auction.lots if lot.closes_at]
    if closes:
        auction.end_at = max(closes)
    opens = [lot.opens_at for lot in auction.lots if lot.opens_at]
    if opens:
        auction.start_at = min(opens)
    log.debug(
        "applied bid feed",
        extra={"auction": auction.url, "lots_updated": updated, "feed_size": len(feed)},
    )
    return updated


# ---------------------------------------------------------------------------
# Lot gallery (original photos)
# ---------------------------------------------------------------------------


def lot_gallery_url(config: Config, lot_site_id: str) -> str:
    template = str(config.get("site.lot_gallery_url", ""))
    return template.format(lot_site_id=lot_site_id) if template else ""


def fetch_lot_gallery(
    fetcher: Fetcher, config: Config, lot_site_id: str
) -> list[dict[str, Any]]:
    """Photos for one lot: ``[{href, thumbnail, title}, ...]``."""
    if not lot_site_id:
        return []
    url = lot_gallery_url(config, lot_site_id)
    if not url:
        return []
    payload = fetcher.get_json(url)
    return parse_gallery(payload)


def parse_gallery(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("images") or payload.get("data") or []
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        href = clean_text(item.get("href") or item.get("url") or "")
        if not href:
            continue
        out.append(
            {
                "href": href,
                "thumbnail": clean_text(item.get("thumbnail") or ""),
                "title": clean_text(item.get("title") or ""),
            }
        )
    return out


def enrich_lot_images(
    fetcher: Fetcher,
    config: Config,
    lots: Iterable[Lot],
    *,
    max_lots: int | None = None,
) -> int:
    """Populate ``image_urls`` (originals) and ``thumbnail_urls`` per lot.

    Only lots whose photo set is not yet known are fetched, so repeat cycles
    cost nothing. Returns the number of lots enriched.
    """
    limit = int(config.get("images.max_images_per_lot", 40))
    enriched = 0
    for lot in lots:
        if max_lots is not None and enriched >= max_lots:
            break
        # Already have every photo we expect? Skip the request.
        if lot.image_urls and (
            not lot.image_count or len(lot.image_urls) >= min(lot.image_count, limit)
        ):
            continue
        gallery = fetch_lot_gallery(fetcher, config, lot.site_id)
        if not gallery:
            continue
        originals: list[str] = []
        thumbs: list[str] = list(lot.thumbnail_urls)
        for item in gallery[:limit]:
            if item["href"] not in originals:
                originals.append(item["href"])
            if item["thumbnail"] and item["thumbnail"] not in thumbs:
                thumbs.append(item["thumbnail"])
        lot.image_urls = originals
        lot.thumbnail_urls = thumbs
        lot.image_count = max(lot.image_count, len(originals))
        # The gallery title is the full lot name, untruncated by the HTML.
        title = gallery[0].get("title") or ""
        if title:
            lot.extra["gallery_title"] = title
            if not lot.description or len(title) > len(lot.description):
                cleaned = title.split(":", 1)[-1].strip() if ":" in title else title
                if cleaned and not cleaned.endswith("..."):
                    lot.description = cleaned
        enriched += 1
    return enriched
