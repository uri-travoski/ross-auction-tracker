"""Database migration tests."""
from __future__ import annotations

from auction_tracker.db import SCHEMA_VERSION, Database, MIGRATIONS


def test_migrations_apply_on_fresh_db(tmp_path):
    db = Database(tmp_path / "test.db")
    applied = db.migrate()
    assert 1 in applied
    assert 2 in applied
    assert 3 in applied
    assert SCHEMA_VERSION == MIGRATIONS[-1][0]


def test_migrations_are_idempotent(tmp_path):
    db = Database(tmp_path / "test.db")
    db.migrate()
    second = db.migrate()
    assert second == [], "second migrate() should apply nothing"


def test_all_core_tables_exist(tmp_path):
    db = Database(tmp_path / "test.db")
    db.migrate()
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in (
        "auctions", "lots", "lot_changes", "lot_snapshots", "images",
        "cycles", "reminder_log", "email_log", "ai_cache", "ai_calls",
        "price_estimates", "ai_providers", "schema_migrations",
    ):
        assert expected in tables, f"missing table {expected}"


def test_applied_versions_recorded(tmp_path):
    db = Database(tmp_path / "test.db")
    db.migrate()
    versions = db.applied_versions()
    assert versions == {1, 2, 3}


def test_fts_table_present_when_supported(tmp_path):
    db = Database(tmp_path / "test.db")
    db.migrate()
    if db.has_fts():
        tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "lots_fts" in tables


def test_in_memory_db_migrates():
    db = Database(":memory:")
    db.migrate()
    assert db.applied_versions() == {1, 2, 3}
    # A select against a migrated table should work.
    assert db.scalar("SELECT COUNT(*) FROM auctions") == 0
