"""Auction detail-page parsing: auction metadata plus every lot.

Pure functions — HTML in, dataclasses out — so the whole parser is testable
against ``tests/fixtures/detail_page_it.html.gz``.
"""

from __future__ import annotations

import copy
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..logging_setup import get_logger
from ..models import ACTIVE, Auction, Lot
from ..selectors import LOT_STATUS_MAP, PATTERNS, SELECTORS, STATUS_MAP
from ..util import (
    clean_text,
    from_epoch,
    parse_int,
    parse_money,
    parse_site_datetime,
    truncate,
)

log = get_logger(__name__)

S = SELECTORS["detail_page"]
A = SELECTORS["attributes"]
T = SELECTORS["lot_id_templates"]


def detail_validator(result) -> bool:
    """Auto-fetcher check: did we get a page with a catalogue on it?"""
    text = getattr(result, "text", "") or ""
    return bool(text) and (A["lot_id"] in text or "catalogue" in text)


def parse_detail_page(html: str, url: str, config: Config) -> Auction:
    """Parse an auction detail page. Lots are attached as ``auction.lots``."""
    tz = config.timezone
    base_url = str(config.get("site.base_url", "https://auctions.com.au"))
    soup = BeautifulSoup(html, "lxml")

    auction = Auction(url=url)
    title = soup.select_one(S["title"])
    auction.title = clean_text(title.get_text()) if title else ""

    # Detail pages carry no status badge (verified: the ``online-auction-status``
    # span only exists on index cards), so the status is normally derived from
    # the parsed start/close times at the end of this function. The lookup is
    # kept for the case where the site starts emitting one.
    status_node = soup.select_one(S["status"])
    status_from_badge = ""
    if status_node:
        classes = " ".join(status_node.get("class", [])).lower()
        text = clean_text(status_node.get_text()).lower()
        for token, mapped in STATUS_MAP.items():
            if token in classes or token == text:
                status_from_badge = mapped
                break

    # The site's internal auction id, taken from the bid-feed URL its own
    # JavaScript polls; image paths are the fallback.
    scripts = " ".join(
        s.string or "" for s in soup.find_all("script") if not s.get("src")
    )
    site_id = re.search(PATTERNS["auction_site_id"], scripts)
    if site_id:
        auction.site_id = site_id.group(1)
    else:
        from_image = re.search(PATTERNS["image_path_ids"], html)
        if from_image:
            auction.site_id = from_image.group(1)

    _parse_meta_table(soup, auction, tz)
    _parse_text_panels(soup, auction)

    primary = soup.select_one(S["primary_image"])
    if primary:
        auction.thumbnail_url = clean_text(
            primary.get(A["lazy_src"]) or primary.get("src") or ""
        )

    auction.lots = [
        lot
        for lot in (
            _parse_lot(row, base_url, tz) for row in soup.select(S["lot_row"])
        )
        if lot is not None
    ]
    auction.lot_count = len(auction.lots)

    # Auction close time is the latest lot close time when the header is vague.
    lot_closes = [lot.closes_at for lot in auction.lots if lot.closes_at]
    if lot_closes:
        latest = max(lot_closes)
        if auction.end_at is None or latest > auction.end_at:
            auction.end_at = latest
    lot_opens = [lot.opens_at for lot in auction.lots if lot.opens_at]
    if lot_opens and auction.start_at is None:
        auction.start_at = min(lot_opens)

    auction.status = status_from_badge or auction.computed_status()

    log.debug(
        "parsed detail page",
        extra={"url": url, "lots": auction.lot_count, "site_id": auction.site_id},
    )
    return auction


def _parse_meta_table(soup: BeautifulSoup, auction: Auction, tz) -> None:
    """Read the Starts / Closes / Inspection / Location / Contact rows."""
    for row in soup.select(S["meta_table_row"]):
        header = row.find("th")
        cell = row.find("td")
        if not header or not cell:
            continue
        label = clean_text(header.get_text()).lower().rstrip(":")
        time_node = cell.select_one(".time")
        date_node = cell.select_one(".date")
        when = parse_site_datetime(
            clean_text(time_node.get_text()) if time_node else "",
            clean_text(date_node.get_text()) if date_node else "",
            tz,
        )
        value = clean_text(cell.get_text(separator=", "))
        if label.startswith("start") or label.startswith("open"):
            auction.start_at = when or auction.start_at
        elif label.startswith("close") or label.startswith("end"):
            auction.end_at = when or auction.end_at
        elif label.startswith("inspection"):
            auction.inspection = value
        elif label.startswith("location"):
            auction.location = value
        elif label.startswith("contact"):
            auction.contact = value
        elif label.startswith("collection"):
            auction.collection = value


def _panel_text(soup: BeautifulSoup, selector: str) -> str:
    """Text of a tab panel with the image gallery and lot rows removed."""
    node = soup.select_one(selector)
    if node is None:
        return ""
    clone = copy.copy(node)
    for junk in clone.select(
        ".listing-gallery, .listing-gallery-thumbs, script, style,"
        " div.post-wrapper.catalogue, .listing-details, table"
    ):
        junk.decompose()
    return clean_text(clone.get_text(separator="\n"))


