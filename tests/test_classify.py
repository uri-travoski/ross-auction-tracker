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


def test_rejects_industrial_false_positives(config):
    clf = _classifier(config)
    false_positives = [
        ("Diesel Engines, Stamford Alternators, Induction Motors & Pumps", "CAT 793F CAB, SURFACE RUST"),
        ("Hardware, Eco-Chlor Chlorine & Heavy Duty Drain Cleaner Online", "2x TUBS OF ECO-CHLOR DRY CHLORINE TABLET BACTERICIDE"),
        ("Caterpillar Spares, Mining Spares & Stores Inventory Online", "HYDRAULIC RAM CYLINDER 50 TON"),
        ("Machine Cabs Online Welshpool Auction", "CAT 793F CAB, SURFACE RUST, MOUNTED ON SKID"),
        ("Metalworking & Woodworking Workshop Online Welshpool Auction", "WOODWORKING PLUNGE ROUTER 1/2 INCH"),
        ("Gym & Exercise Equipment Online Welshpool Auction", "COMMERCIAL SQUAT RACK AND BENCH"),
        ("Recreational Vehicles, Campers, Jet Skis, Zodiacs", "200 HP OUTBOARD MOTOR MERCURY"),
    ]
    for title, lot_desc in false_positives:
        auction = Auction(url="https://x", title=title)
        auction.lots = [Lot(lot_number=str(i), description=lot_desc) for i in range(1, 10)]
        decision = clf.confirm(auction)
        assert decision.is_it is False, f"Expected {title} to be rejected, got {decision}"


def test_lot_negative_and_positive_filtering(config):
    clf = _classifier(config)
    # Negative examples that must NOT be IT
    assert not clf.lot_is_it("2x TUBS OF ECO-CHLOR DRY CHLORINE TABLET BACTERICIDE (GRANUL)")
    assert not clf.lot_is_it("CAT 793F CAB, SOME MARKS, SURFACE RUST, MOUNTED ON SKID")
    assert not clf.lot_is_it("1 SET OF 2 ENGINES MERCURY SEA PRO 200 HP OUTBOARD MOTORS")
    assert not clf.lot_is_it("WOODWORKING PLUNGE ROUTER 1/2 INCH")
    assert not clf.lot_is_it("HYDRAULIC RAM CYLINDER 50 TON")
    assert not clf.lot_is_it("COMMERCIAL SQUAT RACK AND BENCH")
    assert not clf.lot_is_it("FLY SCREEN FOR ALUMINIUM SLIDING WINDOW")
    assert not clf.lot_is_it("LG DIRECT DRIVE DISHWASHER")
    assert not clf.lot_is_it("1 X MARK BRIC BRAND DISPLAY SNAP UP DISPLAY SYSTEM")
    assert not clf.lot_is_it("1 X DRYING RACK")
    assert not clf.lot_is_it("5x LED TEMPERATURE DISPLAY SMART CUPS, 500ml")

    # Positive examples that MUST be IT
    assert clf.lot_is_it("7 X LG LCD COMPUTER MONITORS, 23 INCH, UNTESTED")
    assert clf.lot_is_it("4 X BENQ LCD COMPUTER MONITORS")
    assert clf.lot_is_it("EPSON PROJECTOR, MODEL: EB-X41")
    assert clf.lot_is_it("4X BOXES OF COMPUTER PERIPHERALS AND CABLES")
    assert clf.lot_is_it("Panasonic Toughpad FZ-G1 tablet")
    assert clf.lot_is_it("Lenovo ThinkPad T480s laptop i7 16GB RAM")
    assert clf.lot_is_it("HP EliteDesk 800 G4 Mini Desktop")
    assert clf.lot_is_it("Cisco Catalyst 2960 PoE Network Switch")
    assert clf.lot_is_it("Surface Pro 7 Tablet")


def test_ai_reconciliation_authoritative_rejection(config):
    """When AI returns is_it: false, AI verdict rejects an otherwise keyword-matched candidate."""
    class FakeAIEngine:
        def available_for(self, task):
            return True
        def classify_auction(self, auction, sample):
            return {
                "is_it": False,
                "confidence": 0.9,
                "mixed": False,
                "categories": [],
                "reason": "Industrial machinery, not computing equipment",
            }

    clf = ITClassifier(config, engine=FakeAIEngine())
    auction = Auction(url="https://x", title="Motors, Pumps and Switches Online Auction")
    auction.lots = [Lot(lot_number="1", description="Electric motor and control switch")]
    decision = clf.confirm(auction)
    assert decision.is_it is False
    assert "Industrial machinery" in decision.reason
    assert decision.source == "ai"


def test_ai_reconciliation_confirms_mixed(config):
    """When AI returns is_it: true with mixed: true, it confirms tracking."""
    class FakeAIEngine:
        def available_for(self, task):
            return True
        def classify_auction(self, auction, sample):
            return {
                "is_it": True,
                "confidence": 0.88,
                "mixed": True,
                "categories": ["monitors", "laptops"],
                "reason": "Contains 30 enterprise LCD monitors",
            }

    clf = ITClassifier(config, engine=FakeAIEngine())
    auction = Auction(url="https://x", title="Hospital Surplus Clearance")
    decision = clf.confirm(auction)
    assert decision.is_it is True
    assert decision.confidence == 0.88
    assert decision.mixed is True
    assert "monitors" in decision.categories

