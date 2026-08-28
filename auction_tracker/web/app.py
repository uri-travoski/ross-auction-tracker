"""Flask application serving the report UI.

Routes
------
``/``                dashboard: live auctions, closing-soon block, recent changes
``/auctions``        paginated auction list; ``scope=current|past|all``, search,
                     status/location filters, closing-date range, sorting
``/auction/<id>``    one auction: metadata, change log, full catalogue with the
                     latest scan's changes highlighted
``/lot/<id>``        one lot: specs, stored photos, bid time series
``/image/<id>``      serves a stored image file from disk
``/lots``            cross-auction lot/price search with statistics
``/compare``         side-by-side comparison of two or more auctions
``/changes``         paginated change history
``/ask``             AI price estimate / question answering over the data
``/status``          schedule, SMTP config, AI providers, recent cycles
``/healthz``         plain-text health check for Docker

Pagination defaults to ``web.page_size`` (100) and can be changed per request.
Optional HTTP basic auth is enabled by setting ``WEB_USERNAME`` and
``WEB_PASSWORD`` in ``.env``.
"""

from __future__ import annotations

import os
import secrets
import statistics
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    make_response,
    request,
    send_file,
    send_from_directory,
)

from .. import models as M
from ..ai import AIEngine, build_comparable_terms, context_for_question
from ..config import Config, load_config
from ..db import SCHEMA_VERSION, open_database
from ..logging_setup import get_logger, setup_logging
from ..store import Store
from ..templating import STATIC_DIR, TEMPLATE_DIR, build_environment
from ..util import from_iso, now_utc, truncate

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Request-argument helpers
# ---------------------------------------------------------------------------


def _int_arg(name: str, default: int, *, low: int = 1, high: int = 100000) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _float_arg(name: str) -> float | None:
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _date_arg(name: str, *, end_of_day: bool = False) -> datetime | None:
    """Parse a ``<input type=date>`` value into an aware datetime."""
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    parsed = from_iso(raw)
    if parsed is None:
        return None
    if len(raw) <= 10:
        moment = time(23, 59, 59) if end_of_day else time(0, 0)
        parsed = datetime.combine(parsed.date(), moment, tzinfo=timezone.utc)
    return parsed


