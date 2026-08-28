"""The pipeline: what actually happens on each kind of scan.

Cycle types and the requirement each one satisfies:

===================  ===========================================================
``DISCOVERY_24H``    Every 24h: walk the whole index, find new IT auctions,
                     classify them (keywords + AI), email on anything new.
``CHANGE_6H``        Every 6h: re-scrape every tracked auction, diff against the
                     stored snapshot, record and highlight what changed.
``FINAL_STRETCH_30M``Inside the last 3h: the same scan every 30 minutes, so
                     bidding behaviour near the close is captured.
``REMINDER``         3h and 30min before the close: refresh state and email a
                     summary. Deduplicated per (auction, lead, close time).
``FINALIZE``         3h after the close: final scrape, freeze final prices, mark
                     the auction finalised and email the results.
===================  ===========================================================

Nothing here aborts a whole cycle because one auction failed: errors are
collected onto the :class:`~auction_tracker.models.Cycle` record and the run
continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from . import models as M
from .ai import AIEngine
from .changes import DiffResult, SignificanceRules, diff_lots, summarize_changes
from .config import Config
from .fetch import Fetcher, FetchError, build_fetcher
from .images import ImageDownloader
from .logging_setup import get_logger
from .models import Auction, Change, Cycle, Lot
from .notify import Notifier, build_notifier
from .scrape import (
    ITClassifier,
    apply_bid_feed,
    crawl_index,
    detail_validator,
    enrich_lot_images,
    fetch_bid_feed,
    parse_detail_page,
)
from .store import Store
from .util import money, new_cycle_id, now_utc, to_iso, truncate

log = get_logger(__name__)

# Keys used inside ``auctions.extra_json`` to remember one-off notifications.
FINAL_STRETCH_FLAG = "final_stretch_notified_for"
LAST_CLASSIFIED = "classified_at"


@dataclass
class ScrapeOutcome:
    """Result of scraping one auction."""

    auction: Auction
    diff: DiffResult | None = None
    is_new: bool = False
    images: int = 0
    error: str = ""
    auction_changes: list[Change] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def all_changes(self) -> list[Change]:
        return list(self.auction_changes) + (list(self.diff.changes) if self.diff else [])

    @property
    def significant(self) -> list[Change]:
        return [c for c in self.all_changes if c.significant]


class Pipeline:
    """Owns the fetcher, notifier and AI engine for the duration of a run."""

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        fetcher: Fetcher | None = None,
        notifier: Notifier | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.store = store
        self.dry_run = dry_run
        self._own_fetcher = fetcher is None
        self.fetcher = fetcher or build_fetcher(config)
        self.notifier = notifier or build_notifier(config, store)
        self.rules = SignificanceRules.from_config(config)
        self.final_stretch_minutes = int(config.get("schedule.final_stretch_minutes", 180))
        self.finalize_delay = int(config.get("schedule.finalize_delay_minutes", 180))
        self.poll_minutes = int(config.get("schedule.final_stretch_poll_minutes", 30))

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def close(self) -> None:
        if self._own_fetcher:
            self.fetcher.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _new_cycle(self, cycle_type: str) -> tuple[Cycle, AIEngine, ITClassifier]:
        cycle = Cycle(cycle_type=cycle_type, id=new_cycle_id(), started_at=now_utc())
        self.store.start_cycle(cycle)
        engine = AIEngine(self.config, self.store, cycle.id)
        classifier = ITClassifier(self.config, engine)
        log.info("cycle started", extra={"cycle": cycle.id, "type": cycle_type})
        return cycle, engine, classifier

    def _close_cycle(self, cycle: Cycle, engine: AIEngine) -> Cycle:
        cycle.ai_calls = engine.calls_made
        cycle.emails_sent = self.notifier.sent
        cycle.finished_at = now_utc()
        self.store.finish_cycle(cycle)
        log.info(
            "cycle finished",
            extra={
                "cycle": cycle.id,
                "type": cycle.cycle_type,
                "seconds": round(cycle.duration_seconds or 0, 1),
                "auctions": cycle.auctions_scraped,
                "new": cycle.auctions_new,
                "lots": cycle.lots_seen,
                "changes": cycle.changes_recorded,
                "images": cycle.images_downloaded,
                "ai_calls": cycle.ai_calls,
                "emails": cycle.emails_sent,
                "errors": len(cycle.errors),
            },
        )
        return cycle

    # ==================================================================
    # DISCOVERY — every 24 hours
    # ==================================================================
    def run_discovery(self, *, notify: bool = True) -> Cycle:
        cycle, engine, classifier = self._new_cycle(M.DISCOVERY)
        try:
            found, pages, diagnostics = crawl_index(self.fetcher, self.config)
            cycle.pages_scanned = pages
            cycle.auctions_seen = len(found)
            cycle.notes = str(diagnostics.get("termination_reason", ""))

            minimum = int(self.config.get("site.min_auctions_expected", 1))
            if len(found) < minimum:
                cycle.errors.append(
                    f"discovery found {len(found)} auctions, expected at least {minimum}"
                )

            known = self.store.all_urls()
            for candidate in found:
                try:
                    self._process_discovered(
                        candidate, known, cycle, engine, classifier, notify=notify
                    )
                except Exception as exc:
                    message = f"{candidate.url}: {type(exc).__name__}: {exc}"
                    cycle.errors.append(message)
                    log.exception("discovery failed for auction", extra={"url": candidate.url})

            self._summarize(cycle, engine, [])
        finally:
            self._close_cycle(cycle, engine)
        return cycle

    def _process_discovered(
        self,
        candidate: Auction,
        known: set[str],
        cycle: Cycle,
        engine: AIEngine,
        classifier: ITClassifier,
        *,
        notify: bool,
    ) -> None:
        """Classify one index card and, if relevant, scrape it in full."""
        existing = self.store.get_auction_by_url(candidate.url)
        first_sighting = existing is None

        # Known and already judged IT: just refresh the metadata; the 6-hourly
        # change scan handles its lots.
        if existing is not None and existing.is_it:
            merged = self._merge_card_into(existing, candidate)
            _, _, auction_changes = self.store.upsert_auction(merged)
            if auction_changes:
                cycle.changes_recorded += self.store.record_changes(auction_changes, cycle.id)
                self._handle_extension(merged, auction_changes)
            return

        # Known and judged non-IT: re-check only if the title changed.
        if existing is not None and not existing.is_it:
            if candidate.title and candidate.title == existing.title:
                return
            log.info(
                "re-classifying auction after title change",
                extra={"url": candidate.url, "title": candidate.title},
            )

        pre = classifier.pre_filter(candidate)
        # Even a "no" gets its detail page read once: a mixed auction (say a
        # medical clearance full of laptops) never announces IT in its title.
        outcome = self.scrape_auction(
            candidate,
            cycle,
            engine,
            classifier,
            download_images=False,
            persist_lots=False,
        )
        if not outcome.ok:
            cycle.errors.append(outcome.error)
            return

        scraped = outcome.auction
        decision = classifier.confirm(scraped)
        decision.apply(scraped)
        scraped.extra[LAST_CLASSIFIED] = to_iso(now_utc())
        log.info(
            "classified auction",
            extra={
                "url": scraped.url,
                "is_it": decision.is_it,
                "confidence": round(decision.confidence, 2),
                "source": decision.source,
                "pre_filter": pre.is_it,
                "reason": truncate(decision.reason, 120),
            },
        )

        if not decision.is_it:
            # Record the rejection so the next discovery does not redo the work.
            self.store.upsert_auction(scraped)
            return

        # It is an IT auction: store it properly, with lots and images.
        full = self.scrape_auction(scraped, cycle, engine, classifier)
        if not full.ok:
            cycle.errors.append(full.error)
            return
        cycle.auctions_scraped += 1
        if first_sighting:
            cycle.auctions_new += 1
            if notify:
                self.notifier.new_auction(full.auction)

    @staticmethod
    def _merge_card_into(existing: Auction, card: Auction) -> Auction:
        """Refresh a stored auction with the fresher facts from an index card."""
        for attr in ("title", "status", "location", "thumbnail_url", "start_at", "end_at"):
            value = getattr(card, attr)
            if value:
                setattr(existing, attr, value)
        return existing

    # ==================================================================
    # CHANGE / FINAL STRETCH scans
    # ==================================================================
    def run_change_scan(
        self,
        *,
        cycle_type: str = M.CHANGE,
        auctions: Sequence[Auction] | None = None,
        notify: bool = True,
        download_images: bool = True,
    ) -> Cycle:
        cycle, engine, classifier = self._new_cycle(cycle_type)
        touched: list[Auction] = []
        all_changes: list[Change] = []
        try:
            targets = list(auctions) if auctions is not None else self._scan_targets()
            cycle.auctions_seen = len(targets)
            log.info(
                "scanning auctions", extra={"count": len(targets), "type": cycle_type}
            )
            for auction in targets:
                outcome = self.scrape_auction(auction, cycle, engine, classifier,
                                              download_images=download_images)
                if not outcome.ok:
                    cycle.errors.append(outcome.error)
                    continue
                cycle.auctions_scraped += 1
                touched.append(outcome.auction)
                all_changes.extend(outcome.all_changes)
                self._handle_extension(outcome.auction, outcome.auction_changes)
                # On a first capture every lot is "new"; that is not news worth
                # emailing, and the new-auction alert already covers it.
                if notify and outcome.significant and not outcome.is_new:
                    self.notifier.significant_changes(
                        outcome.auction, outcome.significant
                    )
            self._summarize(cycle, engine, all_changes, touched)
        finally:
            self._close_cycle(cycle, engine)
        return cycle

    def _scan_targets(self) -> list[Auction]:
        """Tracked auctions that are open (or closed but not yet finalised)."""
        now = now_utc()
        targets = []
        for auction in self.store.tracked_auctions():
            if auction.start_at and now < auction.start_at - timedelta(days=1):
                # Not open yet and not opening soon; the daily discovery run
                # keeps its metadata fresh.
                continue
            targets.append(auction)
        return targets

    def run_final_stretch(self, *, notify: bool = True) -> Cycle | None:
        """Scan only auctions inside the final window and due for a poll."""
        due = self.auctions_due_for_final_stretch()
        if not due:
            return None
        return self.run_change_scan(
            cycle_type=M.FINAL_STRETCH,
            auctions=due,
            notify=notify,
            # Photos rarely change in the last hours; keep these polls fast.
            download_images=False,
        )

    def auctions_due_for_final_stretch(self, at: datetime | None = None) -> list[Auction]:
        now = at or now_utc()
        due: list[Auction] = []
        for auction in self.store.tracked_auctions():
            if not auction.in_final_stretch(self.final_stretch_minutes, now):
                continue
            last = auction.last_scraped_at
            if last is None or now - last >= timedelta(minutes=self.poll_minutes):
                due.append(auction)
        return due

    # ==================================================================
    # Core: scrape one auction
    # ==================================================================
    def scrape_auction(
        self,
        auction: Auction,
        cycle: Cycle,
        engine: AIEngine,
        classifier: ITClassifier,
        *,
        download_images: bool = True,
        persist_lots: bool = True,
        finalizing: bool = False,
    ) -> ScrapeOutcome:
        """Fetch, parse, diff and persist one auction.

        With ``persist_lots=False`` nothing is written — used by discovery to
        inspect an auction before deciding whether it is worth tracking.
        """
        url = auction.url
        try:
            result = self.fetcher.fetch(url, validator=detail_validator)
            scraped = parse_detail_page(result.text, url, self.config)
        except FetchError as exc:
            return ScrapeOutcome(auction, error=f"{url}: fetch failed: {exc.message}")
        except Exception as exc:
            log.exception("detail parse failed", extra={"url": url})
            return ScrapeOutcome(auction, error=f"{url}: parse failed: {exc}")

        # Carry over what the index card knew and the database already held.
        scraped.id = auction.id
        scraped.is_it = auction.is_it or scraped.is_it
        scraped.it_reason = auction.it_reason
        scraped.it_confidence = auction.it_confidence
        scraped.it_source = auction.it_source
        scraped.extra = {**auction.extra, **scraped.extra}
        if not scraped.thumbnail_url:
            scraped.thumbnail_url = auction.thumbnail_url

        # Authoritative bid data from the site's own JSON feed.
        feed = fetch_bid_feed(self.fetcher, self.config, scraped.site_id)
        if feed:
            apply_bid_feed(scraped, feed)
        else:
            log.debug("no bid feed available", extra={"url": url, "site_id": scraped.site_id})

        scraped.status = scraped.computed_status()
        scraped.last_scraped_at = now_utc()
        cycle.lots_seen += len(scraped.lots)

        if not persist_lots:
            return ScrapeOutcome(scraped, is_new=auction.id is None)

        # --- persist the auction row first so lots have a parent id
        stored, is_new, auction_changes = self.store.upsert_auction(scraped)
        previous_lots = [] if is_new else self.store.get_lots(stored.id)

        # Original photos come from the per-lot gallery feed; only fetch for
        # lots whose photo set we do not know yet.
        if self.config.get("images.download", True):
            unknown = {l.lot_number for l in previous_lots if not l.image_urls}
            needs = [
                lot
                for lot in scraped.lots
                if lot.lot_number in unknown or lot.lot_number not in
                {p.lot_number for p in previous_lots}
            ]
            if needs:
                enrich_lot_images(self.fetcher, self.config, needs)
        # Reuse known photo URLs for lots we did not re-query.
        known_images = {l.lot_number: l for l in previous_lots}
        for lot in scraped.lots:
            prior = known_images.get(lot.lot_number)
            if prior and not lot.image_urls:
                lot.image_urls = prior.image_urls
                lot.thumbnail_urls = lot.thumbnail_urls or prior.thumbnail_urls

        diff = diff_lots(
            previous_lots,
            scraped.lots,
            config=self.config,
            auction=stored,
            cycle_id=cycle.id,
            rules=self.rules,
        )
        self._persist_lots(stored, scraped, previous_lots, diff, cycle)

        # AI enrichment for lots we have not described yet.
        self._extract_specs(diff.new_lots, engine, cycle)

        images = 0
        if download_images:
            images = self._download_images(stored, scraped, diff, cycle)

        recorded = self.store.record_changes(auction_changes + diff.changes, cycle.id)
        cycle.changes_recorded += recorded
        cycle.lots_changed += len(diff.changed_lots) + len(diff.new_lots)
        self.store.mark_scraped(stored.id, scraped.last_scraped_at)

        if diff.changes:
            log.info(
                "auction changes recorded",
                extra={
                    "url": url,
                    "lots": len(scraped.lots),
                    "counts": summarize_changes(diff.changes),
                    "significant": len(diff.significant),
                },
            )

        stored.lots = scraped.lots
        return ScrapeOutcome(
            stored, diff=diff, is_new=is_new, images=images,
            auction_changes=auction_changes,
        )

    def _persist_lots(
        self,
        stored: Auction,
        scraped: Auction,
        previous_lots: Sequence[Lot],
        diff: DiffResult,
        cycle: Cycle,
    ) -> None:
        by_number = {lot.lot_number: lot for lot in previous_lots}
        changed_numbers = {lot.lot_number for lot in diff.changed_lots}
        new_numbers = {lot.lot_number for lot in diff.new_lots}

        for lot in scraped.lots:
            prior = by_number.get(lot.lot_number)
            if prior is None:
                self.store.insert_lot(stored.id, lot)
            else:
                lot.id = prior.id
                lot.auction_id = stored.id
                lot.first_seen_at = prior.first_seen_at
                lot.last_seen_at = now_utc()
                lot.final_bid = prior.final_bid
                # Preserve AI-derived attributes; they are not re-derived every scan.
                lot.category = lot.category or prior.category
                lot.brand = lot.brand or prior.brand
                lot.model = lot.model or prior.model
                lot.specs = lot.specs or prior.specs
                lot.is_it = prior.is_it if lot.is_it is None else lot.is_it
                lot.extra = {**prior.extra, **lot.extra}
                self.store.update_lot(lot)
            # Time series: record first sighting and every subsequent move.
            if lot.lot_number in new_numbers or lot.lot_number in changed_numbers:
                self.store.record_snapshot(lot, stored, cycle.id)

        for lot in diff.removed_lots:
            if lot.id:
                self.store.mark_lot_removed(lot.id)

    def _extract_specs(
        self, lots: Sequence[Lot], engine: AIEngine, cycle: Cycle
    ) -> None:
        """Ask the AI for brand/model/specs on newly seen lots."""
        if not lots or not engine.available_for("extract_specs"):
            return
        limit = int(self.config.get("ai.max_spec_extractions_per_cycle", 150))
        batch = [lot for lot in lots if lot.id and lot.description][:limit]
        if not batch:
            return
        extracted = engine.extract_specs(batch)
        for index, data in extracted.items():
            if index >= len(batch):
                continue
            lot = batch[index]
            self.store.set_lot_specs(
                lot.id,
                category=data.get("category", ""),
                brand=data.get("brand", ""),
                model=data.get("model", ""),
                specs=data.get("specs", {}),
                is_it=data.get("is_it"),
            )
            lot.category = data.get("category", "")
            lot.brand = data.get("brand", "")
            lot.model = data.get("model", "")
            lot.specs = data.get("specs", {})
        log.info(
            "extracted lot specifications",
            extra={"lots": len(batch), "resolved": len(extracted)},
        )

    def _download_images(
        self, stored: Auction, scraped: Auction, diff: DiffResult, cycle: Cycle
    ) -> int:
        downloader = ImageDownloader(self.config, self.store, self.fetcher)
        if not downloader.enabled:
            return 0
        count = downloader.download_auction_images(stored)
        # New lots always; existing lots only when their photos moved.
        image_changed = {
            c.lot_number for c in diff.changes if c.change_type == M.IMAGE_CHANGED
        }
        targets = [
            lot
            for lot in scraped.lots
            if lot in diff.new_lots or lot.lot_number in image_changed
        ]
        # Also backfill anything whose files never landed.
        if not targets:
            targets = []
        count += downloader.download_for_lots(stored, targets)
        cycle.images_downloaded += count
        if count:
            log.info("images downloaded", extra={"count": count, **downloader.summary()})
        return count

    # ==================================================================
    # REMINDERS — 3 hours and 30 minutes before close
    # ==================================================================
    def run_reminders(self, *, at: datetime | None = None) -> Cycle | None:
        now = at or now_utc()
        leads = self.config.reminder_lead_minutes
        if not leads:
            return None

        pending: list[tuple[Auction, int]] = []
        for auction in self.store.tracked_auctions():
            if not auction.end_at or now >= auction.end_at:
                continue
            for lead in leads:
                trigger = auction.end_at - timedelta(minutes=lead)
                if now < trigger:
                    continue
                # ``scheduled_for`` is the close time the reminder was computed
                # against, so an extension produces a fresh, un-sent key rather
                # than double-sending against the old one.
                if self.store.reminder_already_sent(auction.id, lead, auction.end_at):
                    continue
                pending.append((auction, lead))

        if not pending:
            return None

        cycle, engine, classifier = self._new_cycle(M.REMINDER)
        try:
            for auction, lead in pending:
                try:
                    self._send_reminder(auction, lead, cycle, engine, classifier)
                except Exception as exc:
                    cycle.errors.append(f"reminder {auction.url} ({lead}m): {exc}")
                    log.exception(
                        "reminder failed",
                        extra={"url": auction.url, "lead_minutes": lead},
                    )
        finally:
            self._close_cycle(cycle, engine)
        return cycle

    def _send_reminder(
        self,
        auction: Auction,
        lead: int,
        cycle: Cycle,
        engine: AIEngine,
        classifier: ITClassifier,
    ) -> None:
        from .models import ReminderRecord

        # Refresh state so the email reflects the bids as of right now.
        outcome = self.scrape_auction(
            auction, cycle, engine, classifier, download_images=False
        )
        current = outcome.auction if outcome.ok else auction
        if not current.lots:
            current.lots = self.store.get_lots(current.id, include_removed=False)
        if outcome.ok:
            cycle.auctions_scraped += 1
            self.store.record_changes(outcome.all_changes, cycle.id)
            self._handle_extension(current, outcome.auction_changes)
            # An extension may have pushed the close time past this lead again.
            if current.end_at and now_utc() < current.end_at - timedelta(minutes=lead):
                log.info(
                    "reminder skipped: auction was extended beyond the lead time",
                    extra={"url": current.url, "lead_minutes": lead},
                )
                return

        hot = self._hot_lots(current)
        sent, message = self.notifier.reminder(current, lead, hot)
        self.store.log_reminder(
            ReminderRecord(
                auction_id=current.id,
                lead_minutes=lead,
                scheduled_for=current.end_at or now_utc(),
                sent_at=now_utc(),
                delivery_status="SENT" if sent else "FAILED",
                recipients=self.notifier.recipients_for("reminder"),
                message_preview=message.text,
            )
        )
        log.info(
            "reminder processed",
            extra={
                "url": current.url,
                "lead_minutes": lead,
                "sent": sent,
                "bids": current.total_bids,
            },
        )

    def _hot_lots(
        self, auction: Auction, count: int = 3
    ) -> list[tuple[Lot, float, int]]:
        """Lots with the biggest bid movement in the last hour."""
        if not auction.id:
            return []
        velocity = self.store.bid_velocity(auction.id, 60)
        by_id = {lot.id: lot for lot in auction.lots if lot.id}
        out: list[tuple[Lot, float, int]] = []
        for lot_id, bid_delta, count_delta in velocity:
            lot = by_id.get(lot_id) or self.store.get_lot(lot_id)
            if lot is None:
                continue
            out.append((lot, bid_delta, count_delta))
            if len(out) >= count:
                break
        return out

    # ==================================================================
    # FINALIZE — 3 hours after close
    # ==================================================================
    def run_finalize(self, *, notify: bool = True) -> Cycle | None:
        due = self.store.auctions_due_for_finalize(self.finalize_delay)
        if not due:
            return None
        cycle, engine, classifier = self._new_cycle(M.FINALIZE)
        try:
            for auction in due:
                try:
                    self._finalize_one(auction, cycle, engine, classifier, notify=notify)
                except Exception as exc:
                    cycle.errors.append(f"finalize {auction.url}: {exc}")
                    log.exception("finalize failed", extra={"url": auction.url})
        finally:
            self._close_cycle(cycle, engine)
        return cycle

    def _finalize_one(
        self,
        auction: Auction,
        cycle: Cycle,
        engine: AIEngine,
        classifier: ITClassifier,
        *,
        notify: bool,
    ) -> None:
        from .report import write_final_report

        outcome = self.scrape_auction(
            auction, cycle, engine, classifier, download_images=True, finalizing=True
        )
        final = outcome.auction if outcome.ok else auction
        if not outcome.ok:
            cycle.errors.append(outcome.error)
        else:
            cycle.auctions_scraped += 1
            self.store.record_changes(outcome.all_changes, cycle.id)

        # Freeze the last observed bid as the final price for every lot.
        self.store.finalize_lots(final.id)
        lots = self.store.get_lots(final.id, include_removed=False)
        final.lots = lots
        sold = [lot for lot in lots if (lot.final_bid or 0) > 0]
        total = sum(lot.final_bid or 0 for lot in sold)

        self.store.mark_finalized(final.id)
        final.finalized_at = now_utc()
        report_path = ""
        try:
            report_path = write_final_report(self.config, self.store, final)
        except Exception as exc:
            cycle.errors.append(f"final report for {final.url}: {exc}")
            log.exception("could not write final report", extra={"url": final.url})

        log.info(
            "auction finalised",
            extra={
                "url": final.url,
                "lots": len(lots),
                "sold": len(sold),
                "total": money(total),
                "report": report_path,
            },
        )
        if notify:
            self.notifier.finalized(
                final, sold_lots=len(sold), total_value=total,
            )

    # ==================================================================
    # Shared helpers
    # ==================================================================
    def _handle_extension(self, auction: Auction, changes: Sequence[Change]) -> None:
        """Email on a genuine extension of the close time."""
        for change in changes:
            if change.change_type != M.CLOSE_TIME_CHANGED or change.note != "extended":
                continue
            from .util import from_iso

            self.notifier.extension(
                auction, from_iso(str(change.previous)), from_iso(str(change.current))
            )
            break

    def check_final_stretch_entries(self, *, notify: bool = True) -> int:
        """Email once per auction when it crosses into the final 3 hours."""
        count = 0
        now = now_utc()
        for auction in self.store.tracked_auctions():
            if not auction.in_final_stretch(self.final_stretch_minutes, now):
                continue
            marker = to_iso(auction.end_at)
            if auction.extra.get(FINAL_STRETCH_FLAG) == marker:
                continue
            auction.lots = self.store.get_lots(auction.id, include_removed=False)
            if notify:
                self.notifier.final_stretch_entry(auction)
            auction.extra[FINAL_STRETCH_FLAG] = marker
            self.store.upsert_auction(auction)
            count += 1
        return count

    def refresh_statuses(self) -> int:
        """Keep stored statuses in step with the clock (FORTHCOMING -> CLOSED)."""
        changed = 0
        for auction in self.store.tracked_auctions(include_finalized=False):
            expected = auction.computed_status()
            if expected != auction.status:
                self.store.set_status(auction.id, expected)
                changed += 1
        return changed

    def _summarize(
        self,
        cycle: Cycle,
        engine: AIEngine,
        changes: Sequence[Change],
        auctions: Sequence[Auction] = (),
    ) -> None:
        """Ask the AI for the analyst note attached to this cycle."""
        if not engine.available_for("summarize_scan"):
            return
        summary = engine.summarize_scan(
            cycle_type=cycle.cycle_type,
            auctions=list(auctions),
            changes=list(changes),
            lot_count=cycle.lots_seen,
        )
        if not summary:
            return
        parts = [summary.get("headline", ""), summary.get("summary", "")]
        watch = summary.get("watch_items") or []
        if watch:
            parts.append("Watch: " + "; ".join(watch))
        momentum = summary.get("momentum")
        if momentum:
            parts.append(f"Momentum: {momentum}")
        cycle.ai_summary = "\n".join(p for p in parts if p)

    # ==================================================================
    # Heartbeat — the once-a-minute evaluation of time-based triggers
    # ==================================================================
    def heartbeat(self, *, notify: bool = True) -> dict[str, object]:
        """Run whatever is due right now. Safe to call every minute."""
        report: dict[str, object] = {}
        self.refresh_statuses()
        report["final_stretch_entries"] = self.check_final_stretch_entries(notify=notify)

        stretch = self.run_final_stretch(notify=notify)
        if stretch:
            report["final_stretch_cycle"] = stretch.id
            report["final_stretch_auctions"] = stretch.auctions_scraped

        reminders = self.run_reminders()
        if reminders:
            report["reminder_cycle"] = reminders.id

        finalize = self.run_finalize(notify=notify)
        if finalize:
            report["finalize_cycle"] = finalize.id
            report["finalized"] = finalize.auctions_scraped
        return report
