"""Index-page parser and pagination tests."""
from __future__ import annotations

import pytest

from auction_tracker.scrape import crawl_index, index_validator, parse_index_page
from auction_tracker.fetch import FetchResult


def test_parse_index_page_finds_auctions(index_page_1_html: str, config):
    page = parse_index_page(index_page_1_html, config)
    assert len(page.auctions) == 10
    # Every card has a detail URL matching /auctions/YYYY/MM/DD/<slug>.html
    for auction in page.auctions:
        assert "/auctions/" in auction.url
        assert auction.url.endswith(".html")
        assert auction.title, "title should not be empty"


def test_parse_index_page_reads_pager_config(index_page_1_html: str, config):
    page = parse_index_page(index_page_1_html, config)
    # The fixture's inline JS advertises totalPages: 4 and a href template.
    assert page.total_pages == 4
    assert page.pagination_template is not None
    assert "{page}" in page.pagination_template
    assert page.has_pager


def test_parse_index_page_empty_has_no_auctions(index_page_empty_html: str, config):
    page = parse_index_page(index_page_empty_html, config)
    assert page.auctions == []


def test_index_validator_accepts_real_page(index_page_1_html: str):
    result = FetchResult(url="x", status=200, text=index_page_1_html)
    assert index_validator(result) is True


def test_index_validator_rejects_genuine_empty_page(index_page_empty_html: str):
    # The empty fixture has no pagination container, so the validator rejects it.
    # (A real empty page from the site would have the pagination container.)
    result = FetchResult(url="x", status=200, text=index_page_empty_html)
    assert index_validator(result) is False


def test_index_validator_rejects_blank():
    assert index_validator(FetchResult(url="x", status=200, text="")) is False


# ---------------------------------------------------------------------------
# crawl_index — pagination termination behaviour
# ---------------------------------------------------------------------------


def _seed_pages(fetcher, pages: dict[str, str]) -> None:
    for url, html in pages.items():
        fetcher.add(url, text=html)


def test_crawl_index_normal_termination(fake_fetcher, config, index_page_1_html, index_page_2_html, index_page_empty_html):
    base = "https://auctions.com.au"
    index = f"{base}/auctions/online"
    page2 = f"{base}/auctions/online/10/31/2?"
    page3 = f"{base}/auctions/online/10/31/3?"
    _seed_pages(fake_fetcher, {
        index: index_page_1_html,
        page2: index_page_2_html,
        page3: index_page_empty_html,  # page 3 is empty -> stop
    })
    auctions, pages, diag = crawl_index(fake_fetcher, config)
    assert pages == 3
    assert len(auctions) == 20  # 10 + 10 + 0
    assert "no auctions" in diag["termination_reason"]


def test_crawl_index_empty_first_page_terminates(fake_fetcher, config, index_page_empty_html):
    fake_fetcher.add("https://auctions.com.au/auctions/online", text=index_page_empty_html)
    auctions, pages, diag = crawl_index(fake_fetcher, config)
    assert pages == 1
    assert auctions == []
    assert "no auctions" in diag["termination_reason"]


def test_crawl_index_duplicate_first_url_loop_guard(fake_fetcher, config, index_page_1_html):
    # Page 2 returns the SAME first URL as page 1 -> loop guard fires.
    base = "https://auctions.com.au"
    fake_fetcher.add(f"{base}/auctions/online", text=index_page_1_html)
    fake_fetcher.add(f"{base}/auctions/online/10/31/2?", text=index_page_1_html)
    auctions, pages, diag = crawl_index(fake_fetcher, config)
    assert pages == 2
    assert "loop guard" in diag["termination_reason"]
    # Page 1's auctions are preserved even though page 2 was a loop.
    assert len(auctions) == 10


def test_crawl_index_safety_cap(fake_fetcher, config):
    # Use minimal pages with no totalPages advertised and different first URLs
    # so neither the total_pages check nor the loop guard fires.
    config._tree["site"]["max_pages_safety_cap"] = 3
    base = "https://auctions.com.au"
    # Each page has one unique auction card and no pager config.
    for n in range(1, 6):
        url = f"{base}/auctions/online" if n == 1 else f"{base}/auctions/online/10/31/{n}?"
        html = (
            '<html><body>'
            f'<div class="listing-post online">'
            f'<h3 class="post-title"><a href="/auctions/2026/08/2{n}/unique-auction-{n}.html">'
            f'Auction {n}</a></h3>'
            '</div>'
            '</body></html>'
        )
        fake_fetcher.add(url, text=html)
    auctions, pages, diag = crawl_index(fake_fetcher, config)
    assert pages == 3
    assert "safety_cap" in diag["termination_reason"] or "safety cap" in diag["termination_reason"].lower()
    assert len(auctions) == 3


def test_crawl_index_non_retryable_error(fake_fetcher, config, index_page_1_html):
    from auction_tracker.fetch import FetchError
    base = "https://auctions.com.au"
    fake_fetcher.add(f"{base}/auctions/online", text=index_page_1_html)
    # Page 2 raises a 404 (non-retryable) -> stop, keep page 1's auctions.
    def _boom(url, **kwargs):
        if "10/31/2" in url:
            raise FetchError(url, "HTTP 404", status=404)
        return fake_fetcher._fetch_once(url, **kwargs)
    fake_fetcher._fetch_once, _orig = _boom, fake_fetcher._fetch_once
    # Restore the original for the non-error path.
    def patched(url, *, binary=False):
        if "10/31/2" in url:
            raise FetchError(url, "HTTP 404", status=404)
        return _orig(url, binary=binary)
    fake_fetcher._fetch_once = patched
    auctions, pages, diag = crawl_index(fake_fetcher, config)
    assert pages == 1
    assert len(auctions) == 10
    assert "fetch error" in diag["termination_reason"]
