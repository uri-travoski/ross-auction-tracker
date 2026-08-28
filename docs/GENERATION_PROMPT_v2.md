# Auction Tracker Agent — Generation Prompt (v2)

> **Purpose of this document.** This is a self-contained, AI-ready specification.
> Paste it (or point an AI coding agent at it) to generate the full
> `auction-tracker` codebase. The codebase produced from this prompt MUST be
> self-documenting, config-driven, and structured so that a future AI agent
> can read it, understand every requirement in this prompt, and extend it
> (add a proxy, swap the HTTP client, update selectors, add categories, etc.)
> without re-deriving the original design.
>
> **v2 changes from v1** — see §16 for the full change list. Headline:
> site recon revealed that the auction site is fully server-rendered,
> so the default fetcher is stdlib HTTP (not Playwright). The T&Cs modal,
> the JS-loaded "CATALOGUE" tab, and the path-based pagination quirks
> are all handled. A new **30-minutes-before-end reminder** notification
> has been added per operator request.

---

## 1. Mission

Build a long-running agent that monitors **https://auctions.com.au/auctions/online** for
upcoming and in-progress **IT-related online auctions** (PCs, laptops, phones,
tablets, screens/displays, printers, networking gear, and similar electronics),
keeps a historical record of every lot in every tracked auction, notifies the
operator on important events, and exposes a downloadable archive of all
captured data.

The agent must run unattended for weeks/months, survive site changes, and
remain maintainable by another AI agent that has only this document and the
codebase to work from.

### 1.1 Non-goals (explicitly out of scope)

- Automated bidding. The agent only **observes** and **reports**.
- Authentication / login to the auction site. The public listings pages are
  assumed sufficient.
- Selling-side data (seller tools, invoicing, payouts).
- Tracking non-IT auctions. The category filter is mandatory; non-IT
  auctions are discarded and not stored.
- Email/SMS/Lark notifications. Mavis chat is the notification channel
  (the Docker deployment uses Mavis chat the same way; the user can wrap
  the notifier module to add other channels later — see §10).
- A graphical user interface. The HTML report + Drive + chat is the UI.

---

## 2. Functional Requirements

### 2.1 Auction discovery (24-hour cycle)

- **Every 24 hours**, scrape `https://auctions.com.au/auctions/online`.
- **Pagination is mandatory and exhaustive.** The site uses path-based
  pagination, not query params. The URL pattern is:

  ```
  https://auctions.com.au/auctions/online/{pageSize}/{totalCount}/{pageNum}?
  ```

  where `{pageSize}` is items per page (currently 10), `{totalCount}` is
  the total item count (currently 19, but the scraper MUST NOT rely on
  this value — treat it as opaque), and `{pageNum}` is 1-indexed.

  The crawler walks `pageNum = 1, 2, 3, ...` and stops on any of:
  - The page returns 0 auction URLs (server returns 200 with no
    auction-detail links; this is the legitimate "no more pages" signal).
  - The first auction URL on the current page is identical to the
    first URL on any prior page (loop guard against infinite walks).
  - The server returns a non-retryable HTTP error (4xx).
  - A configurable `max_pages_safety_cap` is hit (default 50; the
    agent logs a warning and stops, so a malformed site can't
    make the agent run forever).

  A `pages_scanned` counter and the list of visited URLs MUST be logged
  every cycle. **Standard query params (`?page=2`, `?p=2`, `?paged=2`,
  `?start=2`) do NOT work** — do not use them.

- For each auction card on each page, extract: title, URL, status badge
  (`in-progress` / `forthcoming` / `closed`), start date, end date, location.
- Apply the **IT category filter** (see §2.2). Non-matching auctions are
  discarded.
- For each **new** matching auction (URL not in the database), create an
  auction record and **notify the operator** via Mavis chat with a link to
  the auction page.
- For each **known** matching auction, refresh the metadata (start/end
  dates can change on the source site; treat the latest scrape as truth).

### 2.2 IT category filter

The agent must treat an auction as "IT-related" if **any** of the following
are true (case-insensitive, match against title + description text):

- Title or description contains any of these keywords: `laptop`, `notebook`,
  `tablet`, `iphone`, `ipad`, `phone`, `smartphone`, `pc`, `desktop`,
  `mini pc`, `sff`, `workstation`, `monitor`, `display`, `screen`,
  `printer`, `scanner`, `network`, `cisco`, `server`, `imac`, `macbook`,
  `surface`, `thinkpad`, `elitebook`, `zbook`, `probook`, `toughpad`,
  `toughbook`, `android`, `pixel`, `galaxy tab`.
- Title or description contains the word `IT` used as a category marker
  (e.g., "IT Online Auction", "General IT").
- The auction URL slug matches a known IT slug pattern
  (e.g., contains `it-online`, `laptop`, `notebook`, `tablet`, `smartphone`).
- An auction that contains `General IT` in the title is always included,
  even if the description is sparse.

The keyword list is **config-driven** (see §5.2). Adding a new category
must be a one-line config change.

The filter is applied in **two passes** for accuracy:
1. **Cheap pre-filter at index time** (just title + card text on the
   listing page). Most non-IT auctions are dropped here.
2. **Final confirmation at detail-page time** (full description, lot
   names, slug). Some IT auctions hide behind non-IT titles (e.g., a
   "Medical Equipment" auction that contains IT lots as a subset);
   the agent MUST scrape the detail page to confirm before storing.

### 2.3 Auction detail scraping (6-hour cycle for active auctions)

- **Every 6 hours**, for every auction in the database whose status is
  `in-progress` (i.e., between start and end), scrape the auction detail
  page.
- For each lot on the detail page, extract: lot ID (`data-lot-id`),
  lot number (display number), quantity, description, thumbnail URL,
  full-size image URL(s), current bid amount, bid count, time remaining,
  bidding status.
- Diff against the previous snapshot for that auction. If anything changed
  (lot added, lot removed, bid amount changed, bid count changed,
  description changed, image changed), record a change event and highlight
  the change in the report.
- **Notify the operator** on Mavis chat if any of these are observed in
  this cycle: a new lot appeared, a lot's bid jumped by more than 20%,
  a lot was withdrawn/removed, or the auction was paused/extended.
  (These "significant change" rules are config-driven; see §5.2.)

### 2.4 Final-stretch polling (30-minute cadence in last 3 hours)

