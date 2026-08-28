"""Standalone HTML report export.

Two products, both rendered from the same templates the live UI uses, with the
stylesheet inlined so a single file can be emailed or archived:

* ``report_YYYY-MM-DD_HHMM.html`` — a snapshot of everything tracked, grouped
  by status, with the changes from the most recent scans highlighted.
* ``final_<id>_<slug>.html`` — the permanent "what actually happened" record for
  one finalised auction: every lot, its final price, and the timeline of
  significant changes observed while it ran.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from . import models as M
from .config import Config
from .logging_setup import get_logger
from .models import Auction
from .store import Store
from .templating import build_environment, inline_css
from .util import now_utc, slugify

log = get_logger(__name__)


def _auction_row(store: Store, auction: Auction, *, since_hours: int = 24) -> dict:
    lots = store.get_lots(auction.id, include_removed=False)
    auction.lots = lots
    final = [lot for lot in lots if lot.final_bid]
    total = sum((lot.final_bid if lot.final_bid else (lot.current_bid or 0)) for lot in lots)
    changes = store.changes_for_auction(
        auction.id, since=now_utc() - timedelta(hours=since_hours), limit=400
    )
    return {
        "auction": auction,
        "lots": lots,
        "lot_count": len(lots),
        "total_bids": sum(lot.bid_count for lot in lots),
        "total_value": total,
        "is_final": bool(final),
        "changes": changes,
        "top_lots": sorted(
            lots, key=lambda l: (l.final_bid or l.current_bid or 0), reverse=True
        )[:5],
        "reminders": [
            r for r in store.reminders_for_auction(auction.id)
            if r.get("delivery_status") == "SENT"
        ],
        "in_final_stretch": auction.in_final_stretch(180),
    }


def render_snapshot(config: Config, store: Store, *, since_hours: int = 24) -> str:
    """Render the tracked-auctions snapshot report to an HTML string."""
    env = build_environment(config)
    template = env.get_template("report_snapshot.html")
    now = now_utc()

    buckets: dict[str, list[dict]] = {
        M.IN_PROGRESS: [],
        M.FORTHCOMING: [],
        M.CLOSED: [],
        M.FINALIZED: [],
    }
    for auction in store.tracked_auctions(include_finalized=True):
        row = _auction_row(store, auction, since_hours=since_hours)
        status = auction.computed_status(now)
        buckets.setdefault(status, []).append(row)

    for rows in buckets.values():
        rows.sort(key=lambda r: (r["auction"].end_at is None, r["auction"].end_at))

    groups = [
        {"label": "In progress", "rows": buckets.get(M.IN_PROGRESS, [])},
        {"label": "Forthcoming", "rows": buckets.get(M.FORTHCOMING, [])},
        {"label": "Closed — awaiting final record", "rows": buckets.get(M.CLOSED, [])},
        {"label": "Finalised", "rows": buckets.get(M.FINALIZED, [])},
    ]
    last_cycle = (store.recent_cycles(1) or [{}])[0]
    return template.render(
        standalone=True,
        inline_css=inline_css(),
        generated_at=now,
        stats=store.stats(),
        groups=groups,
        ai_summary=last_cycle.get("ai_summary", ""),
    )


def write_snapshot_report(config: Config, store: Store) -> str:
    """Write the snapshot report into ``storage.reports_dir``."""
    html = render_snapshot(config, store)
    directory = config.path("reports_dir")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().astimezone(config.timezone).strftime("%Y-%m-%d_%H%M")
    path = directory / f"report_{stamp}.html"
    path.write_text(html, encoding="utf-8")
    # A stable filename so a bookmark or a static host always has the latest.
    (directory / "latest.html").write_text(html, encoding="utf-8")
    log.info("snapshot report written", extra={"path": str(path), "bytes": len(html)})
    return str(path)


def render_final_report(config: Config, store: Store, auction: Auction) -> str:
    """Render the permanent record for one finalised auction."""
    env = build_environment(config)
    template = env.get_template("report_final.html")
    lots = store.get_lots(auction.id, include_removed=True)
    auction.lots = [lot for lot in lots if not lot.removed_at]
    sold = [lot for lot in lots if (lot.final_bid or 0) > 0]
    prices = [lot.final_bid or 0 for lot in sold]
    timeline = [
        change
        for change in store.changes_for_auction(auction.id, limit=4000)
        if change.significant
    ]
    timeline.sort(key=lambda c: c.observed_at or now_utc())
    return template.render(
        standalone=True,
        inline_css=inline_css(),
        generated_at=now_utc(),
        auction=auction,
        lots=lots,
        sold=sold,
        total_value=sum(prices),
        average=(sum(prices) / len(prices)) if prices else 0.0,
        highest=max(prices) if prices else 0.0,
        total_bids=sum(lot.bid_count for lot in lots),
        timeline=timeline,
        finalize_delay_hours=int(config.get("schedule.finalize_delay_minutes", 180)) // 60,
    )


def write_final_report(config: Config, store: Store, auction: Auction) -> str:
    html = render_final_report(config, store, auction)
    directory = config.path("reports_dir")
    directory.mkdir(parents=True, exist_ok=True)
    name = f"final_{auction.id}_{slugify(auction.title, 60)}.html"
    path = directory / name
    path.write_text(html, encoding="utf-8")
    log.info(
        "final report written",
        extra={"path": str(path), "auction": auction.url, "bytes": len(html)},
    )
    return str(path)


def purge_old_reports(config: Config) -> int:
    """Delete snapshot reports older than the configured retention.

    Final reports and everything in the database are never touched.
    """
    days = int(config.get("storage.purge_reports_older_than_days", 365))
    if days <= 0:
        return 0
    directory = config.path("reports_dir")
    if not directory.is_dir():
        return 0
    cutoff = now_utc() - timedelta(days=days)
    removed = 0
    for path in directory.glob("report_*.html"):
        try:
            from datetime import datetime, timezone

            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("could not purge %s: %s", path, exc)
    if removed:
        log.info("purged old snapshot reports", extra={"count": removed, "days": days})
    return removed