def _human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:,.0f} {unit}" if unit == "B" else f"{count:,.1f} {unit}"
        count /= 1024
    return f"{count:.1f} TB"


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _stats_for(prices: list[float]) -> dict[str, float]:
    if not prices:
        return {"count": 0, "total": 0.0, "average": 0.0, "median": 0.0, "max": 0.0}
    return {
        "count": len(prices),
        "total": sum(prices),
        "average": statistics.fmean(prices),
        "median": statistics.median(prices),
        "max": max(prices),
    }


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config: Config | None = None, store: Store | None = None) -> Flask:
    config = config or load_config()
    config.ensure_dirs()
    setup_logging(
        str(config.get("logging.level", "INFO")),
        str(config.get("logging.format", "text")),
        config.path("logs_dir"),
        bool(config.get("logging.file_enabled", True)),
    )

    app = Flask(
        __name__,
        template_folder=str(TEMPLATE_DIR),
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    app.jinja_env = build_environment(config)
    app.jinja_env.globals["url_for"] = _url_for_shim
    app.config["AT_CONFIG"] = config

    if store is None:
        database = open_database(config.path("sqlite_path"))
        store = Store(database)
    app.config["AT_STORE"] = store

    _install_auth(app)
    _register_routes(app, config, store)
    return app


def _url_for_shim(endpoint: str, **values: Any) -> str:
    """Minimal ``url_for`` so templates work outside a request context too."""
    if endpoint == "static":
        return f"/static/{values.get('filename', '')}"
    from flask import url_for as flask_url_for

    return flask_url_for(endpoint, **values)


def _install_auth(app: Flask) -> None:
    username = os.environ.get("WEB_USERNAME", "").strip()
    password = os.environ.get("WEB_PASSWORD", "")
    if not username or not password:
        return

    @app.before_request
    def require_basic_auth() -> Response | None:
        if request.path in {"/healthz"}:
            return None
        auth = request.authorization
        if (
            auth
            and secrets.compare_digest(auth.username or "", username)
            and secrets.compare_digest(auth.password or "", password)
        ):
            return None
        response = make_response("Authentication required", 401)
        response.headers["WWW-Authenticate"] = 'Basic realm="Auction Tracker"'
        return response

    log.info("HTTP basic auth enabled for the web UI")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: Flask, config: Config, store: Store) -> None:
    render = app.jinja_env.get_template

    def page_size() -> int:
        options = [int(o) for o in config.get("web.page_size_options", [25, 50, 100])]
        default = int(config.get("web.page_size", 100))
        requested = _int_arg("page_size", default, low=1, high=2000)
        return requested if requested in options else default

    def auction_row(auction: M.Auction, *, with_lots: bool = True) -> dict[str, Any]:
        lots = store.get_lots(auction.id, include_removed=False) if with_lots else []
        auction.lots = lots
        return {
            "auction": auction,
            "lots": lots,
            "lot_count": len(lots) or auction.lot_count,
            "total_bids": sum(lot.bid_count for lot in lots),
            "total_value": sum(
                (lot.final_bid if lot.final_bid else (lot.current_bid or 0))
                for lot in lots
            ),
            "reminders": [
                r
                for r in store.reminders_for_auction(auction.id)
                if r.get("delivery_status") == "SENT"
            ],
        }

    # ----------------------------------------------------------- dashboard
    @app.route("/")
    def dashboard() -> str:
        stats = store.stats()
        tracked = store.tracked_auctions(include_finalized=False)
        now = now_utc()
        current, closing = [], []
        stretch_minutes = int(config.get("schedule.final_stretch_minutes", 180))
        for auction in tracked:
            if auction.end_at and auction.end_at < now:
                continue
            row = auction_row(auction)
            current.append(row)
            if auction.in_final_stretch(stretch_minutes, now):
                closing.append(row)
        current.sort(key=lambda r: (r["auction"].end_at is None, r["auction"].end_at))
        closing.sort(key=lambda r: (r["auction"].end_at is None, r["auction"].end_at))

        cycles = store.recent_cycles(10)
        latest_summary = next((c for c in cycles if c.get("ai_summary")), None)
        return render("dashboard.html").render(
            active_page="dashboard",
            stats=stats,
            current=current[:25],
            closing_soon=closing,
            recent_changes=store.recent_changes(hours=48, limit=25),
            latest_summary=latest_summary,
        )

    # ------------------------------------------------------------ auctions
    @app.route("/auctions")
    def auctions() -> str:
        scope = request.args.get("scope", "all")
        size = page_size()
        page = _int_arg("page", 1)
        sort = request.args.get("sort", "end_asc" if scope == "current" else "end_desc")
        rows, total = store.search_auctions(
            scope=scope,
            query=request.args.get("q", "").strip(),
            location=request.args.get("location", "").strip(),
            status=request.args.get("status", "").strip(),
            date_from=_date_arg("date_from"),
            date_to=_date_arg("date_to", end_of_day=True),
            only_it=request.args.get("only_it", "1") != "0",
            sort=sort,
            page=page,
            page_size=size,
        )
        heading = {
            "current": "Current auctions",
            "past": "Past auctions",
        }.get(scope, "All tracked auctions")
        return render("auctions.html").render(
            active_page={"current": "current", "past": "past"}.get(scope, "current"),
            heading=heading,
            rows=[auction_row(a) for a in rows],
            total=total,
            page=page,
            page_size=size,
            args=request.args.to_dict(),
            status_options=[M.FORTHCOMING, M.IN_PROGRESS, M.CLOSED, M.FINALIZED],
            location_options=store.distinct_values("location", 100),
        )

    # ------------------------------------------------------ auction detail
    @app.route("/auction/<int:auction_id>")
    def auction_detail(auction_id: int) -> str:
        auction = store.get_auction(auction_id)
        if auction is None:
            abort(404)
        all_lots = store.get_lots(auction_id, include_removed=True)
        auction.lots = [lot for lot in all_lots if not lot.removed_at]

        # Changes from the most recent scan drive the highlighting.
        cycles = store.recent_cycles(6)
        last_cycle = next(
            (
                c
                for c in cycles
                if c.get("cycle_type") in {M.CHANGE, M.FINAL_STRETCH, M.FINALIZE, M.REMINDER}
            ),
            None,
        )
        recent = store.changes_for_auction(auction_id, limit=3000)
        cycle_id = last_cycle.get("id") if last_cycle else None
        by_lot: dict[str, list] = {}
        for change in recent:
            if cycle_id and change.cycle_id == cycle_id:
                by_lot.setdefault(change.lot_number, []).append(change)

        query = request.args.get("q", "").strip().lower()
        only = request.args.get("only", "")
        rows = []
        for lot in all_lots:
            if query and query not in (
                f"{lot.lot_number} {lot.description} {lot.brand} {lot.model}".lower()
            ):
                continue
            changes = by_lot.get(lot.lot_number, [])
            if only == "changed" and not changes:
                continue
            if only == "bids" and not lot.bid_count:
                continue
            if only == "nobids" and lot.bid_count:
                continue
            bid_change = next(
                (c for c in changes if c.change_type == M.BID_CHANGED), None
            )
            count_change = next(
                (c for c in changes if c.change_type == M.BID_COUNT_CHANGED), None
            )
            previous_bid = None
            delta = None
            if bid_change is not None:
                try:
                    previous_bid = float(bid_change.previous or 0)
                    delta = (lot.current_bid or 0) - previous_bid
                except (TypeError, ValueError):
                    delta = None
            count_delta = None
            if count_change is not None:
                try:
                    count_delta = lot.bid_count - int(float(count_change.previous or 0))
                except (TypeError, ValueError):
                    count_delta = None
            highlight = ""
            if changes:
                from ..templating import CHANGE_STYLES

                highlight = CHANGE_STYLES.get(changes[0].change_type, ("other", ""))[0]
            rows.append(
                {
                    "lot": lot,
                    "changes": changes,
                    "highlight": highlight,
                    "bid_delta": delta,
                    "previous_bid": previous_bid,
                    "count_delta": count_delta,
                }
            )

        sort = request.args.get("lot_sort", "lot")
        if sort == "bid_desc":
            rows.sort(key=lambda r: (r["lot"].final_bid or r["lot"].current_bid or 0), reverse=True)
        elif sort == "bids_desc":
            rows.sort(key=lambda r: r["lot"].bid_count, reverse=True)
        elif sort == "changed":
            rows.sort(key=lambda r: len(r["changes"]), reverse=True)
        else:
            rows.sort(key=lambda r: _lot_sort_key(r["lot"].lot_number))

        return render("auction_detail.html").render(
            active_page="current",
            auction=auction,
            lots=auction.lots,
            lot_rows=rows,
            removed_count=sum(1 for lot in all_lots if lot.removed_at),
            total_bids=sum(lot.bid_count for lot in all_lots),
            total_value=sum(
                (lot.final_bid if lot.final_bid else (lot.current_bid or 0))
                for lot in all_lots
            ),
            no_bid_count=sum(1 for lot in auction.lots if not lot.bid_count),
            is_final=bool(auction.finalized_at),
            changes=recent[:400],
            since=last_cycle.get("started_at") if last_cycle else None,
            reminders=store.reminders_for_auction(auction_id),
            args=request.args.to_dict(),
        )

    # ---------------------------------------------------------- lot detail
    @app.route("/lot/<int:lot_id>")
    def lot_detail(lot_id: int) -> str:
        lot = store.get_lot(lot_id)
        if lot is None:
            abort(404)
        auction = store.get_auction(lot.auction_id)
        if auction is None:
            abort(404)
        snapshots = store.snapshots_for_lot(lot_id)
        return render("lot_detail.html").render(
            active_page="lots",
            lot=lot,
            auction=auction,
            images=store.images_for_lot(lot_id),
            snapshots=snapshots,
            spark_points=_spark(snapshots),
            changes=[
                c
                for c in store.changes_for_auction(auction.id, limit=3000)
                if c.lot_id == lot_id
            ][:80],
        )

    # -------------------------------------------------------------- images
    @app.route("/image/<int:image_id>")
    def image(image_id: int) -> Response:
        row = store.db.query_one("SELECT * FROM images WHERE id = ?", (image_id,))
        if row is None or not row["local_path"]:
            abort(404)
        root = config.path("images_dir")
        path = (root / row["local_path"]).resolve()
        # Never serve anything outside the images directory.
        if not str(path).startswith(str(root.resolve())) or not path.is_file():
            abort(404)
        return send_file(path, mimetype=row["content_type"] or None, max_age=86400)

    # ---------------------------------------------------------------- lots
    @app.route("/lots")
    def lots() -> str:
        size = page_size()
        page = _int_arg("page", 1)
        only = request.args.get("only", "")
        rows, total = store.search_lots(
            query=request.args.get("q", "").strip(),
            brand=request.args.get("brand", "").strip(),
            category=request.args.get("category", "").strip(),
            scope=request.args.get("scope", "all"),
            min_bid=_float_arg("min_bid"),
            max_bid=_float_arg("max_bid"),
            date_from=_date_arg("date_from"),
            date_to=_date_arg("date_to", end_of_day=True),
            with_bids_only=only == "bids",
            sold_only=only == "sold",
            sort=request.args.get("sort", "close_desc"),
            page=page,
            page_size=size,
        )
        prices = [
            (lot.final_bid if lot.final_bid else (lot.current_bid or 0))
            for lot, _ in rows
            if (lot.final_bid or lot.current_bid)
        ]
        return render("lots.html").render(
            active_page="lots",
            rows=rows,
            total=total,
            page=page,
            page_size=size,
            args=request.args.to_dict(),
            brand_options=store.distinct_values("brand", 200),
            category_options=store.distinct_values("category", 100),
            summary=_stats_for(prices),
        )

    # ------------------------------------------------------------- compare
    @app.route("/compare")
    def compare() -> str:
        options, _ = store.search_auctions(
            scope="all", only_it=True, sort="end_desc", page=1, page_size=300
        )
        try:
            selected_ids = [int(v) for v in request.args.getlist("ids")]
        except ValueError:
            selected_ids = []
        restrict = request.args.get("q", "").strip().lower()

        columns = []
        for auction_id in selected_ids[:6]:
            auction = store.get_auction(auction_id)
            if auction is None:
                continue
            lots = store.get_lots(auction_id, include_removed=False)
            if restrict:
                lots = [
                    lot
                    for lot in lots
                    if restrict in f"{lot.description} {lot.brand} {lot.model}".lower()
                ]
            prices = [
                (lot.final_bid if lot.final_bid else (lot.current_bid or 0))
                for lot in lots
                if (lot.final_bid or lot.current_bid)
            ]
            summary = _stats_for(prices)
            columns.append(
                {
                    "auction": auction,
                    "lots": lots,
                    "lot_count": len(lots),
                    "with_bids": sum(1 for lot in lots if lot.bid_count),
                    "total_bids": sum(lot.bid_count for lot in lots),
                    "total": summary["total"],
                    "average": summary["average"],
                    "median": summary["median"],
                    "max": summary["max"],
                    "bids_per_lot": (
                        sum(lot.bid_count for lot in lots) / len(lots) if lots else 0
                    ),
                    "changes": len(store.changes_for_auction(auction_id, limit=5000)),
                }
            )

        # Average price per brand, per selected auction.
        brands: set[str] = set()
        for column in columns:
            brands |= {lot.brand for lot in column["lots"] if lot.brand}
        brand_rows = []
        for brand in sorted(brands):
            cells = []
            for column in columns:
                prices = [
                    (lot.final_bid if lot.final_bid else (lot.current_bid or 0))
                    for lot in column["lots"]
                    if lot.brand == brand and (lot.final_bid or lot.current_bid)
                ]
                cells.append(_stats_for(prices))
            brand_rows.append((brand, cells))

        return render("compare.html").render(
            active_page="compare",
            options=options,
            selected_ids=selected_ids,
            columns=columns,
            brand_rows=brand_rows,
            args=request.args.to_dict(),
        )

    # ------------------------------------------------------------- changes
    @app.route("/changes")
    def changes() -> str:
        size = page_size()
        page = _int_arg("page", 1)
        hours = _int_arg("hours", 24, low=1, high=8760 * 5)
        change_type = request.args.get("type", "").strip()
        significant = request.args.get("significant") == "1"

        where = ["observed_at >= ?"]
        params: list[Any] = [
            (now_utc() - timedelta(hours=hours)).isoformat()
        ]
        if change_type:
            where.append("change_type = ?")
            params.append(change_type)
        if significant:
            where.append("significant = 1")
        clause = " AND ".join(where)
        total = int(
            store.db.scalar(f"SELECT COUNT(*) FROM lot_changes WHERE {clause}", tuple(params))
            or 0
        )
        rows = store.db.query(
            f"SELECT * FROM lot_changes WHERE {clause} "
            "ORDER BY observed_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params) + (size, (page - 1) * size),
        )
        from ..store import row_to_change

        cache: dict[int, Any] = {}
        pairs = []
        for row in rows:
            change = row_to_change(row)
            auction_id = change.auction_id
            if auction_id and auction_id not in cache:
                cache[auction_id] = store.get_auction(auction_id)
            pairs.append((change, cache.get(auction_id)))

        return render("changes.html").render(
            active_page="changes",
            rows=pairs,
            total=total,
            page=page,
            page_size=size,
            args=request.args.to_dict(),
            type_options=[
                M.BID_CHANGED,
                M.BID_COUNT_CHANGED,
                M.NEW_LOT,
                M.REMOVED,
                M.DESCRIPTION_CHANGED,
                M.IMAGE_CHANGED,
                M.STATUS_CHANGED,
                M.QUANTITY_CHANGED,
                M.RESERVE_MET,
                M.CLOSE_TIME_CHANGED,
            ],
        )

    # ----------------------------------------------------------------- ask
    @app.route("/ask", methods=["GET", "POST"])
    def ask() -> str:
        question = (request.form.get("question") or request.args.get("q") or "").strip()
        mode = request.form.get("mode") or request.args.get("mode") or "estimate"
        engine = AIEngine(config, store)
        context: dict[str, Any] = {
            "active_page": "ask",
            "question": question,
            "mode": mode,
            "ai_available": engine.any_available,
            "comparables": [],
            "history": store.recent_estimates(10),
        }
        if not question:
            return render("ask.html").render(**context)

        terms = build_comparable_terms(question)
        comparables = store.comparable_lots(terms=terms, limit=60, sold_only=True)
        if not comparables:
            comparables = store.comparable_lots(terms=terms, limit=60, sold_only=False)
        context["comparables"] = comparables

        if not engine.any_available:
            context["error"] = (
                "No AI provider is available, so only the historical comparables "
                "below could be gathered."
            )
            return render("ask.html").render(**context)

        if mode == "general":
            answer = engine.ask(question, context_for_question(store, question))
            if answer is None:
                context["error"] = "Every configured AI provider failed; see the logs."
            else:
                context["answer"] = answer
            return render("ask.html").render(**context)

        estimate = engine.estimate_price(
            question=question, target=question, comparables=comparables
        )
        if estimate is None:
            context["error"] = "Every configured AI provider failed; see the logs."
            return render("ask.html").render(**context)
        context["estimate"] = estimate
        store.save_estimate(
            question=question,
            answer=estimate.get("reasoning", ""),
            comparables=comparables[:20],
            max_price=estimate.get("max_bid_aud"),
            provider=estimate.get("provider", ""),
            model=estimate.get("model", ""),
        )
        context["history"] = store.recent_estimates(10)
        return render("ask.html").render(**context)

    # -------------------------------------------------------------- status
    @app.route("/status")
    def status() -> str:
        engine = AIEngine(config, store)
        email = config.email
        cycles = []
        for cycle in store.recent_cycles(25):
            started = from_iso(cycle.get("started_at"))
            finished = from_iso(cycle.get("finished_at"))
            cycle["seconds"] = (
                round((finished - started).total_seconds()) if started and finished else "—"
            )
            cycles.append(cycle)
        db_path = config.path("sqlite_path")
        images_dir = config.path("images_dir")
        return render("status.html").render(
            active_page="status",
            stats=store.stats(),
            schedule=config.section("schedule"),
            reminder_leads=config.reminder_lead_minutes,
            email={
                "channel": "SMTP email" if email.configured else "log only",
                "configured": email.configured,
                "host": email.host,
                "port": email.port,
                "security": email.security,
                "sender": email.sender,
                "recipients": email.recipients,
                "reminder_recipients": email.reminder_recipients,
                "dry_run": email.dry_run,
            },
            providers=engine.describe(),
            ai_tasks={
                task: (config.get(f"ai.tasks.{task}.providers", []) or [])
                for task in ("classify", "extract_specs", "summarize_scan", "estimate_price")
            },
            cycles=cycles,
            emails=store.recent_emails(25),
            image_counts=store.image_counts(),
            config_source=str(config.source or "defaults only"),
            paths={
                "db": str(db_path),
                "db_size": _human_bytes(db_path.stat().st_size if db_path.is_file() else 0),
                "images": str(images_dir),
                "images_size": _human_bytes(_dir_size(images_dir)),
                "reports": str(config.path("reports_dir")),
            },
            fetch_client=str(config.get("fetch.client", "auto")),
            schema_version=SCHEMA_VERSION,
            fts=store.db.has_fts(),
        )

    # ------------------------------------------------------------- reports
    @app.route("/reports/")
    @app.route("/reports/<path:name>")
    def reports(name: str = "") -> Response | str:
        directory = config.path("reports_dir")
        if not name:
            files = sorted(
                (p for p in directory.glob("*.html")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            links = "".join(
                f'<li><a href="/reports/{p.name}">{p.name}</a> '
                f'<span class="muted">({_human_bytes(p.stat().st_size)})</span></li>'
                for p in files
            )
            return (
                "<h1>Exported reports</h1><ul>" + (links or "<li>None yet</li>") + "</ul>"
            )
        return send_from_directory(directory, name)

    # -------------------------------------------------------------- health
    @app.route("/healthz")
    def healthz() -> Response:
        try:
            store.db.scalar("SELECT 1")
        except Exception as exc:
            return Response(f"database error: {exc}\n", status=500, mimetype="text/plain")
        return Response("ok\n", mimetype="text/plain")

    @app.errorhandler(404)
    def not_found(_: object) -> tuple[str, int]:
        return (
            "<h1>Not found</h1><p><a href='/'>Back to the dashboard</a></p>",
            404,
        )


def _lot_sort_key(lot_number: str) -> tuple[int, str]:
    """Sort "1", "2", "10", "10A" the way a human reads a catalogue."""
    digits = ""
    for char in str(lot_number):
        if char.isdigit():
            digits += char
        else:
            break
    return (int(digits) if digits else 10**9, str(lot_number))


def _spark(snapshots: list[dict[str, Any]]) -> str:
    """Build ``points`` for a 600x60 sparkline of the bid history."""
    values = [float(s.get("current_bid") or 0) for s in snapshots]
    if len(values) < 2:
        return ""
    high = max(values) or 1.0
    step = 600 / (len(values) - 1)
    return " ".join(
        f"{index * step:.1f},{58 - (value / high) * 54:.1f}"
        for index, value in enumerate(values)
    )