- When an auction is **within 3 hours of its end time**, switch its poll
  cadence from 6 hours to **30 minutes**. This applies to **all lots** in
  the auction, not just a user watch list. The goal is to capture bidding
  behavior (bid count progression, last-minute jumps, extensions).
- At each 30-min tick, scrape, diff, and record. Notify on the same
  "significant change" rules as §2.3.
- If the auction page shows the auction has been **extended** (end time
  pushed back), update the end time, leave the 3-hour window based on
  the *new* end time, and notify the operator of the extension.
- This is the most operationally sensitive window. The scheduler MUST
  prioritize these ticks (see §6.4).

### 2.5 30-minute-before-end reminder (NEW in v2)

- **At exactly 30 minutes before an auction's end time**, the agent MUST
  send a chat notification to the operator summarizing that auction.
  This is independent of the 30-min cadence ticks (which scrape data
  every 30 min in the final 3 hours — the reminder is the final
  pre-close ping).
- The notification includes:
  - Auction title + URL.
  - End time (and whether it was extended).
  - Current lot count and total bid count across all lots.
  - Top 3 lots by current bid (lot #, description excerpt, current bid,
    bid count).
  - The 3 lots with the highest bid-jump-in-last-hour (largest
    percentage bid increase in the last hour) — these are the "hot" lots.
  - A one-line summary like "1 lot is at reserve, 4 lots have no bids yet".
- The reminder lead time is **configurable** via
  `notify.reminder_lead_minutes` in `config.yaml` (default 30). Valid
  values: any positive integer. The agent supports multiple lead times
  if configured (e.g., a 60-min reminder AND a 30-min reminder).
- A reminder is sent only **once per auction** (deduplication: track
  which (auction_id, lead_minutes) pairs have already been notified).
- The reminder must NOT be sent if the auction was extended past the
  scheduled end time and a new reminder is queued for the new end time.
- If the agent misses the exact 30-min mark (e.g., the scheduler was
  late), the reminder must still fire on the next tick that observes
  `now >= end_at - lead_minutes AND reminder_not_yet_sent AND
  status != 'finalized'`.
- The reminder is generated even if no bid data has changed since the
  last tick (a snapshot of state is enough).

### 2.6 Finalization (4 hours after end)

- **4 hours after an auction's end time**, perform a **final** scrape.
- After the final scrape, mark the auction `finalized` in the database.
- Generate a `final_<auction-id>.html` report and a `final_<auction-id>.json`
  archive in Mavis Drive. The HTML report is the human-readable "what
  actually happened" record: every lot, its final bid, the bid count,
  and a timeline of significant bid changes observed during the auction.
- Notify the operator that the auction has been finalized and the report
  is ready.
- **Do not** delete any data after finalization. The record is permanent.

### 2.7 Operator deliverables (per cycle)

Each cycle produces, at minimum:

- One **HTML report** in Mavis Drive, named
  `report_YYYY-MM-DD_HHMM.html`. The report shows:
  - All currently tracked IT auctions, grouped by status
    (`forthcoming`, `in-progress`, `closed-not-finalized`, `finalized`).
  - For each in-progress auction, the lots, current bids, and any
    changes since the last cycle (highlighted: new lots in green,
    bid changes in amber, removed lots in red, with a side-by-side
    diff where useful).
  - For each auction in the final 3h, a more prominent block with the
    most recent bid tick timestamp, a short trend line, and a clear
    countdown to end time.
  - For each auction within `reminder_lead_minutes` of end, a
    "REMINDER SENT" badge and the same summary the chat message
    contained.
  - Easily scannable: tables, generous whitespace, thumbnail-to-the-
    left-of-lot-name, clickable links to the lot images and to the
    auction page.
- A **per-auction JSON snapshot** in Drive, named
  `auction_<id>_<cycle-iso>.json`. One file per auction per cycle.
- A **bid-history row** in the SQLite database (see §4) for every lot
  whose bid amount or bid count changed.
- **Mavis chat notifications** per §2.8.

### 2.8 Notification rules (Mavis chat)

The agent sends a chat message to the operator on these events, and **only**
these events:

| Event | When | Message contents |
|---|---|---|
| New IT auction discovered | New URL in index page after IT filter | Title, URL, start/end dates, thumbnail link |
| Auction enters final-3h window | now crosses `end_at - 3h` | Title, URL, end time, current lot count |
| **Reminder (lead_minutes before end)** | **now crosses `end_at - lead_minutes`** | **Title, URL, end time, lot count, total bids, top-3 lots, hot-3 lots, no-bid summary** |
| Auction extended | end_at moves to a later time | Old end time, new end time, URL |
| Auction finalized | 4h after end | Title, URL, final lot count, total bids, link to final report |
| Significant change in a lot | Any cycle | Lot #, lot title, what changed (bid jump, new lot, etc.) |

The agent does **not** send a chat message for routine cycles where
nothing changed AND no reminder is due. The HTML report is the record
of silence.

All notification messages include a link to the auction page and, when
relevant, a link to the latest HTML report.

### 2.9 Archive download

- The agent must support an on-demand command `archive download` (or
  equivalent) that produces a single `.zip` file in Mavis Drive containing:
  - The full SQLite database file.
  - All per-auction JSON snapshots.
  - The current HTML report.
  - A `MANIFEST.md` listing every file with timestamps and a brief
    description.
- The zip must be downloadable to the operator's local PC.

---

## 3. Site Structure (Reference)

