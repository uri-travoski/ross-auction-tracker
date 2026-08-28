"""Persistence: every read and write of the SQLite database lives here.

The rest of the codebase never issues SQL. That keeps the schema swappable and
gives the web UI and the scheduler one consistent view of the data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from . import models as M
from .db import Database
from .logging_setup import get_logger
from .models import Auction, Change, Cycle, Lot, ReminderRecord
from .util import (
    clean_text,
    from_iso,
    now_utc,
    to_iso,
    truncate,
)

log = get_logger(__name__)


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _tri(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def _from_tri(value: Any) -> bool | None:
    return None if value is None else bool(value)


# ---------------------------------------------------------------------------
# Row -> model
# ---------------------------------------------------------------------------


def row_to_auction(row: sqlite3.Row) -> Auction:
    return Auction(
        id=row["id"],
        url=row["url"],
        slug=row["slug"],
        site_id=row["site_id"],
        title=row["title"],
        status=row["status"],
        start_at=from_iso(row["start_at"]),
        end_at=from_iso(row["end_at"]),
        end_at_original=from_iso(row["end_at_original"]),
        end_at_updated_at=from_iso(row["end_at_updated_at"]),
        location=row["location"],
        thumbnail_url=row["thumbnail_url"],
        description=row["description"],
        inspection=row["inspection"],
        collection=row["collection"],
        contact=row["contact"],
        terms=row["terms"],
        lot_count=row["lot_count"],
        is_it=bool(row["is_it"]),
        it_reason=row["it_reason"],
        it_confidence=row["it_confidence"],
        it_source=row["it_source"],
        first_seen_at=from_iso(row["first_seen_at"]),
        last_scraped_at=from_iso(row["last_scraped_at"]),
        finalized_at=from_iso(row["finalized_at"]),
        extra=_loads(row["extra_json"], {}),
    )


def row_to_lot(row: sqlite3.Row) -> Lot:
    return Lot(
        id=row["id"],
        auction_id=row["auction_id"],
        lot_number=row["lot_number"],
        site_id=row["site_id"],
        description=row["description"],
        short_description=row["short_description"],
        quantity=row["quantity"],
        thumbnail_url=row["thumbnail_url"],
        image_urls=_loads(row["image_urls_json"], []),
        thumbnail_urls=_loads(row["thumb_urls_json"], []),
        image_count=row["image_count"],
        current_bid=row["current_bid"],
        currency=row["currency"],
        bid_count=row["bid_count"],
        highest_bidder_id=row["highest_bidder_id"],
        bidder_sequence=_loads(row["bidder_seq_json"], []),
        met_reserve=_from_tri(row["met_reserve"]),
        bidding_status=row["bidding_status"],
        status_label=row["status_label"],
        time_remaining=row["time_remaining"],
        closes_at=from_iso(row["closes_at"]),
        opens_at=from_iso(row["opens_at"]),
        bid_url=row["bid_url"],
        category=row["category"],
        brand=row["brand"],
        model=row["model"],
        specs=_loads(row["specs_json"], {}),
        is_it=_from_tri(row["is_it"]),
        final_bid=row["final_bid"],
        first_seen_at=from_iso(row["first_seen_at"]),
        last_seen_at=from_iso(row["last_seen_at"]),
        removed_at=from_iso(row["removed_at"]),
        extra=_loads(row["extra_json"], {}),
    )


def row_to_change(row: sqlite3.Row) -> Change:
    return Change(
        id=row["id"],
        auction_id=row["auction_id"],
        lot_id=row["lot_id"],
        lot_number=row["lot_number"],
        lot_site_id=row["lot_site_id"],
        observed_at=from_iso(row["observed_at"]),
        change_type=row["change_type"],
        field_name=row["field_name"],
        previous=row["prev_value"],
        current=row["new_value"],
        significant=bool(row["significant"]),
        note=row["note"],
        cycle_id=row["cycle_id"],
    )


class Store:
    """Repository over :class:`~auction_tracker.db.Database`."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    # Auctions
    # ==================================================================
    def get_auction(self, auction_id: int) -> Auction | None:
        row = self.db.query_one("SELECT * FROM auctions WHERE id = ?", (auction_id,))
        return row_to_auction(row) if row else None

    def get_auction_by_url(self, url: str) -> Auction | None:
        row = self.db.query_one("SELECT * FROM auctions WHERE url = ?", (url,))
        return row_to_auction(row) if row else None

    def get_auction_by_site_id(self, site_id: str) -> Auction | None:
        if not site_id:
            return None
        row = self.db.query_one("SELECT * FROM auctions WHERE site_id = ?", (site_id,))
        return row_to_auction(row) if row else None

    def upsert_auction(self, auction: Auction) -> tuple[Auction, bool, list[Change]]:
        """Insert or update an auction.

        Returns ``(stored, is_new, changes)``. Auction-level changes (title,
        end-time extensions, status transitions) are returned so the caller can
        record and notify on them. ``end_at_original`` is written once and then
        left alone, which is how extensions are detected.
        """
        existing = self.get_auction_by_url(auction.url)
        stamp = now_utc()
        changes: list[Change] = []

        if existing is None:
            with self.db.write() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO auctions (
                        url, slug, site_id, title, status, start_at, end_at,
                        end_at_original, location, thumbnail_url, description,
                        inspection, collection, contact, terms, lot_count,
                        is_it, it_reason, it_confidence, it_source,
                        first_seen_at, last_scraped_at, extra_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        auction.url,
                        auction.slug,
                        auction.site_id,
                        auction.title,
                        auction.status,
                        to_iso(auction.start_at),
                        to_iso(auction.end_at),
                        to_iso(auction.end_at),
                        auction.location,
                        auction.thumbnail_url,
                        auction.description,
                        auction.inspection,
                        auction.collection,
                        auction.contact,
                        auction.terms,
                        auction.lot_count,
                        int(auction.is_it),
                        auction.it_reason,
                        auction.it_confidence,
                        auction.it_source,
                        to_iso(auction.first_seen_at or stamp),
                        to_iso(auction.last_scraped_at),
                        _dumps(auction.extra),
                    ),
                )
                auction.id = int(cur.lastrowid)
            auction.end_at_original = auction.end_at
            auction.first_seen_at = auction.first_seen_at or stamp
            return auction, True, changes

        # --- update path
        auction.id = existing.id
        auction.first_seen_at = existing.first_seen_at
        auction.end_at_original = existing.end_at_original or auction.end_at
        auction.finalized_at = auction.finalized_at or existing.finalized_at
        end_updated = existing.end_at_updated_at

        if auction.end_at and existing.end_at and auction.end_at != existing.end_at:
            end_updated = stamp
            changes.append(
                Change(
                    change_type=M.CLOSE_TIME_CHANGED,
                    field_name="end_at",
                    previous=to_iso(existing.end_at),
                    current=to_iso(auction.end_at),
                    observed_at=stamp,
                    significant=True,
                    auction_id=existing.id,
                    note="extended" if auction.end_at > existing.end_at else "brought forward",
                )
            )
        if auction.status != existing.status:
            changes.append(
                Change(
                    change_type=M.STATUS_CHANGED,
                    field_name="status",
                    previous=existing.status,
                    current=auction.status,
                    observed_at=stamp,
                    auction_id=existing.id,
                )
            )
        for field_name, before, after in (
            ("title", existing.title, auction.title),
            ("description", existing.description, auction.description),
            ("location", existing.location, auction.location),
            ("terms", existing.terms, auction.terms),
            ("inspection", existing.inspection, auction.inspection),
            ("collection", existing.collection, auction.collection),
        ):
            after = after or ""
            if after and clean_text(before) != clean_text(after):
                changes.append(
                    Change(
                        change_type=M.DESCRIPTION_CHANGED,
                        field_name=field_name,
                        previous=truncate(before or "", 400),
                        current=truncate(after, 400),
                        observed_at=stamp,
                        significant=field_name in {"description", "terms"},
                        auction_id=existing.id,
                    )
                )

        # Keep any previously-known value when this scrape did not supply one.
        def keep(new: Any, old: Any) -> Any:
            return new if (new not in (None, "", 0)) else old

        with self.db.write() as conn:
            conn.execute(
                """
                UPDATE auctions SET
                    slug = ?, site_id = ?, title = ?, status = ?, start_at = ?,
                    end_at = ?, end_at_updated_at = ?, location = ?,
                    thumbnail_url = ?, description = ?, inspection = ?,
                    collection = ?, contact = ?, terms = ?, lot_count = ?,
                    is_it = ?, it_reason = ?, it_confidence = ?, it_source = ?,
                    last_scraped_at = ?, finalized_at = ?, extra_json = ?
                WHERE id = ?
                """,
                (
                    keep(auction.slug, existing.slug),
                    keep(auction.site_id, existing.site_id),
                    keep(auction.title, existing.title),
                    auction.status,
                    to_iso(auction.start_at or existing.start_at),
                    to_iso(auction.end_at or existing.end_at),
                    to_iso(end_updated),
                    keep(auction.location, existing.location),
                    keep(auction.thumbnail_url, existing.thumbnail_url),
                    keep(auction.description, existing.description),
                    keep(auction.inspection, existing.inspection),
                    keep(auction.collection, existing.collection),
                    keep(auction.contact, existing.contact),
                    keep(auction.terms, existing.terms),
                    keep(auction.lot_count, existing.lot_count),
                    int(auction.is_it or existing.is_it),
                    keep(auction.it_reason, existing.it_reason),
                    keep(auction.it_confidence, existing.it_confidence),
                    keep(auction.it_source, existing.it_source),
                    to_iso(auction.last_scraped_at or existing.last_scraped_at),
                    to_iso(auction.finalized_at),
                    _dumps({**existing.extra, **auction.extra}),
                    existing.id,
                ),
            )
        auction.end_at_updated_at = end_updated
        return auction, False, changes

    def mark_scraped(self, auction_id: int, when: datetime | None = None) -> None:
        self.db.execute(
            "UPDATE auctions SET last_scraped_at = ? WHERE id = ?",
            (to_iso(when or now_utc()), auction_id),
        )

    def mark_finalized(self, auction_id: int, when: datetime | None = None) -> None:
        self.db.execute(
            "UPDATE auctions SET status = ?, finalized_at = ? WHERE id = ?",
            (M.FINALIZED, to_iso(when or now_utc()), auction_id),
        )

    def set_status(self, auction_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE auctions SET status = ? WHERE id = ?", (status, auction_id)
        )

    def tracked_auctions(self, *, include_finalized: bool = False) -> list[Auction]:
        sql = "SELECT * FROM auctions WHERE is_it = 1"
        if not include_finalized:
            sql += " AND (finalized_at IS NULL OR finalized_at = '')"
        sql += " ORDER BY end_at IS NULL, end_at"
        return [row_to_auction(r) for r in self.db.query(sql)]

    def auctions_due_for_finalize(self, delay_minutes: int) -> list[Auction]:
        """IT auctions whose close time is at least ``delay_minutes`` ago."""
        cutoff = to_iso(now_utc() - timedelta(minutes=delay_minutes))
        rows = self.db.query(
            """
            SELECT * FROM auctions
            WHERE is_it = 1
              AND (finalized_at IS NULL OR finalized_at = '')
              AND end_at IS NOT NULL AND end_at <= ?
            ORDER BY end_at
            """,
            (cutoff,),
        )
        return [row_to_auction(r) for r in rows]

    def all_urls(self) -> set[str]:
        return {r["url"] for r in self.db.query("SELECT url FROM auctions")}

    # ==================================================================
    # Lots
    # ==================================================================
    def get_lots(self, auction_id: int, *, include_removed: bool = True) -> list[Lot]:
        sql = "SELECT * FROM lots WHERE auction_id = ?"
        if not include_removed:
            sql += " AND removed_at IS NULL"
        sql += " ORDER BY CAST(lot_number AS INTEGER), lot_number"
        return [row_to_lot(r) for r in self.db.query(sql, (auction_id,))]

    def get_lot(self, lot_id: int) -> Lot | None:
        row = self.db.query_one("SELECT * FROM lots WHERE id = ?", (lot_id,))
        return row_to_lot(row) if row else None

    def insert_lot(self, auction_id: int, lot: Lot) -> Lot:
        stamp = now_utc()
        with self.db.write() as conn:
            cur = conn.execute(
                """
                INSERT INTO lots (
                    auction_id, lot_number, site_id, description,
                    short_description, quantity, thumbnail_url, image_urls_json,
                    thumb_urls_json, image_count, current_bid, currency,
                    bid_count, highest_bidder_id, bidder_seq_json, met_reserve,
                    bidding_status, status_label, time_remaining, closes_at,
                    opens_at, bid_url, category, brand, model, specs_json,
                    is_it, final_bid, first_seen_at, last_seen_at, extra_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    auction_id,
                    lot.lot_number,
                    lot.site_id,
                    lot.description,
                    lot.short_description,
                    lot.quantity,
                    lot.thumbnail_url,
                    _dumps(lot.image_urls),
                    _dumps(lot.thumbnail_urls),
                    lot.image_count,
                    lot.current_bid,
                    lot.currency,
                    lot.bid_count,
                    lot.highest_bidder_id,
                    _dumps(lot.bidder_sequence),
                    _tri(lot.met_reserve),
                    lot.bidding_status,
                    lot.status_label,
                    lot.time_remaining,
                    to_iso(lot.closes_at),
                    to_iso(lot.opens_at),
                    lot.bid_url,
                    lot.category,
                    lot.brand,
                    lot.model,
                    _dumps(lot.specs),
                    _tri(lot.is_it),
                    lot.final_bid,
                    to_iso(lot.first_seen_at or stamp),
                    to_iso(lot.last_seen_at or stamp),
                    _dumps(lot.extra),
                ),
            )
            lot.id = int(cur.lastrowid)
        lot.auction_id = auction_id
        lot.first_seen_at = lot.first_seen_at or stamp
        lot.last_seen_at = lot.last_seen_at or stamp
        return lot

    def update_lot(self, lot: Lot) -> None:
        """Persist the current state of a lot. Never clears known-good values."""
        self.db.execute(
            """
            UPDATE lots SET
                site_id = ?, description = ?, short_description = ?,
                quantity = ?, thumbnail_url = ?, image_urls_json = ?,
                thumb_urls_json = ?, image_count = ?, current_bid = ?,
                currency = ?, bid_count = ?, highest_bidder_id = ?,
                bidder_seq_json = ?, met_reserve = ?, bidding_status = ?,
                status_label = ?, time_remaining = ?, closes_at = ?,
                opens_at = ?, bid_url = ?, category = ?, brand = ?, model = ?,
                specs_json = ?, is_it = ?, final_bid = ?, last_seen_at = ?,
                removed_at = ?, extra_json = ?
            WHERE id = ?
            """,
            (
                lot.site_id,
                lot.description,
                lot.short_description,
                lot.quantity,
                lot.thumbnail_url,
                _dumps(lot.image_urls),
                _dumps(lot.thumbnail_urls),
                lot.image_count,
                lot.current_bid,
                lot.currency,
                lot.bid_count,
                lot.highest_bidder_id,
                _dumps(lot.bidder_sequence),
                _tri(lot.met_reserve),
                lot.bidding_status,
                lot.status_label,
                lot.time_remaining,
                to_iso(lot.closes_at),
                to_iso(lot.opens_at),
                lot.bid_url,
                lot.category,
                lot.brand,
                lot.model,
                _dumps(lot.specs),
                _tri(lot.is_it),
                lot.final_bid,
                to_iso(lot.last_seen_at or now_utc()),
                to_iso(lot.removed_at),
                _dumps(lot.extra),
                lot.id,
            ),
        )

    def mark_lot_removed(self, lot_id: int, when: datetime | None = None) -> None:
        self.db.execute(
            "UPDATE lots SET removed_at = ?, bidding_status = ? WHERE id = ?",
            (to_iso(when or now_utc()), M.WITHDRAWN, lot_id),
        )

    def set_lot_specs(
        self,
        lot_id: int,
        *,
        category: str = "",
        brand: str = "",
        model: str = "",
        specs: dict[str, Any] | None = None,
        is_it: bool | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE lots SET category = ?, brand = ?, model = ?, specs_json = ?,
                            is_it = COALESCE(?, is_it)
            WHERE id = ?
            """,
            (category, brand, model, _dumps(specs or {}), _tri(is_it), lot_id),
        )

    def finalize_lots(self, auction_id: int) -> int:
        """Freeze ``final_bid`` from the last observed bid for every lot."""
        cur = self.db.execute(
            """
            UPDATE lots SET final_bid = current_bid, bidding_status = ?
            WHERE auction_id = ? AND removed_at IS NULL
            """,
            (M.LOT_CLOSED, auction_id),
        )
        return cur.rowcount or 0

    def lots_missing_specs(self, limit: int = 100) -> list[Lot]:
        rows = self.db.query(
            """
            SELECT * FROM lots
            WHERE (brand = '' OR brand IS NULL) AND removed_at IS NULL
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [row_to_lot(r) for r in rows]

    # ==================================================================
    # Changes and snapshots
    # ==================================================================
    def record_changes(self, changes: Sequence[Change], cycle_id: str = "") -> int:
        if not changes:
            return 0
        stamp = now_utc()
        rows = [
            (
                c.auction_id,
                c.lot_id,
                c.lot_number,
                c.lot_site_id,
                to_iso(c.observed_at or stamp),
                c.change_type,
                c.field_name,
                None if c.previous is None else str(c.previous),
                None if c.current is None else str(c.current),
                int(c.significant),
                c.note,
                c.cycle_id or cycle_id,
            )
            for c in changes
        ]
        with self.db.write() as conn:
            conn.executemany(
                """
                INSERT INTO lot_changes (
                    auction_id, lot_id, lot_number, lot_site_id, observed_at,
                    change_type, field_name, prev_value, new_value,
                    significant, note, cycle_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        return len(rows)

    def record_snapshot(
        self,
        lot: Lot,
        auction: Auction,
        cycle_id: str = "",
        when: datetime | None = None,
    ) -> None:
        stamp = when or now_utc()
        minutes_to_close = None
        close = lot.closes_at or auction.end_at
        if close:
            minutes_to_close = (close - stamp).total_seconds() / 60.0
        self.db.execute(
            """
            INSERT INTO lot_snapshots (
                lot_id, auction_id, observed_at, current_bid, bid_count,
                unique_bidders, met_reserve, minutes_to_close, cycle_id
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                lot.id,
                auction.id,
                to_iso(stamp),
                lot.current_bid,
                lot.bid_count,
                lot.unique_bidders,
                _tri(lot.met_reserve),
                minutes_to_close,
                cycle_id,
            ),
        )

    def changes_for_auction(
        self,
        auction_id: int,
        *,
        since: datetime | None = None,
        limit: int = 2000,
        significant_only: bool = False,
    ) -> list[Change]:
        sql = "SELECT * FROM lot_changes WHERE auction_id = ?"
        params: list[Any] = [auction_id]
        if since:
            sql += " AND observed_at >= ?"
            params.append(to_iso(since))
        if significant_only:
            sql += " AND significant = 1"
        sql += " ORDER BY observed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [row_to_change(r) for r in self.db.query(sql, tuple(params))]

    def changes_for_cycle(self, cycle_id: str, limit: int = 2000) -> list[Change]:
        rows = self.db.query(
            "SELECT * FROM lot_changes WHERE cycle_id = ? ORDER BY id LIMIT ?",
            (cycle_id, limit),
        )
        return [row_to_change(r) for r in rows]

    def recent_changes(
        self, *, hours: int = 24, significant_only: bool = False, limit: int = 500
    ) -> list[Change]:
        sql = "SELECT * FROM lot_changes WHERE observed_at >= ?"
        params: list[Any] = [to_iso(now_utc() - timedelta(hours=hours))]
        if significant_only:
            sql += " AND significant = 1"
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        return [row_to_change(r) for r in self.db.query(sql, tuple(params))]

    def changed_lot_ids_since(self, auction_id: int, since: datetime) -> set[int]:
        rows = self.db.query(
            """
            SELECT DISTINCT lot_id FROM lot_changes
            WHERE auction_id = ? AND observed_at >= ? AND lot_id IS NOT NULL
            """,
            (auction_id, to_iso(since)),
        )
        return {int(r[0]) for r in rows}

    def snapshots_for_lot(self, lot_id: int, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT observed_at, current_bid, bid_count, unique_bidders,
                   minutes_to_close
            FROM lot_snapshots WHERE lot_id = ?
            ORDER BY observed_at LIMIT ?
            """,
            (lot_id, limit),
        )
        return [dict(r) for r in rows]

    def bid_velocity(
        self, auction_id: int, window_minutes: int = 60
    ) -> list[tuple[int, float, int]]:
        """``(lot_id, bid_delta, count_delta)`` over the trailing window.

        Used to pick the "hot lots" for reminder emails.
        """
        cutoff = to_iso(now_utc() - timedelta(minutes=window_minutes))
        rows = self.db.query(
            """
            SELECT lot_id,
                   MAX(current_bid) - MIN(current_bid) AS bid_delta,
                   MAX(bid_count)   - MIN(bid_count)   AS count_delta
            FROM lot_snapshots
            WHERE auction_id = ? AND observed_at >= ?
            GROUP BY lot_id
            HAVING count_delta > 0 OR bid_delta > 0
            ORDER BY bid_delta DESC
            """,
            (auction_id, cutoff),
        )
        return [(int(r[0]), float(r[1] or 0), int(r[2] or 0)) for r in rows]

    # ==================================================================
    # Images
    # ==================================================================
    def image_by_url(self, url: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM images WHERE source_url = ?", (url,))
        return dict(row) if row else None

    def image_by_hash(self, sha256: str) -> dict[str, Any] | None:
        if not sha256:
            return None
        row = self.db.query_one(
            "SELECT * FROM images WHERE sha256 = ? AND local_path != '' LIMIT 1",
            (sha256,),
        )
        return dict(row) if row else None

    def record_image(
        self,
        *,
        source_url: str,
        kind: str,
        auction_id: int | None = None,
        lot_id: int | None = None,
        site_image_id: str = "",
        local_path: str = "",
        sha256: str = "",
        byte_size: int = 0,
        content_type: str = "",
        error: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO images (
                auction_id, lot_id, kind, source_url, site_image_id,
                local_path, sha256, byte_size, content_type, downloaded_at, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_url) DO UPDATE SET
                auction_id = COALESCE(excluded.auction_id, images.auction_id),
                lot_id = COALESCE(excluded.lot_id, images.lot_id),
                local_path = CASE WHEN excluded.local_path != ''
                                  THEN excluded.local_path ELSE images.local_path END,
                sha256 = CASE WHEN excluded.sha256 != ''
                              THEN excluded.sha256 ELSE images.sha256 END,
                byte_size = MAX(excluded.byte_size, images.byte_size),
                content_type = CASE WHEN excluded.content_type != ''
                                    THEN excluded.content_type ELSE images.content_type END,
                downloaded_at = COALESCE(excluded.downloaded_at, images.downloaded_at),
                error = excluded.error
            """,
            (
                auction_id,
                lot_id,
                kind,
                source_url,
                site_image_id,
                local_path,
                sha256,
                byte_size,
                content_type,
                to_iso(now_utc()) if local_path else None,
                error,
            ),
        )

    def images_for_lot(self, lot_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM images WHERE lot_id = ? ORDER BY kind, site_image_id, id",
            (lot_id,),
        )
        return [dict(r) for r in rows]

    def image_counts(self) -> dict[str, int]:
        rows = self.db.query(
            """
            SELECT kind, COUNT(*) AS n, COALESCE(SUM(byte_size), 0) AS bytes
            FROM images WHERE local_path != '' GROUP BY kind
            """
        )
        out = {r["kind"]: int(r["n"]) for r in rows}
        out["total_bytes"] = sum(int(r["bytes"]) for r in rows)
        return out

    # ==================================================================
    # Cycles
    # ==================================================================
    def start_cycle(self, cycle: Cycle) -> Cycle:
        cycle.started_at = cycle.started_at or now_utc()
        self.db.execute(
            "INSERT OR REPLACE INTO cycles (id, cycle_type, started_at) VALUES (?,?,?)",
            (cycle.id, cycle.cycle_type, to_iso(cycle.started_at)),
        )
        return cycle

    def finish_cycle(self, cycle: Cycle) -> None:
        cycle.finished_at = cycle.finished_at or now_utc()
        self.db.execute(
            """
            UPDATE cycles SET
                finished_at = ?, pages_scanned = ?, auctions_seen = ?,
                auctions_new = ?, auctions_scraped = ?, lots_seen = ?,
                lots_changed = ?, changes_recorded = ?, images_downloaded = ?,
                ai_calls = ?, emails_sent = ?, errors_json = ?, ai_summary = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                to_iso(cycle.finished_at),
                cycle.pages_scanned,
                cycle.auctions_seen,
                cycle.auctions_new,
                cycle.auctions_scraped,
                cycle.lots_seen,
                cycle.lots_changed,
                cycle.changes_recorded,
                cycle.images_downloaded,
                cycle.ai_calls,
                cycle.emails_sent,
                _dumps(cycle.errors),
                cycle.ai_summary,
                cycle.notes,
                cycle.id,
            ),
        )

    def recent_cycles(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["errors"] = _loads(d.pop("errors_json", "[]"), [])
            out.append(d)
        return out

    def last_cycle_of_type(self, cycle_type: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            """
            SELECT * FROM cycles WHERE cycle_type = ? AND finished_at IS NOT NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (cycle_type,),
        )
        return dict(row) if row else None

    def last_scrape_time(self, auction_id: int) -> datetime | None:
        row = self.db.query_one(
            "SELECT last_scraped_at FROM auctions WHERE id = ?", (auction_id,)
        )
        return from_iso(row[0]) if row else None

    # ==================================================================
    # Reminders
    # ==================================================================
    def reminder_already_sent(
        self, auction_id: int, lead_minutes: int, scheduled_for: datetime
    ) -> bool:
        row = self.db.query_one(
            """
            SELECT delivery_status FROM reminder_log
            WHERE auction_id = ? AND lead_minutes = ? AND scheduled_for = ?
            """,
            (auction_id, lead_minutes, to_iso(scheduled_for)),
        )
        # A previous FAILED attempt is allowed to be retried.
        return bool(row and row["delivery_status"] == "SENT")

    def log_reminder(self, record: ReminderRecord) -> None:
        self.db.execute(
            """
            INSERT INTO reminder_log (
                auction_id, lead_minutes, scheduled_for, sent_at,
                delivery_status, recipients, message_preview
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(auction_id, lead_minutes, scheduled_for) DO UPDATE SET
                sent_at = excluded.sent_at,
                delivery_status = excluded.delivery_status,
                recipients = excluded.recipients,
                message_preview = excluded.message_preview
            """,
            (
                record.auction_id,
                record.lead_minutes,
                to_iso(record.scheduled_for),
                to_iso(record.sent_at or now_utc()),
                record.delivery_status,
                ", ".join(record.recipients),
                truncate(record.message_preview, 500),
            ),
        )

    def reminders_for_auction(self, auction_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM reminder_log WHERE auction_id = ? ORDER BY lead_minutes DESC",
            (auction_id,),
        )
        return [dict(r) for r in rows]

    # ==================================================================
    # Email log
    # ==================================================================
    def log_email(
        self,
        *,
        event_type: str,
        subject: str,
        recipients: Iterable[str],
        auction_id: int | None = None,
        status: str = "SENT",
        error: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO email_log (
                sent_at, event_type, auction_id, subject, recipients, status, error
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                to_iso(now_utc()),
                event_type,
                auction_id,
                truncate(subject, 300),
                ", ".join(recipients),
                status,
                truncate(error, 500),
            ),
        )

    def emails_sent_since(self, minutes: int = 60) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM email_log WHERE sent_at >= ? AND status = 'SENT'",
                (to_iso(now_utc() - timedelta(minutes=minutes)),),
            )
            or 0
        )

    def recent_emails(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM email_log ORDER BY sent_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    # ==================================================================
    # AI cache + call log
    # ==================================================================
    def ai_cache_get(self, task: str, input_hash: str, ttl_days: int = 30) -> str | None:
        row = self.db.query_one(
            "SELECT response, created_at FROM ai_cache WHERE task = ? AND input_hash = ?",
            (task, input_hash),
        )
        if not row:
            return None
        created = from_iso(row["created_at"])
        if created and ttl_days > 0 and now_utc() - created > timedelta(days=ttl_days):
            return None
        return row["response"]

    def ai_cache_put(
        self, task: str, input_hash: str, response: str, provider: str, model: str
    ) -> None:
        self.db.execute(
            """
            INSERT INTO ai_cache (task, input_hash, provider, model, response, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(task, input_hash) DO UPDATE SET
                response = excluded.response, provider = excluded.provider,
                model = excluded.model, created_at = excluded.created_at
            """,
            (task, input_hash, provider, model, response, to_iso(now_utc())),
        )

    def log_ai_call(
        self,
        *,
        task: str,
        provider: str = "",
        model: str = "",
        cycle_id: str = "",
        auction_id: int | None = None,
        lot_id: int | None = None,
        cached: bool = False,
        ok: bool = True,
        duration_ms: int = 0,
        error: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO ai_calls (
                called_at, cycle_id, task, provider, model, auction_id, lot_id,
                cached, ok, duration_ms, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                to_iso(now_utc()),
                cycle_id,
                task,
                provider,
                model,
                auction_id,
                lot_id,
                int(cached),
                int(ok),
                duration_ms,
                truncate(error, 400),
            ),
        )

    def ai_call_stats(self, hours: int = 24) -> dict[str, Any]:
        row = self.db.query_one(
            """
            SELECT COUNT(*) AS total,
                   SUM(cached) AS cached,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed
            FROM ai_calls WHERE called_at >= ?
            """,
            (to_iso(now_utc() - timedelta(hours=hours)),),
        )
        return {
            "total": int(row["total"] or 0),
            "cached": int(row["cached"] or 0),
            "failed": int(row["failed"] or 0),
        }

    # ==================================================================
    # Price estimates
    # ==================================================================
    def save_estimate(
        self,
        *,
        question: str,
        answer: str,
        comparables: list[dict[str, Any]],
        max_price: float | None,
        provider: str,
        model: str,
        lot_id: int | None = None,
    ) -> int:
        cur = self.db.execute(
            """
            INSERT INTO price_estimates (
                created_at, question, lot_id, comparables, answer, max_price,
                provider, model
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                to_iso(now_utc()),
                question,
                lot_id,
                _dumps(comparables),
                answer,
                max_price,
                provider,
                model,
            ),
        )
        return int(cur.lastrowid or 0)

    def recent_estimates(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM price_estimates ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["comparables"] = _loads(d.get("comparables"), [])
            out.append(d)
        return out

    # ==================================================================
    # Search / listing for the web UI
    # ==================================================================
    def search_auctions(
        self,
        *,
        scope: str = "all",
        query: str = "",
        location: str = "",
        status: str = "",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        only_it: bool = True,
        sort: str = "end_desc",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[Auction], int]:
        """Paginated auction search. ``scope`` is ``current``/``past``/``all``."""
        where: list[str] = ["1=1"]
        params: list[Any] = []
        if only_it:
            where.append("is_it = 1")
        now_iso = to_iso(now_utc())
        if scope == "current":
            where.append("(end_at IS NULL OR end_at > ?)")
            params.append(now_iso)
        elif scope == "past":
            where.append("(end_at IS NOT NULL AND end_at <= ?)")
            params.append(now_iso)
        if status:
            where.append("status = ?")
            params.append(status)
        if location:
            where.append("location LIKE ?")
            params.append(f"%{location}%")
        if query:
            where.append("(title LIKE ? OR description LIKE ? OR url LIKE ?)")
            params += [f"%{query}%"] * 3
        if date_from:
            where.append("end_at >= ?")
            params.append(to_iso(date_from))
        if date_to:
            where.append("end_at <= ?")
            params.append(to_iso(date_to))

        clause = " AND ".join(where)
        total = int(
            self.db.scalar(f"SELECT COUNT(*) FROM auctions WHERE {clause}", tuple(params))
            or 0
        )
        order = {
            "end_desc": "end_at IS NULL, end_at DESC",
            "end_asc": "end_at IS NULL, end_at ASC",
            "title": "title COLLATE NOCASE",
            "lots": "lot_count DESC",
            "first_seen": "first_seen_at DESC",
        }.get(sort, "end_at IS NULL, end_at DESC")
        page = max(1, int(page))
        rows = self.db.query(
            f"SELECT * FROM auctions WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (page_size, (page - 1) * page_size),
        )
        return [row_to_auction(r) for r in rows], total

    def search_lots(
        self,
        *,
        query: str = "",
        auction_ids: Sequence[int] | None = None,
        brand: str = "",
        category: str = "",
        scope: str = "all",
        min_bid: float | None = None,
        max_bid: float | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        with_bids_only: bool = False,
        sold_only: bool = False,
        sort: str = "close_desc",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[list[tuple[Lot, Auction]], int]:
        """Paginated lot search joined to its auction, for the UI tables."""
        where = ["1=1"]
        params: list[Any] = []
        if query:
            like = f"%{query}%"
            where.append(
                "(l.description LIKE ? OR l.short_description LIKE ? OR"
                " l.brand LIKE ? OR l.model LIKE ? OR l.lot_number = ? OR a.title LIKE ?)"
            )
            params += [like, like, like, like, query, like]
        if auction_ids:
            where.append(
                "l.auction_id IN (%s)" % ",".join("?" for _ in auction_ids)
            )
            params += list(auction_ids)
        if brand:
            where.append("l.brand LIKE ?")
            params.append(f"%{brand}%")
        if category:
            where.append("l.category LIKE ?")
            params.append(f"%{category}%")
        now_iso = to_iso(now_utc())
        if scope == "current":
            where.append("(a.end_at IS NULL OR a.end_at > ?)")
            params.append(now_iso)
        elif scope == "past":
            where.append("(a.end_at IS NOT NULL AND a.end_at <= ?)")
            params.append(now_iso)
        if min_bid is not None:
            where.append("COALESCE(l.final_bid, l.current_bid, 0) >= ?")
            params.append(min_bid)
        if max_bid is not None:
            where.append("COALESCE(l.final_bid, l.current_bid, 0) <= ?")
            params.append(max_bid)
        if with_bids_only:
            where.append("l.bid_count > 0")
        if sold_only:
            where.append("l.final_bid IS NOT NULL AND l.final_bid > 0")
        if date_from:
            where.append("COALESCE(l.closes_at, a.end_at) >= ?")
            params.append(to_iso(date_from))
        if date_to:
            where.append("COALESCE(l.closes_at, a.end_at) <= ?")
            params.append(to_iso(date_to))

        clause = " AND ".join(where)
        base = f"FROM lots l JOIN auctions a ON a.id = l.auction_id WHERE {clause}"
        total = int(self.db.scalar(f"SELECT COUNT(*) {base}", tuple(params)) or 0)
        order = {
            "close_desc": "COALESCE(l.closes_at, a.end_at) DESC",
            "close_asc": "COALESCE(l.closes_at, a.end_at) ASC",
            "bid_desc": "COALESCE(l.final_bid, l.current_bid, 0) DESC",
            "bid_asc": "COALESCE(l.final_bid, l.current_bid, 0) ASC",
            "bids_desc": "l.bid_count DESC",
            "lot": "l.auction_id, CAST(l.lot_number AS INTEGER)",
        }.get(sort, "COALESCE(l.closes_at, a.end_at) DESC")
        page = max(1, int(page))
        rows = self.db.query(
            f"SELECT l.*, a.id AS a_id {base} ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (page_size, (page - 1) * page_size),
        )
        auctions: dict[int, Auction] = {}
        out: list[tuple[Lot, Auction]] = []
        for row in rows:
            lot = row_to_lot(row)
            aid = int(row["a_id"])
            if aid not in auctions:
                found = self.get_auction(aid)
                if found is None:
                    continue
                auctions[aid] = found
            out.append((lot, auctions[aid]))
        return out, total

    def comparable_lots(
        self,
        *,
        terms: Sequence[str],
        limit: int = 60,
        sold_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Historical lots matching any search term — the AI's evidence base."""
        terms = [clean_text(t) for t in terms if clean_text(t)]
        if not terms:
            return []
        conditions = " OR ".join(
            ["(l.description LIKE ? OR l.brand LIKE ? OR l.model LIKE ?)"] * len(terms)
        )
        params: list[Any] = []
        for term in terms:
            params += [f"%{term}%"] * 3
        sql = f"""
            SELECT l.lot_number, l.description, l.brand, l.model, l.quantity,
                   l.current_bid, l.final_bid, l.bid_count, l.specs_json,
                   a.title AS auction_title, a.end_at, a.url AS auction_url
            FROM lots l JOIN auctions a ON a.id = l.auction_id
            WHERE ({conditions})
        """
        if sold_only:
            sql += " AND l.final_bid IS NOT NULL AND l.final_bid > 0"
        sql += " ORDER BY a.end_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in self.db.query(sql, tuple(params)):
            d = dict(row)
            d["specs"] = _loads(d.pop("specs_json", "{}"), {})
            out.append(d)
        return out

    def distinct_values(self, column: str, limit: int = 200) -> list[str]:
        """Distinct non-empty values for a filter dropdown."""
        if column not in {"brand", "category", "location", "status"}:
            raise ValueError(f"refusing to query column {column!r}")
        table = "auctions" if column in {"location", "status"} else "lots"
        rows = self.db.query(
            f"""
            SELECT DISTINCT {column} FROM {table}
            WHERE {column} IS NOT NULL AND {column} != ''
            ORDER BY {column} COLLATE NOCASE LIMIT ?
            """,
            (limit,),
        )
        return [str(r[0]) for r in rows]

    # ==================================================================
    # Dashboard stats
    # ==================================================================
    def stats(self) -> dict[str, Any]:
        now_iso = to_iso(now_utc())
        q = self.db.scalar
        sold = self.db.query_one(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(final_bid), 0) AS total,
                   COALESCE(AVG(final_bid), 0) AS avg
            FROM lots WHERE final_bid IS NOT NULL AND final_bid > 0
            """
        )
        return {
            "auctions_tracked": int(q("SELECT COUNT(*) FROM auctions WHERE is_it = 1") or 0),
            "auctions_total": int(q("SELECT COUNT(*) FROM auctions") or 0),
            "auctions_live": int(
                q(
                    "SELECT COUNT(*) FROM auctions WHERE is_it = 1 AND"
                    " (end_at IS NULL OR end_at > ?)",
                    (now_iso,),
                )
                or 0
            ),
            "auctions_past": int(
                q(
                    "SELECT COUNT(*) FROM auctions WHERE is_it = 1 AND"
                    " end_at IS NOT NULL AND end_at <= ?",
                    (now_iso,),
                )
                or 0
            ),
            "auctions_finalized": int(
                q("SELECT COUNT(*) FROM auctions WHERE finalized_at IS NOT NULL") or 0
            ),
            "lots": int(q("SELECT COUNT(*) FROM lots") or 0),
            "lots_sold": int(sold["n"] or 0),
            "sold_value": float(sold["total"] or 0),
            "avg_sold": float(sold["avg"] or 0),
            "changes": int(q("SELECT COUNT(*) FROM lot_changes") or 0),
            "changes_24h": int(
                q(
                    "SELECT COUNT(*) FROM lot_changes WHERE observed_at >= ?",
                    (to_iso(now_utc() - timedelta(hours=24)),),
                )
                or 0
            ),
            "images": int(q("SELECT COUNT(*) FROM images WHERE local_path != ''") or 0),
            "emails_24h": int(
                q(
                    "SELECT COUNT(*) FROM email_log WHERE sent_at >= ?",
                    (to_iso(now_utc() - timedelta(hours=24)),),
                )
                or 0
            ),
            "ai": self.ai_call_stats(24),
            "last_discovery": (self.last_cycle_of_type(M.DISCOVERY) or {}).get("started_at"),
            "last_change_scan": (self.last_cycle_of_type(M.CHANGE) or {}).get("started_at"),
        }
