"""SQLite connection handling and forward-only migrations.

Add a schema change by appending a ``(version, sql)`` pair to ``MIGRATIONS``.
Never edit an existing migration — deployed databases have already run it.
``migrate()`` is idempotent and runs on every start-up.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .logging_setup import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

MIGRATION_1 = """
-- One row per auction we have ever seen.
CREATE TABLE IF NOT EXISTS auctions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT NOT NULL UNIQUE,
    slug              TEXT NOT NULL DEFAULT '',
    site_id           TEXT NOT NULL DEFAULT '',   -- site's internal id, e.g. 14704
    title             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'FORTHCOMING',
    start_at          TEXT,
    end_at            TEXT,
    end_at_original   TEXT,                       -- first end time ever seen
    end_at_updated_at TEXT,
    location          TEXT NOT NULL DEFAULT '',
    thumbnail_url     TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    inspection        TEXT NOT NULL DEFAULT '',
    collection        TEXT NOT NULL DEFAULT '',
    contact           TEXT NOT NULL DEFAULT '',
    terms             TEXT NOT NULL DEFAULT '',
    lot_count         INTEGER NOT NULL DEFAULT 0,
    is_it             INTEGER NOT NULL DEFAULT 0,
    it_reason         TEXT NOT NULL DEFAULT '',
    it_confidence     REAL NOT NULL DEFAULT 0,
    it_source         TEXT NOT NULL DEFAULT '',
    first_seen_at     TEXT NOT NULL,
    last_scraped_at   TEXT,
    finalized_at      TEXT,
    extra_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_auctions_status  ON auctions(status);
CREATE INDEX IF NOT EXISTS idx_auctions_end     ON auctions(end_at);
CREATE INDEX IF NOT EXISTS idx_auctions_is_it   ON auctions(is_it);
CREATE INDEX IF NOT EXISTS idx_auctions_site_id ON auctions(site_id);

-- Current state of every lot. History lives in lot_changes/lot_snapshots.
CREATE TABLE IF NOT EXISTS lots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id        INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    lot_number        TEXT NOT NULL,
    site_id           TEXT NOT NULL DEFAULT '',   -- data-lot-id
    description       TEXT NOT NULL DEFAULT '',
    short_description TEXT NOT NULL DEFAULT '',
    quantity          INTEGER NOT NULL DEFAULT 1,
    thumbnail_url     TEXT NOT NULL DEFAULT '',
    image_urls_json   TEXT NOT NULL DEFAULT '[]', -- original/full-size photos
    thumb_urls_json   TEXT NOT NULL DEFAULT '[]',
    image_count       INTEGER NOT NULL DEFAULT 0,
    current_bid       REAL,
    currency          TEXT NOT NULL DEFAULT 'AUD',
    bid_count         INTEGER NOT NULL DEFAULT 0,
    highest_bidder_id TEXT NOT NULL DEFAULT '',
    bidder_seq_json   TEXT NOT NULL DEFAULT '[]',
    met_reserve       INTEGER,
    bidding_status    TEXT NOT NULL DEFAULT 'ACTIVE',
    status_label      TEXT NOT NULL DEFAULT '',
    time_remaining    TEXT NOT NULL DEFAULT '',
    closes_at         TEXT,
    opens_at          TEXT,
    bid_url           TEXT NOT NULL DEFAULT '',
    category          TEXT NOT NULL DEFAULT '',
    brand             TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    specs_json        TEXT NOT NULL DEFAULT '{}',
    is_it             INTEGER,
    final_bid         REAL,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    removed_at        TEXT,
    extra_json        TEXT NOT NULL DEFAULT '{}',
    UNIQUE(auction_id, lot_number)
);
CREATE INDEX IF NOT EXISTS idx_lots_auction ON lots(auction_id);
CREATE INDEX IF NOT EXISTS idx_lots_site_id ON lots(site_id);
CREATE INDEX IF NOT EXISTS idx_lots_brand   ON lots(brand);
CREATE INDEX IF NOT EXISTS idx_lots_final   ON lots(final_bid);