def _parse_text_panels(soup: BeautifulSoup, auction: Auction) -> None:
    auction.description = _panel_text(soup, S["description_panel"])
    notes = _panel_text(soup, S["notes_panel"])
    terms = _panel_text(soup, S["terms_panel"])
    auction.terms = terms or notes
    if notes and terms and notes != terms:
        auction.extra["notes"] = notes
    # "Collection" appears as an <h2> section in the information panel.
    for heading in soup.select("#information h2, #information h3"):
        if "collection" in clean_text(heading.get_text()).lower():
            following = heading.find_next("p")
            if following:
                auction.collection = clean_text(following.get_text())
            break


def _lot_element_text(row, lot_id: str, kind: str) -> str:
    """Text of ``#lot-<id>-<kind>`` within this lot row."""
    element_id = T[kind].format(lot_id=lot_id)
    node = row.find(id=element_id) or row.select_one(f"#{element_id}")
    return clean_text(node.get_text()) if node else ""


def _parse_lot(row, base_url: str, tz) -> Lot | None:
    lot_id = clean_text(row.get(A["lot_id"]) or "")
    if not lot_id:
        return None

    lot = Lot(lot_number="", site_id=lot_id)

    # --- lot number and bid URL
    number_link = row.select_one(S["lot_number_link"])
    if number_link:
        lot.lot_number = clean_text(number_link.get_text())
        href = number_link.get("href")
        if href:
            lot.bid_url = urljoin(base_url, href)
    if not lot.lot_number:
        # Fall back to the id of the anchor: id="lot-number-12"
        anchor = row.select_one("a[id^='lot-number-']")
        if anchor:
            lot.lot_number = anchor.get("id", "").rsplit("-", 1)[-1]
    if not lot.lot_number:
        lot.lot_number = lot_id

    # --- quantity: the second .cat-no cell reads "Qty: 3"
    for cell in row.select(S["lot_number_cell"]):
        text = clean_text(cell.get_text())
        match = re.search(PATTERNS["quantity"], text, re.I)
        if match:
            lot.quantity = max(1, int(match.group(1)))
            break
        if "qty" in text.lower():
            digits = re.search(r"(\d+)", text)
            if digits:
                lot.quantity = max(1, int(digits.group(1)))
                break

    # --- images (thumbnail here; originals come from the gallery feed)
    thumb = row.select_one(S["lot_thumb"])
    if thumb:
        lot.thumbnail_url = clean_text(thumb.get(A["lazy_src"]) or thumb.get("src") or "")
        if lot.thumbnail_url:
            lot.thumbnail_urls = [lot.thumbnail_url]
    count_node = row.select_one(S["lot_image_count"])
    if count_node:
        lot.image_count = parse_int(clean_text(count_node.get_text()), 0)
    if not lot.image_count:
        gallery = row.select_one(S["lot_gallery_link"])
        if gallery:
            # title="Images in this lot: 8"
            match = re.search(r"(\d+)", gallery.get(A["gallery_title"], "") or "")
            if match:
                lot.image_count = int(match.group(1))
    if not lot.image_count and lot.thumbnail_url:
        lot.image_count = 1

    # --- description
    desc_node = row.select_one(S["lot_description"])
    if desc_node:
        lot.short_description = clean_text(desc_node.get(A["short_desc"]) or "")
        clone = copy.copy(desc_node)
        for junk in clone.select(".post-date, .row, input, script, style"):
            junk.decompose()
        lot.description = clean_text(clone.get_text(separator=" "))
    if not lot.description:
        lot.description = lot.short_description
    if not lot.short_description:
        lot.short_description = truncate(lot.description, 120)

    # --- bidding
    lot.current_bid = parse_money(_lot_element_text(row, lot_id, "maxbid"))
    lot.bid_count = parse_int(_lot_element_text(row, lot_id, "bids"), 0)
    lot.status_label = _lot_element_text(row, lot_id, "status")
    lot.time_remaining = _lot_element_text(row, lot_id, "time")
    reserve = _lot_element_text(row, lot_id, "reserve")
    if reserve:
        lot.extra["reserve_note"] = reserve
        low = reserve.lower()
        if "met" in low:
            lot.met_reserve = "not" not in low

    lot.bidding_status = ACTIVE
    label = lot.status_label.lower().rstrip(":")
    for token, mapped in LOT_STATUS_MAP.items():
        if label.startswith(token) or token in label:
            lot.bidding_status = mapped
            break

    # --- close time: hidden input carries epoch seconds
    stamp_node = row.select_one(S["lot_timestamp_input"])
    if stamp_node:
        closes = from_epoch(stamp_node.get(A["timestamp_value"]))
        if closes:
            if "opens" in label:
                lot.opens_at = closes
            else:
                lot.closes_at = closes
    return lot
