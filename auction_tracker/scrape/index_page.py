"""Auction index parsing and exhaustive pagination.

``parse_index_page`` is pure (HTML in, dataclasses out) so it can be tested
against the captured fixtures. ``crawl_index`` adds the network walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import Config
from ..fetch import Fetcher, FetchError, FetchResult
from ..logging_setup import get_logger
from ..models import Auction
from ..selectors import PATTERNS, SELECTORS, STATUS_MAP
from ..util import clean_text, parse_site_datetime, slugify

log = get_logger(__name__)

S = SELECTORS["index_page"]


@dataclass
class IndexPage:
    """Result of parsing one page of the auction index."""

    auctions: list[Auction] = field(default_factory=list)
    total_pages: int | None = None
    pagination_template: str | None = None
    has_pager: bool = False

    @property
    def urls(self) -> list[str]:
        return [a.url for a in self.auctions]


def _canonical_url(href: str, base_url: str) -> str:
    """Absolute URL for an auction, with ``#catalogue`` and query stripped."""
    absolute = urljoin(base_url, href.strip())
    parsed = urlparse(absolute)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _slug_from_url(url: str) -> str:
    match = re.search(PATTERNS["detail_url"], urlparse(url).path)
    if match:
        return match.group(4)
    return slugify(urlparse(url).path.rsplit("/", 1)[-1] or url)


def _is_detail_url(url: str) -> bool:
    return bool(re.search(r"/auctions/\d{4}/\d{2}/\d{2}/", urlparse(url).path))


def _status_from_class(node) -> str:
    """Map ``<span class="online-auction-status in-progress">`` to our status."""
    if node is None:
        return ""
    classes = " ".join(node.get("class", []))
    for token, status in STATUS_MAP.items():
        if token in classes.lower():
            return status
    return STATUS_MAP.get(clean_text(node.get_text()).lower(), "")


def parse_index_page(html: str, config: Config) -> IndexPage:
    """Parse one index page into auctions plus the pager configuration."""
    base_url = str(config.get("site.base_url", "https://auctions.com.au"))
    tz = config.timezone
    soup = BeautifulSoup(html, "lxml")
    page = IndexPage()

    # --- pager configuration, printed in inline JS on every page
    # Use .get_text() instead of .string because .string returns None when
    # a <script> tag contains comments or multiple child nodes (which newer
    # lxml/bs4 versions handle differently).
    scripts = " ".join(
        s.get_text() or "" for s in soup.find_all("script") if not s.get("src")
    )
    total = re.search(PATTERNS["total_pages"], scripts)
    if total:
        page.total_pages = int(total.group(1))
    href = re.search(PATTERNS["pagination_href"], scripts)
    if href:
        page.pagination_template = href.group(1).replace("{{number}}", "{page}")
    page.has_pager = bool(soup.select(S["pagination_container"]))

    # --- auction cards
    seen: set[str] = set()
    for card in soup.select(S["auction_card"]):
        auction = _parse_card(card, base_url, tz)
        if auction and auction.url not in seen:
            seen.add(auction.url)
            page.auctions.append(auction)

    if not page.auctions:
        # Structure may have changed: fall back to any auction-detail anchors.
        for anchor in soup.select("a[href]"):
            url = _canonical_url(anchor.get("href", ""), base_url)
            if _is_detail_url(url) and url not in seen:
                seen.add(url)
                page.auctions.append(
                    Auction(
                        url=url,
                        title=clean_text(anchor.get_text()) or _slug_from_url(url),
                        slug=_slug_from_url(url),
                    )
                )
        if page.auctions:
            log.warning(
                "index card selector matched nothing; used anchor fallback",
                extra={"found": len(page.auctions)},
            )
    return page


def _parse_card(card, base_url: str, tz) -> Auction | None:
    # Title link first; fall back to any auction-detail anchor in the card.
    link = card.select_one(S["card_title_link"])
    url = ""
    title = ""
    if link and link.get("href"):
        url = _canonical_url(link["href"], base_url)
        title = clean_text(link.get_text())
    if not _is_detail_url(url):
        for anchor in card.select(S["card_link"]):
            candidate = _canonical_url(anchor.get("href", ""), base_url)
            if _is_detail_url(candidate):
                url = candidate
                title = title or clean_text(anchor.get("title") or anchor.get_text())
                break
    if not url or not _is_detail_url(url):
        return None

    auction = Auction(url=url, title=title, slug=_slug_from_url(url))
    auction.status = _status_from_class(card.select_one(S["card_status"]))

    thumb = card.select_one(S["card_thumbnail"])
    if thumb:
        auction.thumbnail_url = clean_text(
            thumb.get("data-src") or thumb.get("src") or ""
        )

    location = card.select_one(S["card_location"])
    if location:
        auction.location = clean_text(location.get_text(separator=", "))

    # The card shows either "Starts <time> <date>" or "Ends <time> <date>".
    block = card.select_one(S["card_date_block"])
    if block:
        label = clean_text(block.get_text()).lower()
        time_node = block.select_one(".time")
        date_node = block.select_one(".date")
        parsed = parse_site_datetime(
            clean_text(time_node.get_text()) if time_node else "",
            clean_text(date_node.get_text()) if date_node else "",
            tz,
        )
        if parsed:
            if label.startswith("start") or "starts" in label:
                auction.start_at = parsed
            else:
                auction.end_at = parsed
    return auction


