"""Dataclasses passed between the scraper, the store and the reporters.

These are deliberately plain: scrapers build them from HTML/JSON with no I/O,
the store persists them, and the report/notify layers read them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .util import clean_text, humanize_delta, now_utc, truncate

# Auction lifecycle
FORTHCOMING = "FORTHCOMING"
IN_PROGRESS = "IN_PROGRESS"
CLOSED = "CLOSED"
FINALIZED = "FINALIZED"

# Lot bidding state
ACTIVE = "ACTIVE"
LOT_CLOSED = "CLOSED"
WITHDRAWN = "WITHDRAWN"

# Change types recorded in ``lot_changes`` / ``auction_changes``
NEW_LOT = "NEW_LOT"
REMOVED = "REMOVED"
BID_CHANGED = "BID_CHANGED"
BID_COUNT_CHANGED = "BID_COUNT_CHANGED"
DESCRIPTION_CHANGED = "DESCRIPTION_CHANGED"
IMAGE_CHANGED = "IMAGE_CHANGED"
STATUS_CHANGED = "STATUS_CHANGED"
QUANTITY_CHANGED = "QUANTITY_CHANGED"
RESERVE_MET = "RESERVE_MET"
CLOSE_TIME_CHANGED = "CLOSE_TIME_CHANGED"

# Cycle types
DISCOVERY = "DISCOVERY_24H"
CHANGE = "CHANGE_6H"
FINAL_STRETCH = "FINAL_STRETCH_30M"
FINALIZE = "FINALIZE"
REMINDER = "REMINDER"
MANUAL = "MANUAL"


@dataclass
class Auction:
    """One online auction."""

    url: str
    title: str = ""
    slug: str = ""
    site_id: str = ""          # the site's internal id, e.g. "14704"
    status: str = FORTHCOMING
    start_at: datetime | None = None
    end_at: datetime | None = None
    end_at_original: datetime | None = None
    end_at_updated_at: datetime | None = None
    location: str = ""
    thumbnail_url: str = ""
    description: str = ""
    inspection: str = ""
    collection: str = ""
    contact: str = ""
    terms: str = ""
    lot_count: int = 0

    # Classification
    is_it: bool = False
    it_reason: str = ""
    it_confidence: float = 0.0
    it_source: str = ""        # keyword | ai | manual | mixed-lots

    # Bookkeeping
    id: int | None = None
    first_seen_at: datetime | None = None
    last_scraped_at: datetime | None = None
    finalized_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    lots: list["Lot"] = field(default_factory=list)

    # -- derived ----------------------------------------------------------
    def computed_status(self, at: datetime | None = None) -> str:
        """Status implied by the clock, ignoring the stored value."""
        if self.finalized_at:
            return FINALIZED
        moment = at or now_utc()
        if self.end_at and moment >= self.end_at:
            return CLOSED
        if self.start_at and moment < self.start_at:
            return FORTHCOMING
        return IN_PROGRESS

    def time_left(self, at: datetime | None = None) -> timedelta | None:
        if not self.end_at:
            return None
        return self.end_at - (at or now_utc())

    def countdown(self, at: datetime | None = None) -> str:
        return humanize_delta(self.time_left(at))

    def in_final_stretch(self, minutes: int, at: datetime | None = None) -> bool:
        left = self.time_left(at)
        return left is not None and timedelta(0) < left <= timedelta(minutes=minutes)

    @property
    def was_extended(self) -> bool:
        return bool(
            self.end_at and self.end_at_original and self.end_at > self.end_at_original
        )

    @property
    def total_bids(self) -> int:
        return sum(lot.bid_count for lot in self.lots)

    @property
    def total_current_bids(self) -> float:
        return sum(lot.current_bid or 0.0 for lot in self.lots)

    @property
    def lots_without_bids(self) -> int:
        return sum(1 for lot in self.lots if not lot.bid_count)

    def top_lots(self, count: int = 3) -> list["Lot"]:
        return sorted(self.lots, key=lambda l: l.current_bid or 0.0, reverse=True)[:count]


@dataclass
class Lot:
    """One lot within an auction. ``site_id`` is the site's ``data-lot-id``."""

    lot_number: str
    site_id: str = ""
    description: str = ""
    short_description: str = ""
    quantity: int = 1
    thumbnail_url: str = ""
    image_urls: list[str] = field(default_factory=list)
    thumbnail_urls: list[str] = field(default_factory=list)
    image_count: int = 0

    current_bid: float | None = None
    currency: str = "AUD"
    bid_count: int = 0
    highest_bidder_id: str = ""
    bidder_sequence: list[str] = field(default_factory=list)
    met_reserve: bool | None = None
    bidding_status: str = ACTIVE
    status_label: str = ""
    time_remaining: str = ""
    closes_at: datetime | None = None
    opens_at: datetime | None = None
    bid_url: str = ""

    # AI-extracted attributes (see ai/tasks.py::extract_specs)
    category: str = ""
    brand: str = ""
    model: str = ""
    specs: dict[str, Any] = field(default_factory=dict)
    is_it: bool | None = None

    # Bookkeeping
    id: int | None = None
    auction_id: int | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    removed_at: datetime | None = None
    final_bid: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def unique_bidders(self) -> int:
        return len({b for b in self.bidder_sequence if b})

    @property
    def has_bids(self) -> bool:
        return bool(self.bid_count)

    def title(self, limit: int = 90) -> str:
        return truncate(self.short_description or self.description, limit)

    def fingerprint(self) -> str:
        """Fields that, if they move, mean the listing itself was edited."""
        return "|".join(
            [
                clean_text(self.description),
                str(self.quantity),
                str(sorted(self.image_urls)),
                clean_text(self.thumbnail_url),
            ]
        )


