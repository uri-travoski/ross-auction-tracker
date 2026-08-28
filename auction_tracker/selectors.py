"""Central selector registry — the only file to edit when the site's HTML changes.

Every value below was verified against live pages of auctions.com.au (Ross's
Auctioneers & Valuers, Welshpool WA). Fixtures captured at the same time live in
``tests/fixtures/``, so after editing this file run ``python -m pytest`` (or
``python runtests.py``) to confirm the scrapers still parse.

Structure of the site, as observed
---------------------------------
INDEX  ``/auctions/online``
  Server-rendered cards, one per auction::

      <div class="listing-post online ...">
        <div class="post-img">
          <span class="label">ONLINE</span>
          <a href="/auctions/2026/08/21/<slug>.html"><img src="...300x200..."></a>
          <span class="online-auction-status in-progress">IN PROGRESS</span>
        <h3 class="post-title"><a href="...">Title</a></h3>
        <div class="post-location">Welshpool Complex<br>453 Orrong Road...</div>
        <div class="post-date">Ends <span class="time">04:00pm</span>
                                    <span class="date">Monday, 31/08/26</span></div>

  The pager itself is rendered by JavaScript (twbsPagination) into an empty
  ``<ul class="pagination">``, but its configuration is printed in an inline
  script on every page::

      $('#search-pagination-top,#search-pagination-bottom').twbsPagination({
          totalPages: 4, startPage: 1,
          href: '/auctions/online/10/31/{{number}}?', ...

  So the crawler reads ``totalPages`` and the ``href`` template straight off
  page 1 instead of hard-coding the item counts, and those URLs serve plain
  server-rendered HTML. A page past the end returns HTTP 200 with zero cards.

DETAIL  ``/auctions/YYYY/MM/DD/<slug>.html``
  Also fully server-rendered, including the whole catalogue (verified: 115 lots
  present in the initial response). Auction metadata is a ``<th>``/``<td>``
  table (Starts / Closes / Inspection / Location / Contact). Each lot is::

      <div class="post-wrapper catalogue" data-lot-id="2206305">
        <a class="lot-gallery-link" title="Images in this lot: 8"
           id="lot-2206305-gallery-link">
          <img class="lazy lot-thumb" src="spacer.gif" data-src="...300x200...">
          <span class="lot-image-count"> 8</span></a>
        <div class="cat-no"><a href=".../bid/1" data-lot-id="2206305">1</a></div>
        <div class="cat-no">Qty: 1</div>
        <div class="post-desc" data-short-desc="2 x EIZO RADIFORCE ...">
            2 x EIZO RADIFORCE RX340 ... MONITORS
          <div class="post-date" id="lot-2206305-remaining">
            <input class="lot-timestamp" id="lot-2206305-timestamp" value="1788172200">
            <strong id="lot-2206305-status">Bidding Closes in:</strong>
            <span id="lot-2206305-time">3 days 1 hr 47 mins</span>
        $<span id="lot-2206305-maxbid">24</span>
        (<span id="lot-2206305-bids">14</span> bids)

JSON FEEDS  (``static.auctions.com.au/cache/...``)
  The site's own front-end polls these every 5 seconds, and they are the
  authoritative source for bidding:

  * ``max_bids_<auctionId>.json`` — per lot: ``max_bid``, ``num_bids``,
    ``highest`` (bidder id), ``under[]`` (the losing-bidder sequence, i.e. the
    bid history), ``met_reserve``, ``bids_close_time``, ``bids_start_time``
    (epoch seconds, Perth time when rendered).
  * ``auction_lot_gallery_<lotId>.json`` — every photo for one lot, with
    ``href`` at 1500x1000 (the original) and ``thumbnail`` at 60x60.
  * ``auction_gallery_<auctionId>.json`` — the auction-level gallery.

  These caches persist for years after an auction closes (checked back to
  September 2025), which is what makes capturing final sale prices reliable.
"""

from __future__ import annotations

