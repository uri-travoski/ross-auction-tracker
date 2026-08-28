"""IT classifier tests: keyword pre-filter and detail-time confirmation."""
from __future__ import annotations

from auction_tracker.scrape import ITClassifier
from auction_tracker.scrape.detail_page import parse_detail_page
from auction_tracker.models import Auction, Lot


def _classifier(config) -> ITClassifier:
    # No AI engine -> pure keyword logic, deterministic.
    return ITClassifier(config, engine=None)


def test_pre_filter_matches_it_title(config):
    clf = _classifier(config)
    decision = clf.pre_filter(Auction(url="x", title="General IT Online Auction"))
    assert decision.is_it is True
    assert decision.source in {"regex", "keyword"}


def test_pre_filter_matches_laptop(config):
    clf = _classifier(config)
    # Word-boundary match: "laptop" matches "Laptop" but not "Laptops".
    decision = clf.pre_filter(Auction(url="x", title="Laptop and Printer Clearance"))
    assert decision.is_it is True
    assert len(decision.matched) >= 2


def test_pre_filter_rejects_cattle(config):
    clf = _classifier(config)
    decision = clf.pre_filter(Auction(url="x", title="Cattle and Fencing Equipment"))
    assert decision.is_it is False


def test_pre_filter_excludes_placeholder(config):
    clf = _classifier(config)
    decision = clf.pre_filter(Auction(url="x", title="placeholder auction"))
    assert decision.is_it is False
    assert decision.source == "excluded"


def test_confirm_it_auction_with_lots(config, detail_page_it_html):
    clf = _classifier(config)
    auction = parse_detail_page(detail_page_it_html, "https://x", config)
    decision = clf.confirm(auction)
    assert decision.is_it is True
    assert decision.confidence >= 0.8


def test_confirm_non_it_auction(config):
    clf = _classifier(config)
    auction = Auction(url="x", title="Cattle and Fencing Equipment")
    auction.lots = [
        Lot(lot_number="1", description="1 x steel cattle crush"),
        Lot(lot_number="2", description="fencing posts and wire"),
    ]
    decision = clf.confirm(auction)
    assert decision.is_it is False


def test_confirm_mixed_auction_keeps_it_lots(config):
    """A medical-equipment auction full of monitors+laptops is tracked."""
    clf = _classifier(config)
    auction = Auction(url="x", title="Medical Equipment Clearance")
    auction.lots = [
        Lot(lot_number=str(i),
            description=f"HP ProBook laptop i7 {i}GB RAM" if i % 2 == 0
            else f"EIZO medical LCD monitor {i}cm")
        for i in range(1, 21)
    ]
    decision = clf.confirm(auction)
    assert decision.is_it is True
    # The mixed-auction path should fire because the title has no IT keyword
    # but the lots are overwhelmingly IT.
    assert clf.it_lots(auction)


def test_lot_is_it_keyword_match(config):
    clf = _classifier(config)
    assert clf.lot_is_it(Lot(lot_number="1", description="HP ZBook laptop"))
    assert clf.lot_is_it(Lot(lot_number="2", description="Brother HL-2270DW printer"))
    assert not clf.lot_is_it(Lot(lot_number="3", description="steel cattle crush"))


def test_it_lots_in_medical_fixture(config, detail_page_it_html):
    clf = _classifier(config)
    auction = parse_detail_page(detail_page_it_html, "https://x", config)
    it_lots = clf.it_lots(auction)
    # Almost every lot in the medical-IT fixture is IT equipment.
    assert len(it_lots) >= 110