-- Append-only change log. This is what "highlight changes" reads from.
CREATE TABLE IF NOT EXISTS lot_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id      INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    lot_id          INTEGER REFERENCES lots(id) ON DELETE CASCADE,
    lot_number      TEXT NOT NULL DEFAULT '',
    lot_site_id     TEXT NOT NULL DEFAULT '',
    observed_at     TEXT NOT NULL,
    change_type     TEXT NOT NULL,
    field_name      TEXT NOT NULL DEFAULT '',
    prev_value      TEXT,
    new_value       TEXT,
    significant     INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    cycle_id        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_changes_auction ON lot_changes(auction_id);
CREATE INDEX IF NOT EXISTS idx_changes_lot     ON lot_changes(lot_id);
CREATE INDEX IF NOT EXISTS idx_changes_time    ON lot_changes(observed_at);
CREATE INDEX IF NOT EXISTS idx_changes_cycle   ON lot_changes(cycle_id);
CREATE INDEX IF NOT EXISTS idx_changes_type    ON lot_changes(change_type);

-- Bid time series: one row whenever a lot's bidding state moves. Powers the
-- "is bidding picking up or slowing down" trend view.
CREATE TABLE IF NOT EXISTS lot_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id         INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    auction_id     INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    observed_at    TEXT NOT NULL,
    current_bid    REAL,
    bid_count      INTEGER NOT NULL DEFAULT 0,
    unique_bidders INTEGER NOT NULL DEFAULT 0,
    met_reserve    INTEGER,
    minutes_to_close REAL,
    cycle_id       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_snapshots_lot  ON lot_snapshots(lot_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_auct ON lot_snapshots(auction_id, observed_at);

-- Every downloaded image file (thumbnails AND originals).
CREATE TABLE IF NOT EXISTS images (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id     INTEGER REFERENCES auctions(id) ON DELETE CASCADE,
    lot_id         INTEGER REFERENCES lots(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,               -- thumbnail | original | auction
    source_url     TEXT NOT NULL,
    site_image_id  TEXT NOT NULL DEFAULT '',    -- e.g. 4203183 from the path
    local_path     TEXT NOT NULL DEFAULT '',    -- relative to storage.images_dir
    sha256         TEXT NOT NULL DEFAULT '',
    byte_size      INTEGER NOT NULL DEFAULT 0,
    content_type   TEXT NOT NULL DEFAULT '',
    downloaded_at  TEXT,
    error          TEXT NOT NULL DEFAULT '',
    UNIQUE(source_url)
);
CREATE INDEX IF NOT EXISTS idx_images_lot     ON images(lot_id);
CREATE INDEX IF NOT EXISTS idx_images_auction ON images(auction_id);
CREATE INDEX IF NOT EXISTS idx_images_sha     ON images(sha256);

-- One row per scrape run.
CREATE TABLE IF NOT EXISTS cycles (
    id               TEXT PRIMARY KEY,
    cycle_type       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    pages_scanned    INTEGER NOT NULL DEFAULT 0,
    auctions_seen    INTEGER NOT NULL DEFAULT 0,
    auctions_new     INTEGER NOT NULL DEFAULT 0,
    auctions_scraped INTEGER NOT NULL DEFAULT 0,
    lots_seen        INTEGER NOT NULL DEFAULT 0,
    lots_changed     INTEGER NOT NULL DEFAULT 0,
    changes_recorded INTEGER NOT NULL DEFAULT 0,
    images_downloaded INTEGER NOT NULL DEFAULT 0,
    ai_calls         INTEGER NOT NULL DEFAULT 0,
    emails_sent      INTEGER NOT NULL DEFAULT 0,
    errors_json      TEXT NOT NULL DEFAULT '[]',
    ai_summary       TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cycles_started ON cycles(started_at);
CREATE INDEX IF NOT EXISTS idx_cycles_type    ON cycles(cycle_type);

-- Reminder dedup: at most one send per (auction, lead, scheduled end time).
CREATE TABLE IF NOT EXISTS reminder_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id      INTEGER NOT NULL REFERENCES auctions(id) ON DELETE CASCADE,
    lead_minutes    INTEGER NOT NULL,
    scheduled_for   TEXT NOT NULL,
    sent_at         TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    recipients      TEXT NOT NULL DEFAULT '',
    message_preview TEXT NOT NULL DEFAULT '',
    UNIQUE(auction_id, lead_minutes, scheduled_for)
);

-- Outbound email audit trail + hourly rate limiting.
CREATE TABLE IF NOT EXISTS email_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at     TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    auction_id  INTEGER,
    subject     TEXT NOT NULL DEFAULT '',
    recipients  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'SENT',
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_sent ON email_log(sent_at);

-- AI response cache, keyed by task + hash of the exact input.
CREATE TABLE IF NOT EXISTS ai_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    response    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(task, input_hash)
);

-- Every AI interaction, for cost/debug visibility.
CREATE TABLE IF NOT EXISTS ai_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at    TEXT NOT NULL,
    cycle_id     TEXT NOT NULL DEFAULT '',
    task         TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    auction_id   INTEGER,
    lot_id       INTEGER,
    cached       INTEGER NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL DEFAULT 1,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_time ON ai_calls(called_at);

-- Saved "what should I pay?" questions and the AI's answers.
CREATE TABLE IF NOT EXISTS price_estimates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    question      TEXT NOT NULL,
    lot_id        INTEGER REFERENCES lots(id) ON DELETE SET NULL,
    comparables   TEXT NOT NULL DEFAULT '[]',
    answer        TEXT NOT NULL DEFAULT '',
    max_price     REAL,
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

# Full-text search over lot descriptions, kept in sync by triggers. Isolated in
# its own migration so a build of SQLite without FTS5 can skip it gracefully.
MIGRATION_2 = """
CREATE VIRTUAL TABLE IF NOT EXISTS lots_fts USING fts5(
    description,
    brand,
    model,
    content='lots',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS lots_fts_ai AFTER INSERT ON lots BEGIN
    INSERT INTO lots_fts(rowid, description, brand, model)
    VALUES (new.id, new.description, new.brand, new.model);
END;
CREATE TRIGGER IF NOT EXISTS lots_fts_ad AFTER DELETE ON lots BEGIN
    INSERT INTO lots_fts(lots_fts, rowid, description, brand, model)
    VALUES ('delete', old.id, old.description, old.brand, old.model);
END;
CREATE TRIGGER IF NOT EXISTS lots_fts_au AFTER UPDATE ON lots BEGIN
    INSERT INTO lots_fts(lots_fts, rowid, description, brand, model)
    VALUES ('delete', old.id, old.description, old.brand, old.model);
    INSERT INTO lots_fts(rowid, description, brand, model)
    VALUES (new.id, new.description, new.brand, new.model);
END;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_1),
    (2, MIGRATION_2),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


class Database:
    """Thin wrapper around a SQLite file.

    One connection per thread (the web server and the scheduler run
    concurrently), WAL mode so readers never block the writer.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._shared: sqlite3.Connection | None = None

    # -- connections ------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        if str(self.path) == ":memory:":
            # An in-memory DB must reuse one connection or the schema vanishes.
            if self._shared is None:
                self._shared = self._connect()
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=30.0, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if str(self.path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction. Commits on success, rolls back on error."""
        with self._write_lock:
            conn = self.connection
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # -- queries ----------------------------------------------------------
    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> object:
        row = self.query_one(sql, params)
        return row[0] if row else None

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.write() as conn:
            return conn.execute(sql, params)

    # -- schema -----------------------------------------------------------
    def applied_versions(self) -> set[int]:
        exists = self.scalar(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if not exists:
            return set()
        return {int(r[0]) for r in self.query("SELECT version FROM schema_migrations")}

    def migrate(self) -> list[int]:
        """Apply pending migrations. Returns the versions applied."""
        from .util import now_utc, to_iso

        applied = self.applied_versions()
        done: list[int] = []
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            try:
                with self._write_lock:
                    conn = self.connection
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_migrations(version, applied_at)"
                        " VALUES (?, ?)",
                        (version, to_iso(now_utc())),
                    )
            except sqlite3.OperationalError as exc:
                # FTS5 is optional; everything else is fatal.
                if "fts5" in str(exc).lower():
                    log.warning(
                        "skipping migration %s: SQLite has no FTS5 (search falls "
                        "back to LIKE): %s",
                        version,
                        exc,
                    )
                    continue
                raise
            done.append(version)
            log.info("applied migration", extra={"version": version})
        return done

    def has_fts(self) -> bool:
        return bool(
            self.scalar(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lots_fts'"
            )
        )

    def close(self) -> None:
        for conn in (getattr(self._local, "conn", None), self._shared):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        self._local = threading.local()
        self._shared = None


def open_database(path: str | Path, *, migrate: bool = True) -> Database:
    db = Database(path)
    if migrate:
        db.migrate()
    return db