Based on live reconnaissance of `https://auctions.com.au/auctions/online`
(operator site: Ross's Auctioneers & Valuers, Welshpool WA, 2026-08-26):

- **Index page** (`/auctions/online/10/19/1?` etc.) lists online auctions
  as cards: thumbnail, status badge, title, location, start/end dates.
  **The page is fully server-rendered** — no JS required.
- Each auction has a detail page at
  `/auctions/YYYY/MM/DD/<slug>.html` (or `.htm`, no extension — both occur).
- The detail page is also fully server-rendered. **All lot data is in the
  initial HTML response**, despite the "CATALOGUE" tab and a T&Cs modal
  suggesting a JS-driven flow. The modal is a CSS overlay that visually
  blocks interaction in a browser but does not affect the underlying HTML.
- Lot identification uses `data-lot-id="XXXXXX"` attributes (one per lot,
  repeated on multiple elements: anchor, button, status span, time span,
  bids span, maxbid span, etc.).
- Buyer's premium and other terms are stated on the detail page but are
  not part of the lot-level data.

**Important:** the exact HTML structure, CSS class names, and URL patterns
are version-specific and may change. The agent MUST NOT hard-code selectors
in scraping logic; selectors live in a central registry (§5.3) and can be
updated without touching other modules.

**Anti-bot notes:**
- The site is behind an invalid SSL cert (the cert is for an internal
  subdomain). The agent MUST configure its HTTP client to skip cert
  verification (with a logged warning), or the request fails entirely.
  The T&Cs modal that Playwright hits is the SAME issue — the site works
  fine in raw HTTP once SSL is handled.
- The site occasionally returns HTTP 503 (Service Unavailable) on
  bursts of requests. The agent MUST retry with exponential backoff:
  1s, 2s, 4s, up to 3 attempts per URL. A 503 on the final attempt is
  logged and the cycle continues.
- `robots.txt` disallows only `/data/` (not relevant) and fully blocks
  `Googlebot-Image` (not relevant). All auction pages are scrapable.

---

## 4. Data Model (SQLite)

The database file is `auctions.db`. Schema (initial version):

```sql
-- One row per auction.
CREATE TABLE auctions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  url             TEXT NOT NULL UNIQUE,           -- canonical URL on the source site
  slug            TEXT NOT NULL,                  -- URL slug
  title           TEXT NOT NULL,
  status          TEXT NOT NULL,                  -- FORTHCOMING | IN_PROGRESS | CLOSED | FINALIZED
  start_at        TEXT,                           -- ISO 8601 with timezone
  end_at          TEXT,                           -- ISO 8601 with timezone
  end_at_original TEXT,                           -- first end_at we ever saw (immutable)
  end_at_updated_at TEXT,                         -- when end_at last changed
  location        TEXT,
  thumbnail_url   TEXT,
  first_seen_at   TEXT NOT NULL,                  -- ISO 8601, when we discovered it
  last_scraped_at TEXT,                           -- ISO 8601, last detail-page scrape
  finalized_at    TEXT,                           -- ISO 8601, when we generated the final report
  extra_json      TEXT                            -- site-specific extra fields
);

CREATE INDEX idx_auctions_status ON auctions(status);
CREATE INDEX idx_auctions_end_at ON auctions(end_at);

-- One row per lot in an auction.
CREATE TABLE lots (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  auction_id      INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
  lot_number      TEXT NOT NULL,                  -- display number as shown on site ("1", "2", "1A")
  data_lot_id     TEXT,                           -- site-specific identifier (e.g., "2206305")
  quantity        INTEGER NOT NULL DEFAULT 1,
  description     TEXT NOT NULL,
  thumbnail_url   TEXT,
  full_image_urls TEXT,                           -- JSON array of URLs
  current_bid     REAL,                           -- nullable if bidding not started
  current_bid_currency TEXT DEFAULT 'AUD',
  bid_count       INTEGER NOT NULL DEFAULT 0,
  time_remaining  TEXT,                           -- human-readable countdown at scrape time
  bidding_status  TEXT,                           -- ACTIVE | CLOSED | WITHDRAWN
  first_seen_at   TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  removed_at      TEXT,                           -- non-null if lot disappeared from the page
  UNIQUE(auction_id, lot_number)
);

CREATE INDEX idx_lots_auction ON lots(auction_id);
CREATE INDEX idx_lots_status ON lots(bidding_status);
CREATE INDEX idx_lots_data_lot_id ON lots(data_lot_id);

-- Append-only history of every observed change to a lot.
CREATE TABLE lot_changes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  lot_id          INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
  observed_at     TEXT NOT NULL,                  -- ISO 8601
  change_type     TEXT NOT NULL,                  -- NEW_LOT | BID_CHANGED | BID_COUNT_CHANGED | DESCRIPTION_CHANGED | IMAGE_CHANGED | STATUS_CHANGED | REMOVED
  prev_value_json TEXT,                           -- JSON of the previous value (subset)
  new_value_json  TEXT,                           -- JSON of the new value (subset)
  cycle_id        TEXT NOT NULL                   -- ties a change to a specific scrape cycle
);

CREATE INDEX idx_lot_changes_lot ON lot_changes(lot_id);
CREATE INDEX idx_lot_changes_time ON lot_changes(observed_at);

-- One row per scrape cycle for observability.
CREATE TABLE cycles (
  id              TEXT PRIMARY KEY,               -- UUID
  started_at      TEXT NOT NULL,
  finished_at     TEXT,
  cycle_type      TEXT NOT NULL,                  -- DISCOVERY_24H | CHANGE_6H | FINAL_STRETCH_30M | FINALIZE | REMINDER
  pages_scanned   INTEGER NOT NULL DEFAULT 0,
  auctions_seen   INTEGER NOT NULL DEFAULT 0,
  auctions_new    INTEGER NOT NULL DEFAULT 0,
  lots_seen       INTEGER NOT NULL DEFAULT 0,
  lots_changed    INTEGER NOT NULL DEFAULT 0,
  errors_json     TEXT,                           -- structured error log
  notes           TEXT
);

-- Reminder delivery log. Ensures we send a given (auction, lead_minutes) only once.
CREATE TABLE reminder_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  auction_id      INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
  lead_minutes    INTEGER NOT NULL,
  scheduled_for   TEXT NOT NULL,                  -- the end_at at the time the reminder was scheduled
  sent_at         TEXT NOT NULL,
  message_preview TEXT,                           -- truncated body for audit
  delivery_status TEXT NOT NULL,                  -- SENT | FAILED
  UNIQUE(auction_id, lead_minutes, scheduled_for)
);

-- Migrations table for future schema changes.
CREATE TABLE schema_migrations (
  version         INTEGER PRIMARY KEY,
  applied_at      TEXT NOT NULL
);
```

The schema is versioned. Any change to tables requires a new migration row
in `schema_migrations`. Initial version is `1`. The codebase ships with a
migration runner that applies pending migrations at startup.

The `reminder_log` table is the dedup mechanism for §2.5: when generating
a reminder, the agent first checks if a row exists for
`(auction_id, lead_minutes, scheduled_for)`. If yes, skip. If no, send
and insert.

---

## 5. Configuration

### 5.1 `config.yaml` (primary config)

```yaml
site:
  index_url: "https://auctions.com.au/auctions/online"
  # Pagination path template. {page_num} is replaced with 1, 2, 3, ...
  pagination_url_template: "https://auctions.com.au/auctions/online/10/19/{page_num}?"
  # Safety cap so a malformed site can't trigger infinite walks.
  max_pages_safety_cap: 50
  request_timeout_seconds: 30
  # The site's cert is invalid; skip verification (logged as a warning).
  verify_ssl: false
  user_agent: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
  # Anti-bot retry policy.
  retry:
    max_attempts: 3
    base_delay_seconds: 1.0
    backoff_multiplier: 2.0
    retry_on_status: [502, 503, 504]

fetch:
  # The default fetcher is stdlib-compatible (urllib under the hood). Works
  # in any Python environment, no extra deps, fast, low memory.
  # Options: "stdlib" | "requests" | "httpx" | "playwright"
  # - "stdlib": uses urllib (zero install). DEFAULT.
  # - "requests": nicer ergonomics, requires `pip install requests`.
  # - "httpx": async-capable, requires `pip install httpx`.
  # - "playwright": headless browser, only use this if a future site
  #   transition requires JS. Adds ~200MB of browser binaries.
  client: "stdlib"
  # Optional proxy URL. If set, used by stdlib/requests/httpx.
  proxy_url: null
  # Optional browser profile directory (only used if client is "playwright").
  browser_profile_dir: null

filter:
  # Keywords that flag an auction as IT-related. See §2.2 for matching rules.
  it_keywords:
    - laptop
    - notebook
    - tablet
    - iphone
    - ipad
    - phone
    - smartphone
    - pc
    - desktop
    - mini pc
    - sff
    - workstation
    - monitor
    - display
    - screen
    - printer
    - scanner
    - network
    - cisco
    - server
    - imac
    - macbook
    - surface
    - thinkpad
    - elitebook
    - zbook
    - probook
    - toughpad
    - toughbook
    - android
    - pixel
    - "galaxy tab"
  # Always include auctions whose title matches any of these regexes.
  always_include_title_regex:
    - "(?i)general\\s+it"
  # Always exclude auctions matching any of these regexes.
  always_exclude_title_regex:
    - "(?i)^test"
    - "(?i)placeholder"

schedule:
  discovery_24h_cron: "0 6 * * *"            # daily 6 AM local
  change_6h_cron:     "0 */6 * * *"          # every 6 hours
  # The 30-min cadence and the 30-min reminder are data-driven (fire when
  # an auction crosses the relevant time boundary), not cron-driven. The
  # scheduler wakes every minute to evaluate.
  scheduler_heartbeat_cron: "* * * * *"
  # How many minutes before end_at the 30-min cadence kicks in.
  final_stretch_minutes: 180                 # 3 hours
  # Polling cadence once in the final stretch.
  final_stretch_poll_minutes: 30
  # How many minutes after end_at to wait before the finalization scrape.
  finalize_delay_minutes: 240                 # 4 hours

notify:
  # Significant change rules (Mavis chat). See §2.8.
  on_new_auction:        true
  on_final_stretch_entry: true
  on_extension:          true
  on_finalize:           true
  on_significant_change: true
  # Pre-close reminders. Each entry is one reminder lead time.
  # The default 30-min reminder satisfies the operator's primary requirement.
  # Additional lead times (e.g., 60 min) can be added.
  reminders:
    - lead_minutes: 30
  significant_change:
    bid_jump_pct: 20           # notify if current bid grew by ≥20% in a cycle
    always_notify_lot_events:
      - NEW_LOT
      - REMOVED

storage:
  sqlite_path: "./data/auctions.db"
  drive_html_prefix: "auction-tracker/reports/"
  drive_json_prefix: "auction-tracker/snapshots/"
  drive_archive_prefix: "auction-tracker/archives/"
  # Auto-purge HTML/JSON reports from Drive older than N days. SQLite
  # is NEVER purged.
  auto_purge_drive_files_older_than_days: 90
```

### 5.2 Config-driven extension

The extension points an operator or future AI agent will use most:

- **Adding a new IT keyword**: append to `filter.it_keywords`. No code change.
- **Adding a new pre-close reminder** (e.g., 60 min before end):
  ```yaml
  notify:
    reminders:
      - lead_minutes: 30
      - lead_minutes: 60
      - lead_minutes: 10
  ```
- **Changing the polling schedule**: edit `schedule.*_cron`. No code change.
- **Swapping the HTTP client** (e.g., to Playwright when site adds JS):
  set `fetch.client: "playwright"`. The `Fetcher` interface (§6.1)
  handles the dispatch.
- **Adding a proxy**: set `fetch.proxy_url`. The fetcher passes it to
  the underlying client.
- **Tuning notification sensitivity**: edit `notify.significant_change`.

If a change cannot be expressed in config, it belongs in a code change
documented in `CHANGELOG.md`.

### 5.3 Selector registry

Selectors used by the scraper live in a single Python module,
`auction_tracker/selectors.py`. The registry below reflects the **actual**
structure of `auctions.com.au` as of 2026-08-26:

```python
SELECTORS = {
    "index_page": {
        # One card per auction in the listing.
        "auction_card":            ".listing-post.online",
        # Within a card:
        "card_title":              ".listing-post.online h3 a, .listing-post.online h2 a",
        "card_url":                ".listing-post.online a[href*='/auctions/'][href$='.html'], .listing-post.online a[href*='/auctions/'][href$='.htm']",
        "card_status_class":       ".online-auction-status",
        "card_thumbnail":          ".listing-post.online img",
        # Pagination links (path-based, not query-based).
        "pagination_link":         "a[href*='/auctions/online/'][href$='?']",
    },
    "detail_page": {
        # Auction-level metadata table.
        "title_h1":                "h1",
        "status_class":            ".online-auction-status",
        "starts_th":               "th:contains('Starts')",
        "closes_th":               "th:contains('Closes')",
        "starts_time_span":        "th:contains('Starts') + td .time",
        "starts_date_span":        "th:contains('Starts') + td .date",
        "closes_time_span":        "th:contains('Closes') + td .time",
        "closes_date_span":        "th:contains('Closes') + td .date",
        "location_td":             "th:contains('Location') + td",
        # Per-lot fields. Every lot has data-lot-id="XXXXXX".
        "lot_data_id":             "data-lot-id",
        "lot_gallery_link":        "a[id^='lot-'][id$='-gallery-link']",
        "lot_image_count":         "a[id^='lot-'][id$='-gallery-link']@title",  # "Images in this lot: N"
        "lot_thumb_img":           "img.lot-thumb@data-src",   # lazy-loaded thumbnail
        "lot_number_anchor":       "a[data-lot-id]",           # text content is the displayed number
        "lot_qty":                 "div.cat-no",                # "Qty: N"
        "lot_description":         "[data-lot-id] ~ .description, [data-lot-id] + .description",
        "lot_bids":                "span[id^='lot-'][id$='-bids']",     # just the count number
        "lot_maxbid":              "span[id^='lot-'][id$='-maxbid']",   # the current bid amount (number)
        "lot_time_remaining":      "span[id^='lot-'][id$='-time']",
        "lot_status":              "span[id^='lot-'][id$='-status']",
        "lot_button":              "span[id^='lot-'][id$='-button']",
    },
}
```

**Important notes for the implementer:**

- `id^='lot-'` is a CSS attribute-prefix selector. Every lot has
  ~10 elements with `id="lot-{data_lot_id}-{kind}"` where `kind` is
  `gallery-link`, `bids`, `maxbid`, `time`, `status`, `button`, etc.
- The current bid lives in `id="lot-{X}-maxbid"`. The value is just a
  number (e.g., "24"), no `$` symbol or formatting.
- The bid count lives in `id="lot-{X}-bids"`. The value is just a number.
- The thumbnail uses lazy loading (`data-src` instead of `src`); the
  `src` is a 1x1 placeholder.
- The displayed lot number (e.g., "1", "2", "1A") is the text content
  of the `<a data-lot-id="X">N</a>` anchor.
- The `description` field appears in the lot row's text after the
  "Qty:" line and before the "Bidding Closes in:" line.

When the site changes, an AI agent (or a human) updates **only** this
file, runs the test suite against the fixture HTML in `tests/fixtures/`,
and the scraper continues to work. No other module needs to change.

---

## 6. Architecture

### 6.1 Module layout

```
auction-tracker/
├── README.md                       # Quickstart, overview
├── AGENTS.md                       # Meta-doc: full design, requirements, extension guide
├── CHANGELOG.md                    # Code change log
├── LICENSE
├── pyproject.toml                  # Python project config
├── requirements.txt
├── config.yaml                     # Operator-editable config
├── Dockerfile                      # Docker deployment
├── docker-compose.yml
├── cron/docker-cron                # Crontab for the Docker deployment
├── docs/
│   ├── architecture.md
│   ├── data-model.md
│   ├── deployment.md
│   └── how-to-extend.md
├── auction_tracker/
│   ├── __init__.py
│   ├── config.py                   # Loads + validates config.yaml
│   ├── logging_setup.py            # Structured logging (JSON)
│   ├── db.py                       # SQLite connection pool + migrations
│   ├── models.py                   # Dataclasses: Auction, Lot, Change, Cycle, Reminder
│   ├── selectors.py                # Central selector registry
│   ├── fetch/
│   │   ├── __init__.py
│   │   ├── base.py                 # Fetcher abstract base class
│   │   ├── stdlib_fetcher.py       # HTTP via urllib (DEFAULT)
│   │   ├── requests_fetcher.py     # HTTP via requests
│   │   ├── httpx_fetcher.py        # HTTP via httpx
│   │   └── playwright_fetcher.py   # Headless browser via playwright (fallback)
│   ├── scraper/
│   │   ├── __init__.py
│   │   ├── index_page.py           # Parse auction list + paginate
│   │   ├── detail_page.py          # Parse lots on detail page
│   │   └── filter.py               # IT category filter (two-pass)
│   ├── scheduler/
│   │   ├── __init__.py
│   │   ├── tick.py                 # Single cycle driver
│   │   ├── cadence.py              # Cycle-type selection per auction
│   │   └── reminders.py            # Reminder scheduling + dedup
│   ├── notifier/
│   │   ├── __init__.py
│   │   ├── base.py                 # Notifier abstract base class
│   │   └── mavis_chat.py           # Mavis chat notifier
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── sqlite_store.py         # All DB reads/writes
│   │   ├── drive_store.py          # Mavis Drive read/write
│   │   └── archiver.py             # Zip export
│   ├── report/
│   │   ├── __init__.py
│   │   ├── html_report.py          # Jinja2 templates → HTML
│   │   └── reminder.py             # Build reminder message body
│   ├── templates/
│   │   ├── report.html.j2
│   │   ├── reminder.html.j2
│   │   └── final.html.j2
│   ├── change_detection.py         # Diff lots vs previous snapshot
│   ├── archive.py                  # Build downloadable zip
│   └── cli.py                      # `python -m auction_tracker` entrypoint
├── scripts/
│   ├── run_cycle.py                # Manual single-cycle trigger
│   ├── archive.py                  # Build archive on demand
│   ├── send_reminder.py            # Force-send a reminder for a specific auction
│   └── final_report.py             # Force-finalize a specific auction
├── tests/
│   ├── fixtures/
│   │   ├── index_page_v1.html
│   │   ├── index_page_v2.html      # alternate structure / page 2
│   │   ├── index_page_empty.html   # server returned no auctions
│   │   ├── detail_page_v1.html     # full IT auction with 115 lots
│   │   └── detail_page_v2.html     # alternate structure
│   ├── test_filter.py
│   ├── test_index_page.py
│   ├── test_pagination.py          # loop guard, empty page, max cap, non-retryable error
│   ├── test_detail_page.py
│   ├── test_change_detection.py
│   ├── test_db_migrations.py
│   ├── test_cadence.py
│   ├── test_reminders.py           # lead time, dedup, single-send, hot-lot calc
│   ├── test_retry.py               # 503 retry, exponential backoff
│   ├── test_fetcher.py             # all 4 fetcher backends behave the same
│   ├── test_archiver.py
│   └── test_e2e.py
└── data/                           # Local data dir (gitignored)
    └── auctions.db
```

### 6.2 Component responsibilities

- **Fetcher** (`fetch/`): one interface, multiple implementations. The
  default is `stdlib_fetcher` (urllib). Methods: `get(url) -> str (HTML)`,
  `get_image(url) -> bytes`. Configurable proxy (and browser profile for
  the Playwright backend). Includes a thin retry wrapper.
- **Scraper** (`scraper/`): pure functions that take HTML + selectors and
  return parsed dataclasses. No I/O. Easy to unit-test against fixtures.
- **Filter** (`scraper/filter.py`): given an `Auction` dataclass, returns
  bool. Uses config-driven keywords. Two passes: cheap at index time,
  full confirmation at detail-page time.
- **DB** (`db.py` + `sqlite_store.py`): connection management + migration
  runner + typed CRUD.
- **Scheduler** (`scheduler/`): determines which cycle type to run for
  which auctions, when. See §6.4. Includes the reminder module
  (`scheduler/reminders.py`) which is responsible for computing the
  reminder trigger time and checking `reminder_log` for dedup.
- **Notifier** (`notifier/`): one interface, multiple channels. The Mavis
  chat implementation is the default. Stub implementations for
  `log_only` (for tests) and `email` (skeleton, not implemented).
- **Drive store** (`drive_store.py`): wraps Mavis Drive API for upload,
  download, list, delete.
- **HTML report** (`report/html_report.py`): Jinja2 templates produce the
  human-readable report. Templates live in `auction_tracker/templates/`.
- **Reminder** (`report/reminder.py`): builds the chat message body and
  the HTML snippet for the reminder. Pure function given an Auction +
  list of Lots + change history.
- **Change detection** (`change_detection.py`): takes a previous and current
  list of lots for an auction, returns a list of `Change` events. Pure
  function.
- **Archiver** (`storage/archiver.py`): builds the zip.

### 6.3 End-to-end cycle (one tick)

1. Scheduler picks cycle type (DISCOVERY_24H | CHANGE_6H | FINAL_STRETCH_30M
   | FINALIZE | REMINDER) based on current time + auctions in DB.
2. For each affected auction, fetch the page (via Fetcher), parse (via
   Scraper), diff (via change_detection), persist (via sqlite_store),
   upload artifacts (via drive_store), notify (via notifier).
3. Record the cycle in `cycles` table.
4. Log everything in structured JSON.

### 6.4 Cadence logic

```
For each auction in DB:
  If status == FINALIZED:               skip
  If status == CLOSED (not finalized):   schedule for FINALIZE at end_at + finalize_delay_minutes
  If now < start_at:                     not yet active; refresh on DISCOVERY_24H only
  If start_at <= now < end_at - final_stretch_minutes:
                                         eligible for CHANGE_6H
  If end_at - final_stretch_minutes <= now < end_at:
                                         eligible for FINAL_STRETCH_30M
                                         ALSO: check reminder triggers (see below)
  If now >= end_at:                      eligible for FINALIZE
```

**Reminder trigger logic** (runs every scheduler tick):

```
For each auction where end_at - lead_minutes <= now < end_at
  AND status != 'finalized':
    For each entry in config.notify.reminders:
      If reminder_log has no row for (auction.id, entry.lead_minutes, end_at):
        1. Fetch the latest auction state (lots, bids).
        2. Build reminder message (see §2.5).
        3. Send via notifier.
        4. Insert reminder_log row with delivery_status = SENT or FAILED.
```

The scheduler runs every minute (heartbeat), evaluates which auctions are
due, and groups them by cycle type. The CHANGE_6H cron and DISCOVERY_24H
cron can also force a tick on demand.

The 30-min cadence and the 30-min reminder are **both data-driven** —
they fire when the data conditions are met, not on a cron schedule.
The heartbeat scheduler detects state transitions.

### 6.5 Pagination guarantee

The index page scraper MUST implement:

1. Build page-N URL via `config.pagination_url_template` with
   `{page_num} = 1, 2, 3, ...`.
2. Fetch the page (with retry on 502/503/504 per §3 anti-bot notes).
3. Extract auction-detail URLs.
4. If 0 URLs returned: stop (legitimate end of pagination; server
   returns 200 with empty content).
5. If the first URL is identical to a previously-seen page's first URL:
   stop (loop guard).
6. Continue until termination conditions are met OR
   `pages_scanned >= max_pages_safety_cap` (default 50).
7. Log `pages_scanned`, `auctions_seen`, termination_reason every cycle.
8. The cycle is marked incomplete (and the operator notified) if
   `pages_scanned` is less than a configurable `min_pages_scanned_per_discovery`
   (default 1) — this catches the case where a transient 503 truncated
   the walk before any auctions were found.
9. **Test coverage**: at least three test fixtures must simulate
   pagination behaviour — a multi-page index that ends cleanly, an
   index that returns an empty page mid-walk, and a pathologically
   looping server that never returns an empty page (to verify the
   loop guard and the safety cap).

---

## 7. Future-Proofing Requirements (CRITICAL)

The codebase MUST be built so that a future AI agent, given only this
prompt and the codebase, can:

### 7.1 Update the site selectors when the site changes
- All selectors in one file (`selectors.py`).
- Fixtures in `tests/fixtures/` for unit testing.
- The agent should run the test suite after a selector change to confirm.

### 7.2 Add a new IT category
- Edit `config.yaml` → `filter.it_keywords`. No code change.
- If the new category needs a regex (e.g., "any auction with `server-rack`
  in the title"), add to `filter.always_include_title_regex`.

### 7.3 Swap the HTTP client (e.g., to Playwright when site adds JS)
- Implement `Fetcher` interface in `fetch/playwright_fetcher.py`.
- Set `fetch.client: "playwright"` in config.
- The `Fetcher` factory in `fetch/__init__.py` returns the right class.

### 7.4 Add a proxy
- Set `fetch.proxy_url` in config. The stdlib, requests, and httpx
  fetchers honor it. The Playwright fetcher uses `browser.new_context(proxy=...)`.

### 7.5 Add a new notification channel (e.g., email, Slack)
- Implement `Notifier` interface in `notifier/<channel>.py`.
- Register in `notifier/__init__.py`'s factory function.

### 7.6 Change the database schema
- Add a new migration file in `db/migrations/`.
- Bump `schema_migrations` version.
- Migration runner applies it at startup.

### 7.7 Update the report layout
- Edit Jinja2 templates in `auction_tracker/templates/`.
- No Python code change.

### 7.8 Add a new scrape cadence (e.g., 1-min tick)
- Add cycle type in `scheduler/cadence.py`.
- Add cron expression in `config.yaml`.

### 7.9 Add a new pre-close reminder
- Add an entry to `config.yaml` → `notify.reminders`:
  ```yaml
  notify:
    reminders:
      - lead_minutes: 30
      - lead_minutes: 60
      - lead_minutes: 10
  ```
  No code change. The reminder scheduler iterates over the list.

### 7.10 Read the AGENTS.md meta-doc

A future AI agent MUST be able to start by reading `AGENTS.md` and from
that alone understand:

- The full set of operator requirements (this document, summarized).
- The architecture and module layout.
- Where to add a new fetcher / notifier / scraper.
- How to add a migration.
- How to run tests.
- How to deploy (both Mavis and Docker).
- How to interpret logs.

`AGENTS.md` is the single most important file in the repo for AI-agent
maintainability. It MUST be kept up to date as the codebase evolves.

---

## 8. Testing Requirements

- **Unit tests** for every parser (index, detail, filter).
- **Unit tests** for change detection (golden-file diffs).
- **Unit tests** for cadence logic (mocked clock, table-driven cases).
- **Unit tests** for reminder logic:
  - Lead time correctly identifies "due" auctions.
  - Dedup via `reminder_log` (one send per `(auction, lead, end_at)`).
  - Message body includes top-3 lots, hot-3 lots, no-bid summary.
  - Reminder is suppressed if the auction was extended (new end_at → new
    row in reminder_log is OK, but the OLD scheduled_for is preserved
    so we don't double-send).
- **Unit tests** for pagination:
  - Multi-page index ends cleanly at the empty page.
  - Loop guard trips when server returns a duplicate first URL.
  - Safety cap trips at `max_pages_safety_cap` pages.
  - Non-retryable HTTP error stops the walk without losing already-found
    auctions.
- **Unit tests** for DB migrations (apply v1, apply v2, rollback).
- **Unit tests** for retry (503, 504, 502 retry; 404 does NOT retry;
  exponential backoff timing).
- **Unit tests** for the `Fetcher` interface (all 4 backends must
  produce the same result for the same URL).
- **Integration test** that runs an end-to-end cycle against a saved
  HTML fixture (no real network).
- **E2E test** that runs the whole pipeline against the live site in
  `--dry-run` mode and verifies no exceptions (gated behind an env var,
  not run in CI by default).
- All tests runnable via `pytest`. CI-friendly (no network required
  unless explicitly opted in).

---

## 9. Deployment

### 9.1 Mavis cloud (default)

- The agent's main module is `auction_tracker.cli`.
- The Mavis cron schedules call `python -m auction_tracker tick` (or
  specific cycle types: `tick discovery`, `tick change`, `tick heartbeat`).
- Persistent state in `/workspace/auction-tracker/data/auctions.db`.
- Reports uploaded to Mavis Drive.
- Notifications sent via Mavis chat through the agent's own chat channel.
- The `AGENTS.md` is the operator's reference for kicking the tires.

### 9.2 Docker (production-grade)

- `Dockerfile` builds a Python 3.11+ image with all deps.
- `docker-compose.yml` runs the container with:
  - A mounted volume for `./data` (persistent SQLite).
  - Environment variables for `MAVIS_AGENT_ID`, `MAVIS_CHAT_CHANNEL_ID`,
    and any credentials.
  - A cron daemon in the container that runs the four schedules.
- The container exposes no ports (no HTTP server needed; notifications
  are push-based).
- Logs go to stdout (docker logs captures them) and to `./data/logs/`.
- The operator can `docker exec` into the container to run
  `python -m auction_tracker archive` for a manual archive.

### 9.3 First-run checklist (operator-facing)

1. `pip install -e .` (or `docker compose up -d`).
2. Edit `config.yaml` to confirm `site.index_url`,
   `site.pagination_url_template`, and the IT keyword list.
3. Set Mavis chat credentials (or run with `--notifier log_only` for testing).
4. Run `python -m auction_tracker init-db` to create the SQLite schema.
5. Run `python -m auction_tracker tick discovery --dry-run` to verify
   the scraper can hit the site, walk pagination, and find IT auctions.
6. Start the cron / docker container.
7. Wait for the first cycle. Confirm a chat notification + an HTML
   report in Drive.

---

## 10. Notification Interface

```python
class Notifier(Protocol):
    def new_auction(self, auction: Auction) -> None: ...
    def final_stretch_entry(self, auction: Auction) -> None: ...
    def extension(
        self, auction: Auction, old_end_at: str, new_end_at: str
    ) -> None: ...
    def reminder(
        self,
        auction: Auction,
        lead_minutes: int,
        snapshot: AuctionSnapshot,  # lots + bids + change history
    ) -> None: ...
    def significant_change(
        self, auction: Auction, lot: Lot, change: Change
    ) -> None: ...
    def finalized(self, auction: Auction, report_drive_path: str) -> None: ...
```

The Mavis chat implementation (`notifier/mavis_chat.py`) is the default
and is fully implemented. The `log_only` implementation is a stub for
tests. Other channels (email, Lark, Slack) are scaffolds left for future
work.

The `reminder` method receives a pre-built snapshot so the notifier
doesn't have to know how to query the DB.

---

## 11. Logging and Observability

- Structured JSON logs to stdout AND to `./data/logs/auction-tracker.log`.
- Every cycle logs: cycle_id, cycle_type, started_at, finished_at,
  pages_scanned, auctions_seen, lots_seen, lots_changed, errors.
- Every notification logs: notifier_name, event_type, auction_id, lot_id,
  delivery_status.
- Every fetch logs: url, status_code, duration_ms, fetcher_name,
  retry_attempt.
- Every reminder logs: auction_id, lead_minutes, scheduled_for, sent_at,
  delivery_status, message_size_bytes.
- Errors are caught at module boundaries, logged with full context, and
  recorded in the `cycles.errors_json` field. The cycle does not abort
  on a single auction failure — it logs the failure and continues to
  the next auction.

---

## 12. Configuration-driven extensibility (summary)

A future operator (or AI agent) who needs to change behavior should be
able to do it in this order, choosing the lightest touch first:

1. Edit `config.yaml` (keywords, schedule, thresholds, proxy, reminders).
2. Edit `auction_tracker/selectors.py` (when site HTML changes).
3. Edit a Jinja2 template (when report layout changes).
4. Add a fetcher / notifier implementation (when adding a transport).
5. Add a migration (when changing the DB schema).
6. Modify a scraper (when parsing logic changes beyond selectors).
7. Modify cadence / scheduler (when changing the time-based logic).

Steps 1-4 require no Python knowledge. Steps 5-7 are documented in
`AGENTS.md` with worked examples.

---

## 13. Acceptance Criteria

The codebase is considered complete when:

- [ ] `pytest` passes with all unit and integration tests.
- [ ] `python -m auction_tracker init-db` creates the SQLite schema
      (including the `reminder_log` table).
- [ ] `python -m auction_tracker tick discovery --dry-run` walks the
      live site's pagination, finds all current IT auctions, and exits
      without errors.
- [ ] `python -m auction_tracker tick change <url>` scrapes a single
      auction and persists its lots.
- [ ] The HTML report renders correctly and is uploaded to Drive.
- [ ] A Mavis chat notification is sent on a new IT auction discovery.
- [ ] **A Mavis chat notification is sent exactly once per
      `(auction, lead_minutes, end_at)` tuple when the auction crosses
      the `end_at - lead_minutes` mark.** A second `lead_minutes` entry
      in the config produces a second notification at the right time.
- [ ] **The reminder message includes: title, URL, end time, lot count,
      total bids, top-3 lots by current bid, hot-3 lots by last-hour
      bid jump, and a no-bids-yet summary.**
- [ ] The Docker image builds and the container runs the cron schedules.
- [ ] The cadence logic correctly transitions an auction from CHANGE_6H
      to FINAL_STRETCH_30M at 3 hours before end_at.
- [ ] The finalize step runs at end_at + 4h and produces a final report.
- [ ] `python -m auction_tracker archive` produces a downloadable zip.
- [ ] `AGENTS.md` exists, is up to date, and is sufficient for a future
      AI agent to make any of the changes in §7.

---

## 14. What the implementing AI agent MUST do

1. Read this prompt in full.
2. Read `AGENTS.md` template (provided as a starting point — extend it
   with anything you discover during the build that helps future agents).
3. Scaffold the project structure (§6.1).
4. Implement modules in this order (each independently testable):
   1. `config.py` + `config.yaml`
   2. `db.py` + `sqlite_store.py` + migration runner
   3. `selectors.py` + `fetch/` (with `stdlib_fetcher` as the default,
      and stubs for the other three backends)
   4. `scraper/` (filter first, then index, then detail)
   5. `change_detection.py`
   6. `models.py` (dataclasses)
   7. `report/` (html_report, reminder, templates)
   8. `notifier/` (with `mavis_chat` and `log_only`)
   9. `storage/` (drive_store, archiver)
   10. `scheduler/` (cadence, tick, **reminders**)
   11. `cli.py`
   12. Tests for each module
   13. `Dockerfile` + `docker-compose.yml`
   14. `AGENTS.md` (write last, with everything you learned)
5. Run the full test suite. Fix until green.
6. Run a live dry-run. Verify the site is reachable, the scraper finds
   IT auctions, the DB is populated, and the pagination walk terminates
   cleanly.
7. Generate the first HTML report and verify it renders.
8. Produce a summary of what was built, what assumptions were made, and
   what the operator needs to do next (per §9.3 first-run checklist).

---

## 15. Open assumptions (flag to operator)

- The auction site does not require login. If it does, the
  `requests_fetcher` can be extended with cookie persistence, and the
  Playwright fetcher can use a browser profile (set in config).
- The site returns server-rendered HTML. The stdlib fetcher is
  sufficient. If the site transitions to a client-rendered SPA, switch
  `fetch.client: "playwright"`.
- The site's SSL cert is invalid; the agent skips verification. This is
  logged as a warning. If the cert is ever fixed, set `site.verify_ssl: true`.
- The IT keyword list in §2.2 / §5.1 is a starting point. The operator
  should review the first week's discoveries and add/remove keywords.
- Bid amounts are in AUD. The schema stores currency separately, so
  multi-currency is supported if the site ever mixes them.
- The "4 hours after end" finalization delay is chosen so the site has
  time to settle. If the site has known finalization issues, the
  operator can tune `schedule.finalize_delay_minutes` in config.
- The 30-minute reminder lead time is a starting point. Multiple
  lead times can be configured (e.g., 60 + 30 + 10 min).

---

## 16. v2 changelog (from v1)

1. **Default fetcher changed from `requests` to `stdlib`** (urllib).
   Live recon showed the site is fully server-rendered; the
   T&Cs modal is a CSS overlay that doesn't affect raw HTML.
2. **Removed the T&Cs modal dismissal section** — not needed when
   using HTTP instead of a browser.
3. **Real selector registry** replaces the speculative v1 selectors.
   Reflects the actual DOM of `auctions.com.au` as of 2026-08-26:
   `.listing-post.online` for cards, `id="lot-{X}-{kind}"` for lot
   elements, `<th>Starts</th>`/`<th>Closes</th>` for dates.
4. **Explicit pagination algorithm** with loop-guard and safety cap
   replaces the vague "follow next-page" guidance.
5. **Anti-bot retry policy** (502/503/504 with exponential backoff) is
   now explicit and configurable.
6. **SSL cert bypass** is now explicit and configurable
   (`site.verify_ssl: false`).
7. **`data_lot_id` column** added to the `lots` table for the site's
   internal lot identifier (e.g., "2206305").
8. **30-min reminder** is a new notification event (§2.5, §2.8, §7.9,
   §13 acceptance criteria). It is independent of the existing
   `FINAL_STRETCH_30M` cadence — the cadence scrapes, the reminder
   notifies.
9. **`reminder_log` table** added for reminder dedup
   (one send per `(auction_id, lead_minutes, scheduled_for)`).
10. **`report/reminder.py`** added as a pure-function module that
    builds the reminder message body (top-3 lots, hot-3 lots,
    no-bid summary).
11. **`scheduler/reminders.py`** added as a separate module under
    the scheduler, with its own tests in `tests/test_reminders.py`.
12. **New config keys**: `site.pagination_url_template`,
    `site.max_pages_safety_cap`, `site.verify_ssl`, `site.retry.*`,
    `notify.reminders`, `schedule.final_stretch_minutes`,
    `schedule.final_stretch_poll_minutes`,
    `schedule.finalize_delay_minutes`.
13. **Fetcher module is now `fetch/` not `fetcher/`** to avoid
    collision with the `fetcher` term used in some libraries.
14. **HTML report shows "REMINDER SENT" badge** for any auction
    currently in the reminder window.
15. **Test coverage expanded**: added test_pagination, test_retry,
    test_reminders, test_fetcher (multi-backend parity).

---

**End of generation prompt v2.**
