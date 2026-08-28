"""Change detection: diff the stored lots against a fresh scrape.

A pure function, so it is fully unit-testable. What counts as *significant*
(i.e. worth emailing about, and highlighted in the report) is config-driven via
``notify.significant_change``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from . import models as M
from .config import Config
from .logging_setup import get_logger
from .models import Auction, Change, Lot
from .util import clean_text, now_utc, pct_change, to_iso, truncate

log = get_logger(__name__)


@dataclass
class SignificanceRules:
    """Thresholds deciding which changes escalate to a notification."""

    bid_jump_pct: float = 20.0
    bid_jump_min_dollars: float = 20.0
    always: frozenset[str] = frozenset({M.NEW_LOT, M.REMOVED})

    @classmethod
    def from_config(cls, config: Config) -> "SignificanceRules":
        section = config.section("notify").get("significant_change", {}) or {}
        return cls(
            bid_jump_pct=float(section.get("bid_jump_pct", 20)),
            bid_jump_min_dollars=float(section.get("bid_jump_min_dollars", 0)),
            always=frozenset(
                str(x).upper()
                for x in section.get("always_notify_lot_events", [M.NEW_LOT, M.REMOVED])
            ),
        )

    def bid_jump_is_significant(
        self, previous: float | None, current: float | None
    ) -> bool:
        if current is None:
            return False
        delta = current - (previous or 0.0)
        if delta < self.bid_jump_min_dollars:
            return False
        change = pct_change(previous, current)
        if change is None:
            return False
        # First bid on a lot (growth from nothing) always counts.
        return change == float("inf") or change >= self.bid_jump_pct


@dataclass
class DiffResult:
    changes: list[Change]
    new_lots: list[Lot]
    removed_lots: list[Lot]
    changed_lots: list[Lot]

    @property
    def significant(self) -> list[Change]:
        return [c for c in self.changes if c.significant]

    def __len__(self) -> int:
        return len(self.changes)


def _index(lots: Sequence[Lot]) -> dict[str, Lot]:
    """Index lots by number, with the site id as a secondary key."""
    out: dict[str, Lot] = {}
    for lot in lots:
        if lot.lot_number:
            out[f"n:{clean_text(lot.lot_number).lower()}"] = lot
        if lot.site_id:
            out[f"s:{lot.site_id}"] = lot
    return out


def _match(lot: Lot, index: dict[str, Lot]) -> Lot | None:
    if lot.site_id and f"s:{lot.site_id}" in index:
        return index[f"s:{lot.site_id}"]
    if lot.lot_number:
        return index.get(f"n:{clean_text(lot.lot_number).lower()}")
    return None


def diff_lots(
    previous: Sequence[Lot],
    current: Sequence[Lot],
    *,
    config: Config,
    auction: Auction | None = None,
    cycle_id: str = "",
    observed_at: datetime | None = None,
    rules: SignificanceRules | None = None,
) -> DiffResult:
    """Compare two snapshots of an auction's lots.

    ``previous`` is what the database holds, ``current`` is the fresh scrape.
    Lots are matched on the site's ``data-lot-id`` first (stable even if the
    displayed number is edited) and on the lot number as a fallback.
    """
    rules = rules or SignificanceRules.from_config(config)
    stamp = observed_at or now_utc()
    auction_id = auction.id if auction else None

    previous_index = _index(previous)
    current_index = _index(current)

    changes: list[Change] = []
    new_lots: list[Lot] = []
    removed_lots: list[Lot] = []
    changed_lots: list[Lot] = []

    def add(
        change_type: str,
        lot: Lot,
        *,
        field_name: str = "",
        before: object = None,
        after: object = None,
        significant: bool = False,
        note: str = "",
    ) -> None:
        changes.append(
            Change(
                change_type=change_type,
                lot_number=lot.lot_number,
                lot_site_id=lot.site_id,
                field_name=field_name,
                previous=before,
                current=after,
                observed_at=stamp,
                significant=significant or change_type in rules.always,
                note=note,
                lot_id=lot.id,
                auction_id=auction_id,
                cycle_id=cycle_id,
            )
        )

    # --- lots present now
    for lot in current:
        before = _match(lot, previous_index)
        if before is None:
            new_lots.append(lot)
            add(
                M.NEW_LOT,
                lot,
                field_name="lot",
                after=truncate(lot.description, 200),
                significant=True,
            )
            continue

        lot.id = lot.id or before.id
        lot.first_seen_at = before.first_seen_at
        touched = False

        # Bid amount
        if (lot.current_bid or 0) != (before.current_bid or 0):
            significant = rules.bid_jump_is_significant(before.current_bid, lot.current_bid)
            change = pct_change(before.current_bid, lot.current_bid)
            note = ""
            if change is not None and change != float("inf"):
                note = f"{change:+.1f}%"
            elif change == float("inf"):
                note = "first bid"
            add(
                M.BID_CHANGED,
                lot,
                field_name="current_bid",
                before=before.current_bid,
                after=lot.current_bid,
                significant=significant,
                note=note,
            )
            touched = True

        # Bid count
        if lot.bid_count != before.bid_count:
            add(
                M.BID_COUNT_CHANGED,
                lot,
                field_name="bid_count",
                before=before.bid_count,
                after=lot.bid_count,
            )
            touched = True

        # Description (the operator explicitly wants edits to the listing text)
        if clean_text(lot.description) and clean_text(lot.description) != clean_text(
            before.description
        ):
            add(
                M.DESCRIPTION_CHANGED,
                lot,
                field_name="description",
                before=truncate(before.description, 400),
                after=truncate(lot.description, 400),
                significant=M.DESCRIPTION_CHANGED in rules.always,
            )
            touched = True

        if lot.quantity != before.quantity and lot.quantity:
            add(
                M.QUANTITY_CHANGED,
                lot,
                field_name="quantity",
                before=before.quantity,
                after=lot.quantity,
                significant=True,
            )
            touched = True

        # Images: compare the sets, ignoring ordering.
        before_images = set(before.image_urls) | set(before.thumbnail_urls)
        current_images = set(lot.image_urls) | set(lot.thumbnail_urls)
        if current_images and current_images != before_images:
            added = len(current_images - before_images)
            gone = len(before_images - current_images)
            add(
                M.IMAGE_CHANGED,
                lot,
                field_name="images",
                before=len(before_images),
                after=len(current_images),
                note=f"+{added}/-{gone}",
            )
            touched = True

        if lot.bidding_status != before.bidding_status:
            add(
                M.STATUS_CHANGED,
                lot,
                field_name="bidding_status",
                before=before.bidding_status,
                after=lot.bidding_status,
                significant=lot.bidding_status == M.WITHDRAWN,
            )
            touched = True

        if lot.met_reserve and not before.met_reserve:
            add(
                M.RESERVE_MET,
                lot,
                field_name="met_reserve",
                before=before.met_reserve,
                after=True,
                significant=True,
            )
            touched = True

        if lot.closes_at and before.closes_at and lot.closes_at != before.closes_at:
            add(
                M.CLOSE_TIME_CHANGED,
                lot,
                field_name="closes_at",
                before=to_iso(before.closes_at),
                after=to_iso(lot.closes_at),
                significant=lot.closes_at > before.closes_at,
                note="extended" if lot.closes_at > before.closes_at else "brought forward",
            )
            touched = True

        if touched:
            changed_lots.append(lot)

    # --- lots that have disappeared
    for lot in previous:
        if lot.removed_at:
            continue  # already known to be gone
        if _match(lot, current_index) is None:
            removed_lots.append(lot)
            add(M.REMOVED, lot, field_name="lot", before=truncate(lot.description, 200),
                significant=True)

    log.debug(
        "diff complete",
        extra={
            "changes": len(changes),
            "new": len(new_lots),
            "removed": len(removed_lots),
            "changed": len(changed_lots),
        },
    )
    return DiffResult(changes, new_lots, removed_lots, changed_lots)


def summarize_changes(changes: Sequence[Change]) -> dict[str, int]:
    """Count changes by type, for logs and report headers."""
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.change_type] = counts.get(change.change_type, 0) + 1
    return counts


def momentum(
    changes: Sequence[Change], previous_changes: Sequence[Change]
) -> str:
    """Crude trend label comparing this pass's bid activity with the last one."""
    def bids(items: Sequence[Change]) -> int:
        return sum(
            1 for c in items if c.change_type in {M.BID_CHANGED, M.BID_COUNT_CHANGED}
        )

    now, before = bids(changes), bids(previous_changes)
    if now == 0:
        return "no_activity"
    if before == 0:
        return "heating_up"
    if now >= before * 1.3:
        return "heating_up"
    if now <= before * 0.7:
        return "cooling_off"
    return "steady"
