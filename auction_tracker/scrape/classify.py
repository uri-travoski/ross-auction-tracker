"""The IT filter: a cheap keyword pass, then AI confirmation.

Two passes, as the requirements demand:

1. **Index time** — title, location and card text only. Cheap, catches the
   obvious cases, and lets the crawler skip detail pages it does not need.
2. **Detail time** — full description plus real lot text. This is where a
   "Medical Equipment" auction that is actually full of monitors and laptops
   gets caught, and where a false positive from a word like "switch" or "rack"
   in a farm-machinery auction gets dropped.

The AI has a say on every scan (``filter.use_ai_always``), because keyword
lists cannot know that a Brother HL-2270DW is a printer or that a "Gateway
notebook" is a laptop. When no AI is configured, or every provider fails, the
keyword verdict stands and the tracker keeps running.
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


def _compiled(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    out = []
    for pattern in patterns or []:
        try:
            out.append(re.compile(pattern))
        except re.error as exc:
            log.warning("bad filter regex %r: %s", pattern, exc)
    return out


def _keyword_pattern(keywords: Sequence[str]) -> re.Pattern[str] | None:
    """One word-boundary alternation for all keywords.

    Word boundaries matter: without them "pc" matches "specifications" and
    "hp" matches "graphite".
    """
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
    """Reusable classifier; compile the patterns once per cycle."""

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

        hits = matched_keywords(title, self.keywords)
        if len(hits) >= 2:
            return Decision(
                True, 0.85, f"title keywords: {', '.join(hits[:6])}", KEYWORD,
                certain=False, matched=hits,
            )
        if len(hits) == 1:
            return Decision(
                True, 0.6, f"title keyword: {hits[0]}", KEYWORD, matched=hits
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

        lot_text = " \n".join(
            f"{lot.description} {lot.short_description}" for lot in auction.lots[:400]
        )
        body = clean_text(f"{auction.description} {auction.location} {lot_text}")

        title_hits = matched_keywords(title, self.keywords)
        body_hits = matched_keywords(body, self.keywords)
        forced = any(pattern.search(title) for pattern in self.include)

        # How many lots individually look like IT? This is the signal that
        # distinguishes a genuinely mixed auction from an incidental mention.
        it_lots = [lot for lot in auction.lots if self.lot_is_it(lot)]
        lot_ratio = (len(it_lots) / len(auction.lots)) if auction.lots else 0.0

        keyword_decision = self._keyword_verdict(
            forced, title_hits, body_hits, len(it_lots), lot_ratio
        )

        ask_ai = self.engine is not None and self.engine.available_for("classify") and (
            self.use_ai_always or (self.use_ai_when_uncertain and not keyword_decision.certain)
        )
        if not ask_ai:
            return keyword_decision

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
        body_hits: list[str],
        it_lot_count: int,
        lot_ratio: float,
    ) -> Decision:
        matched = title_hits + [h for h in body_hits if h not in title_hits]
        if forced:
            return Decision(
                True, 0.95, "title matched an always-include rule", REGEX,
                certain=True, matched=matched,
            )
        if len(title_hits) >= 2:
            return Decision(
                True, 0.9, f"title keywords: {', '.join(title_hits[:6])}", KEYWORD,
                certain=True, matched=matched,
            )
        # A substantial block of IT lots means track it, whatever the title says.
        if it_lot_count >= 5 and lot_ratio >= 0.2:
            return Decision(
                True, 0.85,
                f"{it_lot_count} of {int(it_lot_count / lot_ratio) if lot_ratio else 0}"
                " lots look like IT equipment",
                MIXED if len(title_hits) == 0 else KEYWORD,
                certain=True, matched=matched, mixed=len(title_hits) == 0,
            )
        if len(title_hits) == 1:
            return Decision(
                True, 0.65, f"title keyword: {title_hits[0]}", KEYWORD, matched=matched
            )
        if len(body_hits) >= 4:
            return Decision(
                True, 0.5,
                f"description keywords: {', '.join(body_hits[:6])}", KEYWORD,
                matched=matched,
            )
        return Decision(
            False, 0.3,
            "no meaningful IT signal in title, description or lots", KEYWORD,
            matched=matched,
        )

    def _reconcile(
        self,
        keyword_decision: Decision,
        verdict: dict,
        it_lot_count: int,
        lot_ratio: float,
    ) -> Decision:
        """Combine the keyword verdict with the AI's opinion."""
        ai_is_it = bool(verdict.get("is_it"))
        ai_confidence = float(verdict.get("confidence") or 0.5)
        ai_mixed = bool(verdict.get("mixed"))
        reason = verdict.get("reason") or "AI classification"

        # A confident AI wins; the keyword list cannot reason about products.
        if ai_confidence >= 0.6:
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

        # Low-confidence AI: keep it only as a tiebreaker.
        if keyword_decision.certain:
            return keyword_decision
        if ai_is_it != keyword_decision.is_it:
            # Disagreement with nobody confident: keep the item, since a false
            # positive costs one wasted scrape while a false negative loses the
            # auction entirely.
            return Decision(
                True,
                0.5,
                f"uncertain (keywords said {keyword_decision.is_it}, AI said "
                f"{ai_is_it}); tracking to be safe",
                AI,
                matched=keyword_decision.matched,
                categories=verdict.get("categories") or [],
                mixed=ai_mixed,
            )
        return keyword_decision

    # ==================================================================
    # Lot-level
    # ==================================================================
    def lot_is_it(self, lot: Lot) -> bool:
        """Keyword check on a single lot (used for the mixed-auction rule)."""
        if lot.is_it is not None:
            return bool(lot.is_it)
        text = clean_text(f"{lot.description} {lot.short_description} {lot.brand} {lot.model}")
        return len(matched_keywords(text, self.keywords)) >= 1

    def it_lots(self, auction: Auction) -> list[Lot]:
        return [lot for lot in auction.lots if self.lot_is_it(lot)]
