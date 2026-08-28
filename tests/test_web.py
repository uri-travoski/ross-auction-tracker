"""Web UI route smoke tests against a populated in-memory store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auction_tracker import models as M
from auction_tracker.models import Auction, Lot, Change
from auction_tracker.web.app import create_app


@pytest.fixture()
def app(config, store):
    config._tree["logging"]["file_enabled"] = False
    return create_app(config, store)


@pytest.fixture()
def client(app):
    return app.test_client()


def _populate(store) -> tuple[Auction, Lot]:
    end = datetime.now(timezone.utc) + timedelta(days=2)
    auction = Auction(
        url="https://auctions.com.au/x.html", title="Test IT Auction",
        site_id="14704", is_it=True, lot_count=2, status=M.IN_PROGRESS,
        start_at=datetime.now(timezone.utc) - timedelta(days=1),
        end_at=end, end_at_original=end,
        first_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
        location="Welshpool WA",
    )
    auction, _, _ = store.upsert_auction(auction)
    lot = Lot(
        lot_number="1", site_id="1001", description="HP ZBook G8 i7 16GB",
        current_bid=150.0, bid_count=4, quantity=1,
        closes_at=end, opens_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    store.insert_lot(auction.id, lot)
    store.record_changes([
        Change(change_type=M.BID_CHANGED, lot_number="1", lot_id=lot.id,
               auction_id=auction.id, previous=100.0, current=150.0,
               significant=True, observed_at=datetime.now(timezone.utc),
               cycle_id="c1"),
    ], cycle_id="c1")
    return auction, lot


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert b"ok" in resp.data.lower()


def test_dashboard_renders(client, store):
    _populate(store)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Test IT Auction" in resp.data


def test_auctions_list_renders(client, store):
    _populate(store)
    resp = client.get("/auctions")
    assert resp.status_code == 200
    assert b"Test IT Auction" in resp.data


def test_auctions_list_search(client, store):
    _populate(store)
    resp = client.get("/auctions?q=Test")
    assert resp.status_code == 200
    assert b"Test IT Auction" in resp.data


def test_auctions_list_scope_past(client, store):
    _populate(store)
    resp = client.get("/auctions?scope=past")
    assert resp.status_code == 200


def test_auction_detail_renders(client, store):
    auction, lot = _populate(store)
    resp = client.get(f"/auction/{auction.id}")
    assert resp.status_code == 200
    assert b"HP ZBook" in resp.data


def test_auction_detail_404(client):
    resp = client.get("/auction/99999")
    assert resp.status_code == 404


def test_lot_detail_renders(client, store):
    auction, lot = _populate(store)
    resp = client.get(f"/lot/{lot.id}")
    assert resp.status_code == 200
    assert b"HP ZBook" in resp.data


def test_lot_detail_404(client):
    resp = client.get("/lot/99999")
    assert resp.status_code == 404


def test_lots_search_renders(client, store):
    _populate(store)
    resp = client.get("/lots")
    assert resp.status_code == 200
    assert b"HP ZBook" in resp.data


def test_lots_search_with_query(client, store):
    _populate(store)
    resp = client.get("/lots?q=ZBook")
    assert resp.status_code == 200
    assert b"HP ZBook" in resp.data


def test_compare_renders(client, store):
    auction, lot = _populate(store)
    resp = client.get(f"/compare?ids={auction.id}")
    assert resp.status_code == 200


def test_changes_renders(client, store):
    _populate(store)
    resp = client.get("/changes")
    assert resp.status_code == 200


def test_ask_get_renders(client, store):
    _populate(store)
    resp = client.get("/ask")
    assert resp.status_code == 200


def test_ask_post_without_ai_returns_graceful(client, store):
    _populate(store)
    resp = client.post("/ask", data={"question": "HP ZBook G8"})
    assert resp.status_code == 200
    # No AI provider configured -> the page should still render, with a
    # message that AI is unavailable rather than a 500.
    assert b"unavailable" in resp.data.lower() or b"answer" in resp.data.lower()


def test_status_renders(client, store):
    _populate(store)
    resp = client.get("/status")
    assert resp.status_code == 200


def test_reports_dir_renders(client, store, config):
    config.path("reports_dir").mkdir(parents=True, exist_ok=True)
    resp = client.get("/reports/")
    assert resp.status_code == 200


def test_pagination_default_100(client, store):
    # Populate 5 auctions and confirm page_size=100 is accepted.
    for i in range(5):
        end = datetime.now(timezone.utc) + timedelta(days=i + 1)
        auction = Auction(
            url=f"https://x/{i}", title=f"Auction {i}", is_it=True,
            end_at=end, end_at_original=end,
            first_seen_at=datetime.now(timezone.utc),
        )
        store.upsert_auction(auction)
    resp = client.get("/auctions?page_size=100")
    assert resp.status_code == 200


def test_basic_auth_blocks_when_configured(config, store, monkeypatch):
    monkeypatch.setenv("WEB_USERNAME", "admin")
    monkeypatch.setenv("WEB_PASSWORD", "secret")
    app = create_app(config, store)
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 401
    resp = client.get("/", auth=("admin", "secret"))
    assert resp.status_code == 200
    # healthz is exempt.
    assert client.get("/healthz").status_code == 200
