"""Command-line interface: ``python -m auction_tracker <command>``.

Run ``python -m auction_tracker --help`` for the list. The commands most often
used by hand:

* ``serve``       — the container's default: web UI plus all scheduled jobs
* ``discover``    — scan the index now for new IT auctions
* ``scan``        — re-scrape tracked auctions and record changes
* ``heartbeat``   — run whatever is due now (final-stretch polls, reminders,
                    finalisation)
* ``status``      — configuration and health summary
* ``test-email``  — prove SMTP works before relying on the reminders
* ``ask``         — price estimate from the historical data
* ``import``      — import past/current auctions by URL with all details
* ``archive``     — build the downloadable zip
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import Any

from . import __version__, models as M
from .config import Config, load_config
from .db import SCHEMA_VERSION, open_database
from .logging_setup import get_logger, setup_logging
from .store import Store
from .util import money, now_utc, to_iso

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _bootstrap(args: argparse.Namespace) -> tuple[Config, Store]:
    config = load_config(getattr(args, "config", None))
    if getattr(args, "verbose", False):
        import os

        os.environ["AT__LOGGING__LEVEL"] = "DEBUG"
        config = load_config(getattr(args, "config", None), reload=True)
    config.ensure_dirs()
    setup_logging(
        "DEBUG" if getattr(args, "verbose", False) else str(config.get("logging.level", "INFO")),
        str(config.get("logging.format", "text")),
        config.path("logs_dir"),
        bool(config.get("logging.file_enabled", True)),
    )
    database = open_database(config.path("sqlite_path"))
    return config, Store(database)


def _pipeline(config: Config, store: Store, args: argparse.Namespace):
    from .notify import LogOnlyNotifier, build_notifier
    from .pipeline import Pipeline

    notifier = (
        LogOnlyNotifier(config, store)
        if getattr(args, "no_email", False)
        else build_notifier(config, store)
    )
    return Pipeline(config, store, notifier=notifier, dry_run=getattr(args, "dry_run", False))


def _print_cycle(cycle: Any) -> None:
    if cycle is None:
        print("Nothing was due.")
        return
    print(
        f"cycle {cycle.id[:8]} [{cycle.cycle_type}] "
        f"{round(cycle.duration_seconds or 0, 1)}s: "
        f"{cycle.auctions_scraped} auctions scraped, {cycle.auctions_new} new, "
        f"{cycle.lots_seen} lots, {cycle.changes_recorded} changes, "
        f"{cycle.images_downloaded} images, {cycle.ai_calls} AI calls, "
        f"{cycle.emails_sent} emails"
    )
    if cycle.notes:
        print(f"  note: {cycle.notes}")
    if cycle.ai_summary:
        print(f"  AI: {cycle.ai_summary.splitlines()[0]}")
    for error in cycle.errors:
        print(f"  ERROR: {error}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    store.db.migrate()
    applied = sorted(store.db.applied_versions())
    print(f"database: {config.path('sqlite_path')}")
    print(f"schema version: {SCHEMA_VERSION} (migrations applied: {applied})")
    print(f"full-text search: {'enabled' if store.db.has_fts() else 'unavailable'}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    with _pipeline(config, store, args) as pipeline:
        cycle = pipeline.run_discovery(notify=not args.no_notify)
    _print_cycle(cycle)
    return 1 if cycle.errors else 0


def cmd_scan(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    with _pipeline(config, store, args) as pipeline:
        targets = None
        if args.url:
            auction = store.get_auction_by_url(args.url)
            if auction is None:
                from .models import Auction

                auction = Auction(url=args.url, is_it=True)
            targets = [auction]
        cycle = pipeline.run_change_scan(
            auctions=targets,
            notify=not args.no_notify,
            download_images=not args.no_images,
        )
    _print_cycle(cycle)
    return 1 if cycle.errors else 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    with _pipeline(config, store, args) as pipeline:
        report = pipeline.heartbeat(notify=not args.no_notify)
    print(json.dumps(report, indent=2, default=str) if report else "Nothing was due.")
    return 0


def cmd_reminders(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    with _pipeline(config, store, args) as pipeline:
        cycle = pipeline.run_reminders()
    _print_cycle(cycle)
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    with _pipeline(config, store, args) as pipeline:
        cycle = pipeline.run_finalize(notify=not args.no_notify)
    _print_cycle(cycle)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    from .report import write_snapshot_report

    path = write_snapshot_report(config, store)
    print(f"wrote {path}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    from .archive import build_archive

    path = build_archive(config, store, args.output, include_images=args.images)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    from .ai import AIEngine

    stats = store.stats()
    email = config.email
    engine = AIEngine(config, store)

    print(f"ross-auction-tracker {__version__}")
    print(f"config:      {config.source or 'built-in defaults'}")
    print(f"database:    {config.path('sqlite_path')} (schema {SCHEMA_VERSION})")
    print(f"images:      {config.path('images_dir')}")
    print(f"timezone:    {config.get('site.timezone')}")
    print(f"fetcher:     {config.get('fetch.client')}")
    print()
    print("-- data --")
    print(f"  IT auctions tracked : {stats['auctions_tracked']} "
          f"({stats['auctions_live']} live, {stats['auctions_past']} past, "
          f"{stats['auctions_finalized']} finalised)")
    print(f"  auctions seen total : {stats['auctions_total']}")
    print(f"  lots                : {stats['lots']:,} ({stats['lots_sold']:,} with final price)")
    print(f"  total final bids    : {money(stats['sold_value'])}")
    print(f"  changes recorded    : {stats['changes']:,} ({stats['changes_24h']:,} in 24h)")
    print(f"  images stored       : {stats['images']:,}")
    print(f"  last discovery      : {stats['last_discovery'] or 'never'}")
    print(f"  last change scan    : {stats['last_change_scan'] or 'never'}")
    print()
    print("-- email --")
    print(f"  configured : {'yes' if email.configured else 'NO (log-only mode)'}")
    print(f"  host       : {email.host or '-'}:{email.port} ({email.security})")
    print(f"  from       : {email.sender or '-'}")
    print(f"  recipients : {', '.join(email.recipients) or '-'}")
    if email.reminder_recipients:
        print(f"  reminders+ : {', '.join(email.reminder_recipients)}")
    print(f"  dry run    : {email.dry_run}")
    print(f"  reminders  : {', '.join(str(m) + 'min' for m in config.reminder_lead_minutes)}"
          " before close")
    print(f"  sent in 24h: {stats['emails_24h']}")
    print()
    print("-- AI providers --")
    for provider in engine.describe():
        state = (
            "available"
            if provider["available"]
            else ("disabled" if not provider["enabled"] else f"no key ({provider['key_env']})")
        )
        print(f"  {provider['name']:<20} {provider['kind']:<10} {provider['model']:<34} {state}")
    if not engine.any_available:
        print("  (none available — keyword filtering only)")
    print(f"  calls in 24h: {stats['ai']['total']} "
          f"({stats['ai']['cached']} cached, {stats['ai']['failed']} failed)")
    print()
    print("-- schedule --")
    schedule = config.section("schedule")
    print(f"  discovery      : every {schedule.get('discovery_every_hours')}h at "
          f"{schedule.get('discovery_at')}")
    print(f"  change scan    : every {schedule.get('change_every_hours')}h")
    print(f"  final stretch  : every {schedule.get('final_stretch_poll_minutes')}min "
          f"inside the last {int(schedule.get('final_stretch_minutes', 180)) // 60}h")
    print(f"  finalise       : {int(schedule.get('finalize_delay_minutes', 180)) // 60}h "
          "after close")
    return 0


def cmd_test_email(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    from .notify import build_notifier

    notifier = build_notifier(config, store)
    recipients = notifier.recipients_for("test")
    print(f"channel: {notifier.name}; recipients: {', '.join(recipients) or 'none'}")
    if notifier.test():
        print("sent — check the inbox (and the spam folder)")
        return 0
    print("failed — see the log output above", file=sys.stderr)
    return 1


def cmd_ask(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    from .ai import AIEngine, build_comparable_terms, context_for_question

    question = " ".join(args.question).strip()
    if not question:
        print("nothing to ask", file=sys.stderr)
        return 2
    engine = AIEngine(config, store)
    terms = build_comparable_terms(question)
    comparables = store.comparable_lots(terms=terms, limit=60, sold_only=True)
    if not comparables:
        comparables = store.comparable_lots(terms=terms, limit=60, sold_only=False)
    print(f"search terms: {', '.join(terms)}")
    print(f"comparable lots found: {len(comparables)}")
    for row in comparables[:10]:
        print(
            f"  lot {row['lot_number']:<5} {money(row.get('final_bid') or row.get('current_bid')):>10}"
            f"  {row['bid_count']:>3} bids  {str(row.get('end_at'))[:10]}  "
            f"{str(row['description'])[:70]}"
        )
    if not engine.any_available:
        print("\nNo AI provider available; add a key to .env to get an estimate.",
              file=sys.stderr)
        return 1

    if args.general:
        answer = engine.ask(question, context_for_question(store, question))
        if answer is None:
            print("all AI providers failed", file=sys.stderr)
            return 1
        print(f"\n{answer['answer']}\n(confidence: {answer['confidence']}, "
              f"{answer['provider']}/{answer['model']})")
        return 0

    estimate = engine.estimate_price(
        question=question, target=question, comparables=comparables
    )
    if estimate is None:
        print("all AI providers failed", file=sys.stderr)
        return 1
    print(f"\nSuggested maximum bid: {money(estimate['max_bid_aud'])}")
    if estimate.get("fair_range_aud"):
        low, high = estimate["fair_range_aud"]
        print(f"Fair range: {money(low)} – {money(high)}")
    print(f"Confidence: {estimate['confidence']} ({estimate['provider']}/{estimate['model']})")
    print(f"\n{estimate['reasoning']}")
    for caveat in estimate.get("caveats", []):
        print(f"  - caveat: {caveat}")
    print("\nNote: the buyer's premium and GST are charged on top of the hammer price.")
    store.save_estimate(
        question=question,
        answer=estimate.get("reasoning", ""),
        comparables=comparables[:20],
        max_price=estimate.get("max_bid_aud"),
        provider=estimate.get("provider", ""),
        model=estimate.get("model", ""),
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the web UI and the scheduler together (the container default)."""
    config, store = _bootstrap(args)
    from .scheduler import TrackerScheduler
    from .web.app import create_app

    scheduler = None
    if not args.no_scheduler:
        scheduler = TrackerScheduler(config, store)
        scheduler.start(run_discovery_now=args.discover_now)

    app = create_app(config, store)
    host = args.host or str(config.get("web.host", "0.0.0.0"))
    # Precedence: --port flag > WEB_PORT env (set by docker-compose) > config.
    port = args.port or int(os.environ.get("WEB_PORT") or config.get("web.port", 8080))

    stopping = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        log.info("shutting down", extra={"signal": signum})
        stopping.set()
        if scheduler is not None:
            scheduler.shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except ValueError:
            pass  # not on the main thread (tests)

    log.info(
        "starting web UI",
        extra={"host": host, "port": port, "scheduler": scheduler is not None},
    )
    try:
        from werkzeug.serving import make_server

        server = make_server(host, port, app, threaded=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler is not None:
            scheduler.shutdown()
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    config, store = _bootstrap(args)
    from .archive import export_json

    payload = export_json(store)
    text = json.dumps(payload, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"wrote {args.output} ({len(text):,} bytes)")
    else:
        print(text)
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    """Explain how a single auction URL would be classified."""
    config, store = _bootstrap(args)
    from .ai import AIEngine
    from .fetch import build_fetcher
    from .models import Auction
    from .scrape import ITClassifier, detail_validator, parse_detail_page

    engine = AIEngine(config, store)
    classifier = ITClassifier(config, engine)
    fetcher = build_fetcher(config)
    try:
        result = fetcher.fetch(args.url, validator=detail_validator)
        auction = parse_detail_page(result.text, args.url, config)
    finally:
        fetcher.close()

    pre = classifier.pre_filter(Auction(url=args.url, title=auction.title))
    decision = classifier.confirm(auction)
    print(f"title        : {auction.title}")
    print(f"lots         : {auction.lot_count}")
    print(f"pre-filter   : is_it={pre.is_it} ({pre.reason})")
    print(f"final verdict: is_it={decision.is_it} confidence={decision.confidence:.2f} "
          f"source={decision.source}")
    print(f"reason       : {decision.reason}")
    if decision.categories:
        print(f"categories   : {', '.join(decision.categories)}")
    it_lots = classifier.it_lots(auction)
    print(f"IT-looking lots: {len(it_lots)} of {auction.lot_count}")
    for lot in it_lots[:10]:
        print(f"  lot {lot.lot_number}: {lot.description[:80]}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Import one or more past/current auctions by URL with all lots, photos and specs."""
    config, store = _bootstrap(args)
    with _pipeline(config, store, args) as pipeline:
        succeeded = 0
        failed = 0
        total_lots = 0
        for url in args.urls:
            url = url.strip()
            if not url:
                continue
            print(f"importing {url} …")
            try:
                outcome = pipeline.import_auction_url(
                    url,
                    download_images=not args.no_images,
                    force_it=not args.no_force_it,
                )
                if outcome.ok:
                    succeeded += 1
                    stored = store.get_auction_by_url(url)
                    lots = len(store.get_lots(stored.id)) if stored else 0
                    total_lots += lots
                    print(f"  ✓ {outcome.auction.title} — {lots} lots")
                else:
                    failed += 1
                    print(f"  ✗ {outcome.error}", file=sys.stderr)
            except Exception as exc:
                failed += 1
                print(f"  ✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"\nDone: {succeeded} imported ({total_lots} lots), {failed} failed.")
    return 1 if failed and not succeeded else 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    """Re-evaluate stored auctions with the IT classifier and update is_it status."""
    config, store = _bootstrap(args)
    from .ai import AIEngine
    from .scrape import ITClassifier

    engine = AIEngine(config, store)
    classifier = ITClassifier(config, engine)

    auctions = (
        store.all_auctions()
        if getattr(args, "all", True)
        else store.tracked_auctions(include_finalized=True)
    )
    url_target = getattr(args, "url", None)
    if url_target:
        auctions = [a for a in auctions if a.url == url_target]

    print(f"Reclassifying {len(auctions)} auction(s) with ITClassifier...")
    changed = 0
    for auction in auctions:
        auction.lots = store.get_lots(auction.id, include_removed=True)
        decision = classifier.confirm(auction)
        old_is_it = bool(auction.is_it)
        new_is_it = bool(decision.is_it)
        store.reclassify_auction(
            auction.id,
            is_it=new_is_it,
            confidence=decision.confidence,
            reason=decision.reason,
            source=decision.source,
            categories=decision.categories,
        )
        if old_is_it != new_is_it:
            changed += 1
            print(
                f"[{auction.id:2d}] CHANGED is_it {int(old_is_it)} -> {int(new_is_it)} "
                f"({decision.source}, conf={decision.confidence:.2f}): {decision.reason} | {auction.title[:55]}"
            )
        else:
            print(
                f"[{auction.id:2d}] SAME is_it={int(new_is_it)} "
                f"({decision.source}, conf={decision.confidence:.2f}) | {auction.title[:55]}"
            )

    print(f"\nDone. {changed} of {len(auctions)} auctions changed status.")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auction_tracker",
        description=(
            "Monitor IT-equipment auctions at auctions.com.au: discover, track "
            "changes, email reminders and serve the report UI."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(func=handler)
        return sub

    add("init-db", cmd_init_db, "Create or migrate the database schema.")

    discover = add(
        "discover", cmd_discover,
        "Scan the whole auction index for new IT auctions (the 24-hour job).",
    )
    discover.add_argument("--no-notify", action="store_true", help="do not send email")
    discover.add_argument("--no-email", action="store_true", help="force log-only notifier")

    scan = add(
        "scan", cmd_scan,
        "Re-scrape tracked auctions and record what changed (the 6-hour job).",
    )
    scan.add_argument("--url", help="scan only this auction URL")
    scan.add_argument("--no-notify", action="store_true", help="do not send email")
    scan.add_argument("--no-email", action="store_true", help="force log-only notifier")
    scan.add_argument("--no-images", action="store_true", help="skip image downloads")

    heartbeat = add(
        "heartbeat", cmd_heartbeat,
        "Run whatever is due now: final-stretch polls, reminders, finalisation.",
    )
    heartbeat.add_argument("--no-notify", action="store_true")
    heartbeat.add_argument("--no-email", action="store_true")

    reminders = add("reminders", cmd_reminders, "Send any due pre-close reminder emails.")
    reminders.add_argument("--no-email", action="store_true")

    finalize = add(
        "finalize", cmd_finalize,
        "Record final results for auctions that closed 3+ hours ago.",
    )
    finalize.add_argument("--no-notify", action="store_true")
    finalize.add_argument("--no-email", action="store_true")

    add("report", cmd_report, "Export the snapshot HTML report.")

    archive = add("archive", cmd_archive, "Build the downloadable zip archive.")
    archive.add_argument("-o", "--output", help="output path for the zip")
    archive.add_argument(
        "--images", action="store_true", help="include every stored photo (large)"
    )

    dump = add("dump", cmd_dump, "Dump all captured data as JSON.")
    dump.add_argument("-o", "--output", help="write to this file instead of stdout")

    add("status", cmd_status, "Show configuration, data and health summary.")
    add("test-email", cmd_test_email, "Send a test email to the configured recipients.")

    ask = add("ask", cmd_ask, "Ask the AI what to pay, based on past results.")
    ask.add_argument("question", nargs="+", help="the question, in plain English")
    ask.add_argument(
        "--general", action="store_true",
        help="answer as a general data question rather than a price estimate",
    )

    classify = add(
        "classify", cmd_classify, "Explain how one auction URL is classified."
    )
    classify.add_argument("url", help="auction detail page URL")

    reclassify = add(
        "reclassify", cmd_reclassify,
        "Re-evaluate stored auctions with the IT classifier and update their is_it status.",
    )
    reclassify.add_argument(
        "--all", action="store_true", default=True,
        help="reclassify all auctions in the database (default: True)",
    )
    reclassify.add_argument(
        "--tracked-only", action="store_false", dest="all",
        help="reclassify only currently tracked auctions",
    )
    reclassify.add_argument(
        "--url", help="reclassify a specific auction by URL",
    )

    import_cmd = add(
        "import", cmd_import,
        "Import one or more auctions by URL with all lots, photos and specs.",
    )
    import_cmd.add_argument("urls", nargs="+", help="auction detail page URL(s)")
    import_cmd.add_argument("--no-images", action="store_true", help="skip photo downloads")
    import_cmd.add_argument(
        "--no-force-it", action="store_true",
        help="do not force-mark auctions as IT-relevant",
    )
    import_cmd.add_argument("--no-email", action="store_true", help="force log-only notifier")

    serve = add("serve", cmd_serve, "Run the web UI and the scheduler (container default).")
    serve.add_argument("--host", help="bind address (default web.host)")
    serve.add_argument("--port", type=int, help="port (default web.port)")
    serve.add_argument(
        "--no-scheduler", action="store_true", help="web UI only, no scheduled jobs"
    )
    serve.add_argument(
        "--discover-now", action="store_true", help="run a discovery scan at start-up"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        log.exception("command failed")
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
