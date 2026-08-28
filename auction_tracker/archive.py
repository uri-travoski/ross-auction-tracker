"""Build a single downloadable zip of everything captured.

Contents:

* ``auctions.db`` — the whole SQLite database (copied with SQLite's own backup
  API, so it is consistent even while the tracker is writing).
* ``data.json`` — a portable JSON dump of auctions, lots and changes, for
  anyone who would rather not open a SQLite file.
* ``reports/`` — the exported HTML reports.
* ``images/`` — every stored thumbnail and original photo (opt-in; this is by
  far the largest part).
* ``MANIFEST.md`` — an inventory with sizes, timestamps and a description.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

from .config import Config
from .logging_setup import get_logger
from .store import Store
from .util import now_utc, to_iso

log = get_logger(__name__)


def _snapshot_database(store: Store, destination: Path) -> None:
    """Consistent copy of the live database."""
    target = sqlite3.connect(str(destination))
    try:
        store.db.connection.backup(target)
    finally:
        target.close()


def export_json(store: Store) -> dict:
    """Portable dump of the dataset."""
    auctions = []
    for auction in store.tracked_auctions(include_finalized=True):
        lots = store.get_lots(auction.id, include_removed=True)
        auctions.append(
            {
                "url": auction.url,
                "site_id": auction.site_id,
                "title": auction.title,
                "status": auction.status,
                "start_at": to_iso(auction.start_at),
                "end_at": to_iso(auction.end_at),
                "end_at_original": to_iso(auction.end_at_original),
                "location": auction.location,
                "description": auction.description,
                "is_it": auction.is_it,
                "it_reason": auction.it_reason,
                "first_seen_at": to_iso(auction.first_seen_at),
                "finalized_at": to_iso(auction.finalized_at),
                "lots": [
                    {
                        "lot_number": lot.lot_number,
                        "site_id": lot.site_id,
                        "description": lot.description,
                        "quantity": lot.quantity,
                        "brand": lot.brand,
                        "model": lot.model,
                        "category": lot.category,
                        "specs": lot.specs,
                        "current_bid": lot.current_bid,
                        "final_bid": lot.final_bid,
                        "bid_count": lot.bid_count,
                        "unique_bidders": lot.unique_bidders,
                        "met_reserve": lot.met_reserve,
                        "closes_at": to_iso(lot.closes_at),
                        "thumbnail_url": lot.thumbnail_url,
                        "image_urls": lot.image_urls,
                        "images": [
                            {"kind": i["kind"], "path": i["local_path"], "url": i["source_url"]}
                            for i in store.images_for_lot(lot.id)
                        ],
                        "removed_at": to_iso(lot.removed_at),
                    }
                    for lot in lots
                ],
                "changes": [
                    {
                        "observed_at": to_iso(c.observed_at),
                        "type": c.change_type,
                        "lot_number": c.lot_number,
                        "field": c.field_name,
                        "previous": c.previous,
                        "current": c.current,
                        "significant": c.significant,
                        "note": c.note,
                    }
                    for c in store.changes_for_auction(auction.id, limit=20000)
                ],
            }
        )
    return {
        "generated_at": to_iso(now_utc()),
        "source": "https://auctions.com.au/auctions/online",
        "note": (
            "Bid figures are hammer prices in AUD, excluding buyer's premium "
            "and GST."
        ),
        "stats": store.stats(),
        "auctions": auctions,
    }


def build_archive(
    config: Config,
    store: Store,
    output: str | Path | None = None,
    *,
    include_images: bool = False,
) -> Path:
    """Write the archive zip and return its path."""
    destination = Path(output) if output else config.path("archive_path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stamp = now_utc()

    db_path = config.path("sqlite_path")
    temp_db = destination.parent / f".archive-db-{stamp:%Y%m%d%H%M%S}.sqlite"
    entries: list[tuple[str, int, str]] = []

    try:
        _snapshot_database(store, temp_db)
        payload = json.dumps(export_json(store), indent=2, default=str)

        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            bundle.write(temp_db, "auctions.db")
            entries.append(("auctions.db", temp_db.stat().st_size, "Full SQLite database"))

            bundle.writestr("data.json", payload)
            entries.append(("data.json", len(payload), "Portable JSON dump of all data"))

            if config.source and Path(config.source).is_file():
                bundle.write(config.source, "config.yaml")
                entries.append(
                    ("config.yaml", Path(config.source).stat().st_size, "Configuration in force")
                )

            reports = config.path("reports_dir")
            if reports.is_dir():
                for path in sorted(reports.glob("*.html")):
                    name = f"reports/{path.name}"
                    bundle.write(path, name)
                    entries.append((name, path.stat().st_size, "Exported HTML report"))

            if include_images:
                images = config.path("images_dir")
                if images.is_dir():
                    for path in sorted(images.rglob("*")):
                        if not path.is_file() or path.suffix == ".part":
                            continue
                        name = f"images/{path.relative_to(images).as_posix()}"
                        # Images are already compressed; storing is much faster.
                        bundle.write(path, name, compress_type=zipfile.ZIP_STORED)
                        entries.append((name, path.stat().st_size, "Stored auction photo"))

            manifest = _manifest(config, store, entries, stamp, include_images)
            bundle.writestr("MANIFEST.md", manifest)
    finally:
        temp_db.unlink(missing_ok=True)

    log.info(
        "archive written",
        extra={
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "files": len(entries) + 1,
            "images_included": include_images,
        },
    )
    return destination


def _manifest(
    config: Config,
    store: Store,
    entries: list[tuple[str, int, str]],
    stamp,
    include_images: bool,
) -> str:
    stats = store.stats()
    lines = [
        "# Auction Tracker archive",
        "",
        f"Generated: {to_iso(stamp)}",
        f"Source: https://auctions.com.au/auctions/online",
        f"Database schema: see `schema_migrations` inside `auctions.db`",
        "",
        "## Contents",
        "",
        f"- IT auctions tracked: {stats['auctions_tracked']}"
        f" ({stats['auctions_live']} live, {stats['auctions_past']} past,"
        f" {stats['auctions_finalized']} finalised)",
        f"- Lots recorded: {stats['lots']:,} ({stats['lots_sold']:,} with a final price)",
        f"- Total of final bids: ${stats['sold_value']:,.2f}",
        f"- Change events: {stats['changes']:,}",
        f"- Images stored: {stats['images']:,}"
        + ("" if include_images else " (not included in this zip — rerun with --images)"),
        "",
        "## Notes",
        "",
        "- All bid figures are hammer prices in AUD and exclude the buyer's",
        "  premium and GST charged by the auction house.",
        "- `final_bid` is set when an auction is finalised, three hours after it",
        "  closes; before then use `current_bid`.",
        "- Times are stored as ISO 8601 with a UTC offset. The auction house",
        f"  publishes local times in {config.get('site.timezone')}.",
        "",
        "## Files",
        "",
        "| File | Size | Description |",
        "| --- | --- | --- |",
    ]
    grouped: dict[str, tuple[int, int, str]] = {}
    for name, size, description in entries:
        if name.startswith("images/"):
            count, total, _ = grouped.get("images/", (0, 0, "Stored auction photos"))
            grouped["images/"] = (count + 1, total + size, "Stored auction photos")
        elif name.startswith("reports/"):
            count, total, _ = grouped.get("reports/", (0, 0, "Exported HTML reports"))
            grouped["reports/"] = (count + 1, total + size, "Exported HTML reports")
        else:
            lines.append(f"| `{name}` | {size:,} bytes | {description} |")
    for prefix, (count, total, description) in grouped.items():
        lines.append(f"| `{prefix}` | {count:,} files, {total:,} bytes | {description} |")
    lines += ["| `MANIFEST.md` | this file | Inventory and notes |", ""]
    return "\n".join(lines)