SELECTORS: dict[str, dict[str, str]] = {
    "index_page": {
        # One card per auction.
        "auction_card": "div.listing-post.online",
        # Within a card.
        "card_link": "a[href*='/auctions/']",
        "card_title_link": "h3.post-title a, h2.post-title a, .post-title a",
        "card_status": ".online-auction-status",
        "card_thumbnail": ".post-img img",
        "card_location": ".post-location",
        "card_date_block": ".post-date",
        "card_date_time": ".post-date .time",
        "card_date_date": ".post-date .date",
        # Empty container that JS fills; presence means the pager exists.
        "pagination_container": "ul.pagination",
    },
    "detail_page": {
        "title": "h1",
        "status": ".online-auction-status",
        "meta_table_row": ".listing-details table tr",
        "primary_image": "#listing-gallery-primary-link img",
        "gallery_thumb": "a.listing-gallery-thumbs-link img",
        "description_panel": "#information",
        "notes_panel": "#notes",
        "terms_panel": "#terms, #conditions",
        # Lots.
        "lot_row": "div.post-wrapper.catalogue[data-lot-id]",
        "lot_gallery_link": "a.lot-gallery-link",
        "lot_thumb": "img.lot-thumb",
        "lot_image_count": "span.lot-image-count",
        "lot_number_cell": "div.cat-no",
        "lot_number_link": "div.cat-no a[data-lot-id]",
        "lot_description": "div.post-desc",
        "lot_remaining_block": ".post-date",
        "lot_timestamp_input": "input.lot-timestamp",
        "lot_price_block": ".post-price",
    },
    # Attribute names, kept here so the parsers contain no magic strings.
    "attributes": {
        "lot_id": "data-lot-id",
        "lazy_src": "data-src",
        "short_desc": "data-short-desc",
        "gallery_title": "title",
        "timestamp_value": "value",
    },
    # Per-lot element id template: ``lot-<data-lot-id>-<kind>``.
    "lot_id_templates": {
        "status": "lot-{lot_id}-status",
        "time": "lot-{lot_id}-time",
        "bids": "lot-{lot_id}-bids",
        "maxbid": "lot-{lot_id}-maxbid",
        "reserve": "lot-{lot_id}-reserve",
        "button": "lot-{lot_id}-button",
        "timestamp": "lot-{lot_id}-timestamp",
        "remaining": "lot-{lot_id}-remaining",
        "gallery_link": "lot-{lot_id}-gallery-link",
    },
}

# Regexes used to read values the site only exposes inside inline JavaScript.
PATTERNS: dict[str, str] = {
    # $(...).twbsPagination({ totalPages: 4, ... })
    "total_pages": r"totalPages\s*:\s*(\d+)",
    # href: '/auctions/online/10/31/{{number}}?'
    "pagination_href": r"href\s*:\s*['\"]([^'\"]*\{\{number\}\}[^'\"]*)['\"]",
    # updateBids() polls https://.../cache/max_bids_14704.json
    "auction_site_id": r"max_bids_(\d+)\.json",
    # Fallback: image paths contain /public/auctions/<auctionId>/<imageId>.jpg
    "image_path_ids": r"/public/auctions/(\d+)/(\d+)\.",
    # Auction detail URL: /auctions/2026/08/21/<slug>.html
    "detail_url": r"/auctions/(\d{4})/(\d{2})/(\d{2})/([^/?#\"']+?)(?:\.html?|)$",
    # "Images in this lot: 8"
    "image_count": r"(\d+)",
    # "Qty: 3"
    "quantity": r"Qty\s*:?\s*(\d+)",
}

# Status text/class on cards and detail pages -> our canonical status.
STATUS_MAP: dict[str, str] = {
    "in-progress": "IN_PROGRESS",
    "inprogress": "IN_PROGRESS",
    "in progress": "IN_PROGRESS",
    "forthcoming": "FORTHCOMING",
    "upcoming": "FORTHCOMING",
    "closed": "CLOSED",
    "finished": "CLOSED",
    "ended": "CLOSED",
    "sold": "CLOSED",
}

# Lot status text -> canonical bidding status.
LOT_STATUS_MAP: dict[str, str] = {
    "bidding closes in": "ACTIVE",
    "bidding opens in": "ACTIVE",
    "bidding closed": "CLOSED",
    "closed": "CLOSED",
    "sold": "CLOSED",
    "withdrawn": "WITHDRAWN",
    "removed": "WITHDRAWN",
}
