"""The IT filter: a multi-tiered keyword and context pass, then AI confirmation.

Two passes:
1. **Index time** — title, location and card text only. Catches obvious cases and
   filters out clear non-IT domains (e.g. diesel engines, earthmoving, timber,
   farm clearances, cattle yards, gym equipment) so the crawler can prioritize.
2. **Detail time** — full description plus real lot text. Disqualifies false
   positives from polysemous words (e.g. 'surface rust', 'chlorine tablet',
   '200 hp motor', 'hydraulic ram', 'woodworking router', 'drying rack') and
   detects genuine mixed auctions (e.g. a medical clearance with 25+ LCD monitors).

When AI is configured, the AI Engine acts as the authoritative final arbiter
for candidate auctions and lots. If AI is unavailable or offline, the strict,
context-aware deterministic logic operates safely fail-closed on ambiguous words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from ..ai import AIEngine
from ..config import Config
from ..logging_setup import get_logger
from ..models import Auction, Lot
from ..util import clean_text, truncate

log = get_logger(__name__)

KEYWORD = "keyword"
AI = "ai"
MIXED = "mixed-lots"
REGEX = "regex"
EXCLUDED = "excluded"


@dataclass
class Decision:
    """Verdict on one auction."""

    is_it: bool
    confidence: float
    reason: str
    source: str
    certain: bool = False
    matched: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    mixed: bool = False

    def apply(self, auction: Auction) -> None:
        auction.is_it = self.is_it
        auction.it_confidence = round(self.confidence, 3)
        auction.it_reason = truncate(self.reason, 300)
        auction.it_source = self.source
        if self.categories:
            auction.extra["it_categories"] = self.categories
        if self.matched:
            auction.extra["it_keywords_matched"] = self.matched[:20]
        if self.mixed:
            auction.extra["mixed_auction"] = True


# ======================================================================
# Compiled Patterns for Robust Classification
# ======================================================================

# Unambiguous IT keywords that almost exclusively refer to IT equipment.
DEFINITIVE_IT_PATTERNS = [
    r"\blaptops?\b",
    r"\bnotebooks?\b",
    r"\bchromebooks?\b",
    r"\bmacbooks?\b",
    r"\b(?:macbook\s+(?:air|pro))\b",
    r"\b(?:imacs?|mac\s+mini|mac\s+studio|mac\s+pro)\b",
    r"\b(?:desktop\s+pcs?|desktop\s+computers?|workstation\s+pcs?|all-in-one\s+pcs?|mini\s+pcs?|sff\s+pcs?)\b",
    r"\b(?:thinkpads?|thinkcentres?|thinkstations?|elitebooks?|probooks?|zbooks?|elitedesks?|prodesks?|optiplex|latitudes?|precision\s+workstations?|toughbooks?|toughpads?)\b",
    r"\b(?:poweredges?|proliants?|server\s+chassis|blade\s+servers?)\b",
    r"\b(?:cisco\s+catalysts?|cisco\s+switch(?:es)?|ubiquiti|unifi|meraki|mikrotik|fortigate|pfsense)\b",
    r"\b(?:ethernet\s+switch(?:es)?|network\s+switch(?:es)?|poe\s+switch(?:es)?|managed\s+switch(?:es)?|patch\s+panels?)\b",
    r"\b(?:wifi\s+routers?|wireless\s+routers?|gigabit\s+routers?|ethernet\s+routers?|mesh\s+routers?)\b",
    r"\b(?:graphics\s+cards?|geforces?|quadros?|radeons?|motherboards?|intel\s+core|core\s+i[3579]|amd\s+ryzens?|xeons?|epycs?|nvme\s+ssds?|sata\s+ssds?)\b",
    r"\b(?:(?:lcd|led|oled|ips|fhd|4k|qhd|curved|gaming|hdmi|displayport|commercial|medical)?\s*(?:computer\s+)?monitors?)\b",
    r"\b(?:projectors?|eizo\s+radiforce)\b",
    r"\b(?:printers?|plotters?|scanners?|copiers?)\b",
    r"\b(?:laser\s+printers?|laserjets?|inkjet\s+printers?|receipt\s+printers?|barcode\s+scanners?|zebra\s+printers?)\b",
    r"\b(?:microsoft\s+surfaces?|surface\s+pros?|surface\s+laptops?|surface\s+gos?|surface\s+books?)\b",
    r"\b(?:apple\s+ipads?|ipad\s+pros?|ipad\s+airs?|iphones?|android\s+phones?|pixel\s+phones?|galaxy\s+tabs?)\b",
    r"\b(?:nas\s+storages?|san\s+storages?|synolog(?:y|ies)|qnaps?|computer\s+peripherals?|docking\s+stations?|usb-c\s+hubs?)\b",
]

# Patterns for words that are IT ONLY when accompanied by computing/IT context.
CONTEXT_IT_PATTERNS = [
    r"\b(?:\d+\s*(?:gb|mb)\s*ram|ddr[345]|sodimm|ecc\s+memory)\b",
    r"\b(?:(?:wi-?fi|wireless|network|gigabit|ethernet)\s+routers?)\b",
    r"\b(?:(?:network|ethernet|gigabit|poe\+?|managed|unmanaged|cisco)\s+switch(?:es)?)\b",
    r"\b(?:(?:server|network|equipment|data|19[\s\"-]*inch|19in|42u|24u|12u)\s+racks?|rackmounts?)\b",
    r"\b(?:tablets?\s+pcs?|android\s+tablets?|wacoms?)\b",
    r"\b(?:(?:hp|hewlett\s+packard)\s+(?:probook|elitebook|prodesk|elitedesk|zbook|laserjet|deskjet|compaq|thin\s+client|laptop|desktop|printer|monitor|server|workstation)s?)\b",
    r"\b(?:dell\s+(?:optiplex|latitude|precision|inspiron|xps|vostro|poweredge|ultrasharp|monitor|laptop|desktop|server)s?)\b",
    r"\b(?:lenovo\s+(?:thinkpad|thinkcentre|thinkstation|ideapad|yoga|monitor|laptop|desktop|pc)s?)\b",
    r"\b(?:(?:computer|lcd|led|oled|ips|gaming|hdmi|displayport)\s+(?:screens?|displays?)|monitor\s+(?:arms?|risers?|stands?))\b",
    r"\b(?:(?:mini|desktop|gaming|tower|all-in-one)\s+pcs?|pcs?)\b",
]

# Lot-level negative patterns: disqualifies a lot from being counted as IT equipment.
NEGATIVE_LOT_PATTERNS = [
    r"\b(?:\d+\s*hp\b|hp\s+(?:motor|engine|pump|outboard|diesel)s?)\b",
    r"\b(?:hydraulic\s+rams?|ram\s+(?:cylinder|pump)s?)\b",
    r"\b(?:surface\s+(?:rust|plate|grinder|grinding|finish|mount|damage|scratch)s?|(?:road|ground|table|cooking)\s+surfaces?)\b",
    r"\b(?:(?:chlorine|cleaning|bactericide|detergent|sanitizer|effervescent|pool)\s+tablets?|tablets?\s+(?:bactericide|dispenser)s?)\b",
    r"\b(?:(?:woodworking|plunge|cnc|trimmer|spindle)\s+routers?|routers?\s+(?:bit|bits|table|cutter)s?)\b",
    r"\b(?:(?:pallet|towel|drying|squat|weight|dumbbell|roof|bike|dish|storage|magazine|wine)\s+racks?|racks?\s+(?:storage|stand)s?)\b",
    r"\b(?:(?:fly|mesh|trommel|crusher|vibrating|shower|insect|security|window)\s+screens?|windscreens?)\b",
    r"\b(?:displays?\s+(?:cabinet|shelf|case)s?|merchandise\s+displays?|snap\s+up\s+displays?)\b",
    r"\b(?:(?:light|ignition|pressure|limit|toggle|disconnect|safety|foot|temperature|rocker)\s+switch(?:es)?)\b",
    r"\b(?:dishwashers?|washing\s+machines?|washer\s+dryers?|refrigerators?|fridges?|microwaves?|sewing\s+machines?)\b",
    r"\b(?:(?:machine|operator|truck|tractor|excavator|crane|komatsu|caterpillar|cat)\s+cabs?|cabs?\s+assembl(?:y|ies))\b",
    r"\b(?:diesel\s+engines?|stamford\s+alternators?|induction\s+motors?|generators?\s+sets?|drill\s+rigs?|conveyor\s+belts?)\b",
    r"\b(?:\d+\s*pc\s+(?:socket|tool|spanner|wrench|drill|set)s?|komatsu\s+pc)\b",
]

# Non-IT auction title patterns (heavy industrial/farm/vehicle categories)
NON_IT_AUCTION_PATTERNS = [
    r"\b(?:diesel\s+engines?|stamford\s+alternators?|generators?\s+sets?)\b",
    r"\b(?:caterpillar|komatsu|earthmoving|excavator|drill\s+rigs?|mining\s+spares|processing\s+plant|cranes?)\b",
    r"\b(?:trucks?|trailers?|tractors?|machine\s+cabs?|utility\s+trays?)\b",
    r"\b(?:caravans?|campers?|motorhomes?|jet\s+skis?|zodiacs?|sealegs|boats?|outboard\s+motors?)\b",
    r"\b(?:timber|jarrah|pine|doors\s+clearance|tiles\s+clearance|roofing|hardwood|burl|feature\s+grade\s+logs)\b",
    r"\b(?:metalworking|woodworking|welding|workshop\s+tools|rigging\s+equipment)\b",
    r"\b(?:cattle\s+yards?|farm\s+fencing|farm\s+clearance|livestock)\b",
    r"\b(?:chlorine|drain\s+cleaner|paint\s+clearance|coatings)\b",
    r"\b(?:gym\s*(?:&|and)\s*exercise|fitness\s+equipment)\b",
    r"\b(?:commercial\s+meat\s+processing|bakery\s+equipment)\b",
    r"\b(?:gemstones?\s*(&|and)\s*jeweller(?:y|ies))\b",
]


def _compiled(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    out = []
    for pattern in patterns or []:
        try:
            out.append(re.compile(pattern, re.I))
        except re.error as exc:
            log.warning("bad filter regex %r: %s", pattern, exc)
    return out


def _keyword_pattern(keywords: Sequence[str]) -> re.Pattern[str] | None:
    cleaned = sorted({clean_text(k).lower() for k in keywords if clean_text(k)}, key=len, reverse=True)
    if not cleaned:
        return None
    alternation = "|".join(re.escape(k) for k in cleaned)
    return re.compile(rf"(?<![\w-])({alternation})(?![\w-])", re.I)


def matched_keywords(text: str, pattern: re.Pattern[str] | None) -> list[str]:
    if not text or pattern is None:
        return []
    found: list[str] = []
    for match in pattern.finditer(text):
        token = match.group(1).lower()
        if token not in found:
            found.append(token)
    return found


class ITClassifier:
    """Reusable classifier; compiles patterns once per cycle."""

    def __init__(self, config: Config, engine: AIEngine | None = None) -> None:
        self.config = config
        self.engine = engine
        section = config.section("filter")
        self.keywords = _keyword_pattern(section.get("it_keywords", []) or [])
        self.include = _compiled(section.get("always_include_title_regex", []) or [])
        self.exclude = _compiled(section.get("always_exclude_title_regex", []) or [])
        self.use_ai_always = bool(section.get("use_ai_always", True))
        self.use_ai_when_uncertain = bool(section.get("use_ai_when_uncertain", True))
        self.keep_mixed = bool(section.get("keep_it_lots_in_mixed_auctions", True))

        self.definitive = _compiled(DEFINITIVE_IT_PATTERNS)
        self.contextual = _compiled(CONTEXT_IT_PATTERNS)
        self.negative_lots = _compiled(NEGATIVE_LOT_PATTERNS)
        self.non_it_auctions = _compiled(NON_IT_AUCTION_PATTERNS)

    # ==================================================================
    # Pass 1: index-time pre-filter
    # ==================================================================
    def pre_filter(self, auction: Auction) -> Decision:
        """Title-only verdict. Never calls the AI (it runs over many cards)."""
        title = clean_text(auction.title)
        for pattern in self.exclude:
            if pattern.search(title):
                return Decision(
                    False, 0.99, f"title matched exclude rule {pattern.pattern}",
                    EXCLUDED, certain=True,
                )
        for pattern in self.include:
            if pattern.search(title):
                return Decision(
                    True, 0.95, f"title matched include rule {pattern.pattern}",
                    REGEX, certain=True,
                )

        # Check non-IT auction categories
        for pat in self.non_it_auctions:
            if pat.search(title):
                has_definitive = any(d.search(title) for d in self.definitive)
                if not has_definitive:
                    return Decision(
                        False, 0.9, f"title matches non-IT category: {pat.pattern}",
                        EXCLUDED, certain=True,
                    )

        # Check definitive and contextual hits
        def_hits = [m.group(0).lower() for p in self.definitive for m in p.finditer(title)]
        ctx_hits = [m.group(0).lower() for p in self.contextual for m in p.finditer(title)]
        kw_hits = matched_keywords(title, self.keywords)
        all_hits = list(dict.fromkeys(def_hits + ctx_hits + kw_hits))

        if def_hits:
            return Decision(
                True, 0.9, f"title IT equipment: {', '.join(all_hits[:6])}", KEYWORD,
                certain=True, matched=all_hits,
            )
        if len(all_hits) >= 2:
            return Decision(
                True, 0.85, f"title keywords: {', '.join(all_hits[:6])}", KEYWORD,
                certain=False, matched=all_hits,
            )
        if len(all_hits) == 1:
            return Decision(
                True, 0.6, f"title keyword: {all_hits[0]}", KEYWORD, matched=all_hits
            )
        return Decision(False, 0.35, "no IT keywords in title", KEYWORD)

    # ==================================================================
    # Pass 2: detail-time confirmation
    # ==================================================================
    def confirm(self, auction: Auction) -> Decision:
        """Full verdict using description and lot text, with AI adjudication."""
        title = clean_text(auction.title)
        for pattern in self.exclude:
            if pattern.search(title):
                return Decision(
                    False, 0.99, f"excluded by rule {pattern.pattern}", EXCLUDED,
                    certain=True,
                )

        forced = any(pattern.search(title) for pattern in self.include)

        # How many lots individually look like IT?
        it_lots = [lot for lot in auction.lots if self.lot_is_it(lot)]
        lot_ratio = (len(it_lots) / len(auction.lots)) if auction.lots else 0.0

        title_def = [m.group(0).lower() for p in self.definitive for m in p.finditer(title)]
        title_ctx = [m.group(0).lower() for p in self.contextual for m in p.finditer(title)]
        title_hits = list(dict.fromkeys(title_def + title_ctx))

        is_non_it_domain = any(pat.search(title) for pat in self.non_it_auctions)

        keyword_decision = self._keyword_verdict(
            forced, title_hits, title_def, is_non_it_domain, len(it_lots), lot_ratio, len(auction.lots)
        )

        ask_ai = self.engine is not None and self.engine.available_for("classify") and (
            self.use_ai_always or (self.use_ai_when_uncertain and not keyword_decision.certain)
        )
        if not ask_ai:
            return keyword_decision

        # Ask AI to inspect the auction and lot sample
        verdict = self.engine.classify_auction(
            auction, [lot.description for lot in auction.lots[:25]]
        )
        if verdict is None:
            return keyword_decision
        return self._reconcile(keyword_decision, verdict, len(it_lots), lot_ratio)

    def _keyword_verdict(
        self,
        forced: bool,
        title_hits: list[str],
        title_def: list[str],
        is_non_it_domain: bool,
        it_lot_count: int,
        lot_ratio: float,
        total_lots: int,
    ) -> Decision:
        if forced:
            return Decision(
                True, 0.95, "title matched an always-include rule", REGEX,
                certain=True, matched=title_hits,
            )

        # Non-IT domain titles require a verified substantial block of IT lots
        if is_non_it_domain:
            if not title_def and (it_lot_count < 5 or lot_ratio < 0.15):
                return Decision(
                    False, 0.9, "title matches non-IT domain with insufficient IT lots",
                    EXCLUDED, certain=True, matched=title_hits,
                )

        if title_def or len(title_hits) >= 2:
            return Decision(
                True, 0.9, f"title IT equipment: {', '.join(title_hits[:6])}", KEYWORD,
                certain=True, matched=title_hits,
            )

        # A substantial block of IT lots means track it (mixed auction)
        if it_lot_count >= 5 and lot_ratio >= 0.15:
            return Decision(
                True, 0.85,
                f"{it_lot_count} of {total_lots} lots look like IT equipment",
                MIXED if len(title_hits) == 0 else KEYWORD,
                certain=True, matched=title_hits, mixed=len(title_hits) == 0,
            )

        if len(title_hits) == 1:
            return Decision(
                True, 0.65, f"title keyword: {title_hits[0]}", KEYWORD, matched=title_hits
            )

        return Decision(
            False, 0.3,
            "no meaningful IT signal in title or lots", KEYWORD,
            matched=title_hits,
        )

    def _reconcile(
        self,
        keyword_decision: Decision,
        verdict: dict,
        it_lot_count: int,
        lot_ratio: float,
    ) -> Decision:
        """Combine the keyword verdict with the AI's opinion.
        
        AI is authoritative:
        - If AI says is_it: false with confidence >= 0.5, the auction is rejected
          (unless it matched an operator include rule).
        - If AI says is_it: true, the auction is accepted.
        - Only if AI confidence < 0.5 do we fall back to keyword_decision.
        """
        if keyword_decision.source == REGEX:
            return keyword_decision

        ai_is_it = bool(verdict.get("is_it"))
        ai_confidence = float(verdict.get("confidence") or 0.5)
        ai_mixed = bool(verdict.get("mixed"))
        reason = verdict.get("reason") or "AI classification"

        if ai_confidence >= 0.5:
            is_it = ai_is_it or (ai_mixed and self.keep_mixed)
            return Decision(
                is_it,
                ai_confidence,
                f"AI: {reason}",
                MIXED if (ai_mixed and not ai_is_it) else AI,
                certain=True,
                matched=keyword_decision.matched,
                categories=verdict.get("categories") or [],
                mixed=ai_mixed,
            )

        return keyword_decision

    # ==================================================================
    # Lot-level
    # ==================================================================
    def lot_is_it(self, lot: Lot | str) -> bool:
        """Context-aware check on a single lot."""
        if isinstance(lot, Lot) and lot.is_it is not None:
            return bool(lot.is_it)
        text = clean_text(
            f"{lot.description} {lot.short_description} {lot.brand} {lot.model}"
            if isinstance(lot, Lot)
            else str(lot)
        )
        if not text:
            return False

        # 1. Negative patterns disqualify immediately
        for pat in self.negative_lots:
            if pat.search(text):
                return False

        # 2. Definitive IT patterns qualify
        for pat in self.definitive:
            if pat.search(text):
                return True

        # 3. Contextual IT patterns qualify
        for pat in self.contextual:
            if pat.search(text):
                return True

        return False

    def it_lots(self, auction: Auction) -> list[Lot]:
        return [lot for lot in auction.lots if self.lot_is_it(lot)]

    def verify_lots_with_ai(self, lots: Sequence[Lot]) -> list[Lot]:
        """Verify candidate lots using AI if available; returns lots confirmed as IT."""
        if not lots:
            return []
        if self.engine is None or not self.engine.available_for("classify"):
            return [l for l in lots if self.lot_is_it(l)]
        results = self.engine.classify_lots(lots)
        confirmed = []
        for i, lot in enumerate(lots):
            res = results.get(i)
            if res is not None:
                lot.is_it = bool(res.get("is_it"))
                if lot.is_it:
                    confirmed.append(lot)
            elif self.lot_is_it(lot):
                confirmed.append(lot)
        return confirmed