@dataclass
class Change:
    """A single observed difference, appended to history (never updated)."""

    change_type: str
    lot_number: str = ""
    lot_site_id: str = ""
    field_name: str = ""
    previous: Any = None
    current: Any = None
    observed_at: datetime | None = None
    significant: bool = False
    note: str = ""

    lot_id: int | None = None
    auction_id: int | None = None
    cycle_id: str = ""
    id: int | None = None

    def describe(self) -> str:
        """One-line human summary used in emails and the report."""
        where = f"Lot {self.lot_number}" if self.lot_number else "Auction"
        if self.change_type == NEW_LOT:
            return f"{where} added: {truncate(str(self.current), 80)}"
        if self.change_type == REMOVED:
            return f"{where} removed"
        if self.change_type == BID_CHANGED:
            return f"{where} bid {self.previous or 0} -> {self.current}"
        if self.change_type == BID_COUNT_CHANGED:
            return f"{where} bid count {self.previous or 0} -> {self.current}"
        if self.change_type == RESERVE_MET:
            return f"{where} reserve met"
        if self.change_type == CLOSE_TIME_CHANGED:
            return f"{where} close time {self.previous} -> {self.current}"
        label = self.change_type.replace("_", " ").lower()
        return f"{where} {label}: {truncate(str(self.previous), 40)} -> {truncate(str(self.current), 40)}"


@dataclass
class Cycle:
    """One scrape run, for observability and the report's audit trail."""

    cycle_type: str
    id: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pages_scanned: int = 0
    auctions_seen: int = 0
    auctions_new: int = 0
    auctions_scraped: int = 0
    lots_seen: int = 0
    lots_changed: int = 0
    changes_recorded: int = 0
    images_downloaded: int = 0
    ai_calls: int = 0
    emails_sent: int = 0
    errors: list[str] = field(default_factory=list)
    notes: str = ""
    ai_summary: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class ReminderRecord:
    """Delivery log entry: one per (auction, lead_minutes, scheduled_end)."""

    auction_id: int
    lead_minutes: int
    scheduled_for: datetime
    sent_at: datetime | None = None
    delivery_status: str = "PENDING"
    recipients: list[str] = field(default_factory=list)
    message_preview: str = ""
    id: int | None = None