def index_validator(result: FetchResult) -> bool:
    """Used by the auto-fetcher: does this response actually contain cards?

    A page beyond the last one legitimately has none, so an explicit
    "no more auctions" marker also counts as usable.
    """
    text = result.text or ""
    if not text:
        return False
    if 'class="listing-post' in text:
        return True
    # Genuine empty page: the pager container is present but no cards are.
    return 'class="pagination"' in text and "listing-post" not in text


def crawl_index(
    fetcher: Fetcher, config: Config
) -> tuple[list[Auction], int, dict[str, object]]:
    """Walk every page of the auction index.

    Termination (whichever comes first):

    1. A page yields zero auctions — the site's honest end-of-list signal.
    2. ``total_pages`` from the page's own pager config is reached.
    3. The first URL on a page repeats one already seen (loop guard).
    4. A non-retryable HTTP error.
    5. ``site.max_pages_safety_cap`` pages have been fetched.

    Returns ``(auctions, pages_scanned, diagnostics)``.
    """
    base_url = str(config.get("site.base_url", "https://auctions.com.au"))
    index_url = str(config.get("site.index_url", f"{base_url}/auctions/online"))
    cap = int(config.get("site.max_pages_safety_cap", 50))
    fallback_template = str(config.get("site.pagination_url_template", ""))

    auctions: list[Auction] = []
    by_url: dict[str, Auction] = {}
    visited: list[str] = []
    first_urls: set[str] = set()
    template: str | None = None
    total_pages: int | None = None
    reason = "unknown"
    pages = 0

    page_num = 1
    while pages < cap:
        url = index_url if page_num == 1 else _page_url(
            base_url, template or fallback_template, page_num
        )
        if not url:
            reason = "no pagination template available"
            break
        try:
            result = fetcher.fetch(url, validator=index_validator)
        except FetchError as exc:
            reason = f"fetch error on page {page_num}: {exc.message}"
            log.error(reason, extra={"url": url})
            break

        pages += 1
        visited.append(url)
        parsed = parse_index_page(result.text, config)
        if page_num == 1:
            template = parsed.pagination_template or fallback_template
            total_pages = parsed.total_pages

        if not parsed.auctions:
            reason = f"page {page_num} contained no auctions"
            break

        first = parsed.urls[0]
        if first in first_urls:
            reason = f"loop guard: page {page_num} repeats first URL {first}"
            log.warning(reason)
            break
        first_urls.add(first)

        for auction in parsed.auctions:
            if auction.url in by_url:
                _merge(by_url[auction.url], auction)
            else:
                by_url[auction.url] = auction
                auctions.append(auction)

        log.info(
            "index page scanned",
            extra={
                "page": page_num,
                "found": len(parsed.auctions),
                "cumulative": len(auctions),
                "fetcher": result.fetcher,
            },
        )

        if total_pages and page_num >= total_pages:
            reason = f"reached total_pages={total_pages} advertised by the site"
            break
        page_num += 1
    else:
        reason = f"hit max_pages_safety_cap={cap}"
        log.warning(reason)

    diagnostics = {
        "termination_reason": reason,
        "pages_scanned": pages,
        "total_pages_advertised": total_pages,
        "pagination_template": template,
        "visited": visited,
    }
    log.info(
        "index crawl complete",
        extra={"auctions": len(auctions), "pages": pages, "reason": reason},
    )
    return auctions, pages, diagnostics


def _page_url(base_url: str, template: str, page_num: int) -> str:
    if not template:
        return ""
    path = template.replace("{page}", str(page_num))
    # Don't use urljoin here: it strips a trailing "?" (empty query) on some
    # Python versions, which breaks the site's pagination URLs.
    if path.startswith(("http://", "https://")):
        return path
    base = base_url.rstrip("/")
    return f"{base}{path}" if path.startswith("/") else f"{base}/{path}"


def _merge(target: Auction, other: Auction) -> None:
    """Fill blanks on ``target`` from ``other`` (same auction, second sighting)."""
    for attr in (
        "title",
        "status",
        "location",
        "thumbnail_url",
        "slug",
        "start_at",
        "end_at",
    ):
        if not getattr(target, attr) and getattr(other, attr):
            setattr(target, attr, getattr(other, attr))
