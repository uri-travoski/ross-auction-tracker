"""The AI engine: caching, provider fallback, budgets, and the concrete tasks.

Design notes
------------
* Every call goes through :meth:`AIEngine.run`, which handles the cache, the
  provider fallback chain, the per-cycle call budget and the audit log.
* Failures never abort a scan. When ``ai.fail_open`` is true (the default) a
  task that cannot be completed returns ``None`` and the caller falls back to
  deterministic logic — the tracker keeps working with no AI configured at all.
* Results are cached by ``(task, hash(input))``, so re-scanning an unchanged
  auction every six hours costs nothing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..config import Config
from ..logging_setup import get_logger
from ..models import Auction, Change, Lot
from ..store import Store
from ..util import clean_text, money, now_utc, stable_key, to_iso, truncate
from . import prompts
from .providers import AIError, AIResponse, ProviderClient, describe_providers

log = get_logger(__name__)

CLASSIFY = "classify"
EXTRACT_SPECS = "extract_specs"
SUMMARIZE_SCAN = "summarize_scan"
ESTIMATE_PRICE = "estimate_price"
ASK = "ask"


class AIEngine:
    """Config-driven AI access. One instance per cycle or per web request."""

    def __init__(
        self, config: Config, store: Store | None = None, cycle_id: str = ""
    ) -> None:
        self.config = config
        self.store = store
        self.cycle_id = cycle_id
        self.enabled = bool(config.get("ai.enabled", True))
        self.cache_enabled = bool(config.get("ai.cache_enabled", True))
        self.cache_ttl_days = int(config.get("ai.cache_ttl_days", 30))
        self.max_calls = int(config.get("ai.max_calls_per_cycle", 250))
        self.fail_open = bool(config.get("ai.fail_open", True))
        self.calls_made = 0
        self.cache_hits = 0
        self.failures = 0

    # ==================================================================
    # Infrastructure
    # ==================================================================
    def providers(self, task: str) -> list:
        return self.config.providers_for_task(task)

    def available_for(self, task: str) -> bool:
        return bool(self.enabled and self.providers(task))

    @property
    def any_available(self) -> bool:
        return bool(self.enabled and any(p.available for p in self.config.ai_providers))

    def describe(self) -> list[dict[str, Any]]:
        return describe_providers(self.config.ai_providers)

    @property
    def budget_left(self) -> int:
        return max(0, self.max_calls - self.calls_made)

    def run(
        self,
        task: str,
        prompt: str,
        *,
        system: str = prompts.SYSTEM_ANALYST,
        json_mode: bool = True,
        cache_key: str = "",
        auction_id: int | None = None,
        lot_id: int | None = None,
        use_cache: bool = True,
    ) -> AIResponse | None:
        """Execute one AI task, trying each configured provider in turn."""
        if not self.enabled:
            return None
        chain = self.providers(task)
        if not chain:
            log.debug("no AI provider available", extra={"task": task})
            return None

        key = cache_key or stable_key(prompt)
        if use_cache and self.cache_enabled and self.store is not None:
            cached = self.store.ai_cache_get(task, key, self.cache_ttl_days)
            if cached is not None:
                self.cache_hits += 1
                self.store.log_ai_call(
                    task=task,
                    cycle_id=self.cycle_id,
                    auction_id=auction_id,
                    lot_id=lot_id,
                    cached=True,
                )
                return AIResponse(text=cached, provider="cache", model="cache")

        if self.budget_left <= 0:
            log.warning(
                "AI call budget exhausted for this cycle",
                extra={"task": task, "max_calls": self.max_calls},
            )
            return None

        last_error = ""
        for provider in chain:
            client = ProviderClient(provider)
            try:
                response = client.complete(prompt, system=system, json_mode=json_mode)
            except AIError as exc:
                last_error = str(exc)
                self.failures += 1
                log.warning(
                    "AI provider failed, trying next",
                    extra={
                        "task": task,
                        "provider": provider.name,
                        "model": provider.model,
                        "error": truncate(last_error, 200),
                    },
                )
                if self.store is not None:
                    self.store.log_ai_call(
                        task=task,
                        provider=provider.name,
                        model=provider.model,
                        cycle_id=self.cycle_id,
                        auction_id=auction_id,
                        lot_id=lot_id,
                        ok=False,
                        error=last_error,
                    )
                continue

            self.calls_made += 1
            if self.store is not None:
                self.store.log_ai_call(
                    task=task,
                    provider=provider.name,
                    model=provider.model,
                    cycle_id=self.cycle_id,
                    auction_id=auction_id,
                    lot_id=lot_id,
                    duration_ms=response.duration_ms,
                )
                if self.cache_enabled and use_cache:
                    self.store.ai_cache_put(
                        task, key, response.text, provider.name, provider.model
                    )
            log.debug(
                "AI call ok",
                extra={
                    "task": task,
                    "provider": provider.name,
                    "ms": response.duration_ms,
                },
            )
            return response

        log.error(
            "all AI providers failed",
            extra={"task": task, "error": truncate(last_error, 300)},
        )
        if not self.fail_open:
            raise AIError(f"all providers failed for task {task}: {last_error}")
        return None

    # ==================================================================
    # Task: is this auction IT-related?
    # ==================================================================
    def classify_auction(
        self, auction: Auction, lot_sample: Sequence[str] = ()
    ) -> dict[str, Any] | None:
        """Return ``{is_it, confidence, mixed, categories, reason}`` or None."""
        sample = "\n".join(f"- {truncate(s, 140)}" for s in list(lot_sample)[:25])
        prompt = prompts.CLASSIFY_AUCTION.format(
            title=auction.title or "(unknown)",
            url=auction.url,
            location=auction.location or "(unknown)",
            status=auction.status,
            description=truncate(auction.description, 2500) or "(none captured)",
            lot_sample=sample or "(none)",
        )
        response = self.run(
            CLASSIFY,
            prompt,
            cache_key=stable_key(auction.url, auction.title, len(lot_sample), sample[:500]),
            auction_id=auction.id,
        )
        if response is None:
            return None
        data = response.json()
        if not isinstance(data, dict) or "is_it" not in data:
            log.warning(
                "unparseable classify reply",
                extra={"url": auction.url, "reply": truncate(response.text, 200)},
            )
            return None
        return {
            "is_it": bool(data.get("is_it")),
            "confidence": _clamp(data.get("confidence"), 0.0, 1.0, 0.5),
            "mixed": bool(data.get("mixed")),
            "categories": [str(c) for c in (data.get("categories") or [])][:12],
            "reason": truncate(str(data.get("reason") or ""), 300),
            "provider": response.provider,
            "model": response.model,
        }

    # ==================================================================
    # Task: extract brand/model/specs for lots
    # ==================================================================
    def extract_specs(self, lots: Sequence[Lot]) -> dict[int, dict[str, Any]]:
        """Map ``lot index in the input sequence -> extracted attributes``.

        Lots are batched to keep the number of requests small.
        """
        if not lots:
            return {}
        results: dict[int, dict[str, Any]] = {}
        batch_size = 25
        for start in range(0, len(lots), batch_size):
            batch = list(lots)[start : start + batch_size]
            lines = "\n".join(
                f"{i} | qty {lot.quantity} | {truncate(lot.description, 220)}"
                for i, lot in enumerate(batch)
            )
            response = self.run(
                EXTRACT_SPECS,
                prompts.EXTRACT_SPECS.format(lots=lines),
                cache_key=stable_key("specs", lines),
            )
            if response is None:
                break
            data = response.json() or {}
            entries = data.get("lots") if isinstance(data, dict) else data
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    index = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if not 0 <= index < len(batch):
                    continue
                specs = entry.get("specs")
                results[start + index] = {
                    "is_it": bool(entry.get("is_it", True)),
                    "category": truncate(str(entry.get("category") or ""), 40),
                    "brand": truncate(str(entry.get("brand") or ""), 60),
                    "model": truncate(str(entry.get("model") or ""), 120),
                    "specs": {
                        str(k): truncate(str(v), 120)
                        for k, v in (specs or {}).items()
                        if v not in (None, "", "unknown")
                    }
                    if isinstance(specs, dict)
                    else {},
                }
        return results

    # ==================================================================
    # Task: narrative summary of one scan
    # ==================================================================
    def summarize_scan(
        self,
        *,
        cycle_type: str,
        auctions: Sequence[Auction],
        changes: Sequence[Change],
        lot_count: int,
    ) -> dict[str, Any] | None:
        change_lines = "\n".join(
            f"- {truncate(c.describe(), 160)}" for c in list(changes)[:60]
        ) or "- (no changes detected)"
        state_lines = []
        for auction in list(auctions)[:25]:
            state_lines.append(
                f"- {truncate(auction.title, 70)} | closes {to_iso(auction.end_at)}"
                f" | {auction.countdown()} left | {len(auction.lots)} lots"
                f" | {auction.total_bids} bids"
                f" | top {money(max((l.current_bid or 0) for l in auction.lots) if auction.lots else 0)}"
                f" | {auction.lots_without_bids} lots with no bids"
            )
        response = self.run(
            SUMMARIZE_SCAN,
            prompts.SUMMARIZE_SCAN.format(
                cycle_type=cycle_type,
                timestamp=to_iso(now_utc()),
                auction_count=len(auctions),
                lot_count=lot_count,
                change_count=len(changes),
                changes=change_lines,
                auction_state="\n".join(state_lines) or "- (none)",
            ),
            # Each scan is a new situation; caching would defeat the point.
            use_cache=False,
        )
        if response is None:
            return None
        data = response.json()
        if not isinstance(data, dict):
            return {"headline": truncate(response.text, 120), "summary": response.text}
        return {
            "headline": truncate(str(data.get("headline") or ""), 200),
            "summary": str(data.get("summary") or ""),
            "momentum": str(data.get("momentum") or ""),
            "watch_items": [str(w) for w in (data.get("watch_items") or [])][:8],
            "provider": response.provider,
            "model": response.model,
        }

    # ==================================================================
    # Task: price estimation
    # ==================================================================
    def estimate_price(
        self,
        *,
        question: str,
        target: str,
        comparables: Sequence[dict[str, Any]],
    ) -> dict[str, Any] | None:
        rows = []
        for c in list(comparables)[:60]:
            final = c.get("final_bid") if c.get("final_bid") else c.get("current_bid")
            rows.append(
                " | ".join(
                    [
                        f"lot {c.get('lot_number', '?')}",
                        truncate(str(c.get("description") or ""), 150),
                        f"qty {c.get('quantity', 1)}",
                        f"final {money(final)}",
                        f"{c.get('bid_count', 0)} bids",
                        truncate(str(c.get("auction_title") or ""), 60),
                        str(c.get("end_at") or "")[:10],
                    ]
                )
            )
        response = self.run(
            ESTIMATE_PRICE,
            prompts.ESTIMATE_PRICE.format(
                question=question,
                target=target or question,
                comparables="\n".join(rows) or "(no historical sales matched)",
            ),
            system=prompts.SYSTEM_VALUER,
            use_cache=False,
        )
        if response is None:
            return None
        data = response.json()
        if not isinstance(data, dict):
            return {"reasoning": response.text, "max_bid_aud": None}
        fair = data.get("fair_range_aud")
        if isinstance(fair, list) and len(fair) == 2:
            fair = [_number(fair[0]), _number(fair[1])]
        else:
            fair = None
        return {
            "max_bid_aud": _number(data.get("max_bid_aud")),
            "fair_range_aud": fair,
            "confidence": str(data.get("confidence") or "low"),
            "comparables_used": [str(x) for x in (data.get("comparables_used") or [])][:10],
            "reasoning": str(data.get("reasoning") or ""),
            "caveats": [str(x) for x in (data.get("caveats") or [])][:6],
            "provider": response.provider,
            "model": response.model,
        }

    def ask(self, question: str, context: str) -> dict[str, Any] | None:
        response = self.run(
            ESTIMATE_PRICE,  # shares the reasoning-grade provider chain
            prompts.ASK_GENERAL.format(question=question, context=truncate(context, 12000)),
            system=prompts.SYSTEM_VALUER,
            use_cache=False,
        )
        if response is None:
            return None
        data = response.json()
        if not isinstance(data, dict):
            return {"answer": response.text, "confidence": "low"}
        return {
            "answer": str(data.get("answer") or ""),
            "confidence": str(data.get("confidence") or "low"),
            "data_gaps": [str(x) for x in (data.get("data_gaps") or [])][:5],
            "provider": response.provider,
            "model": response.model,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _number(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def build_comparable_terms(text: str, brand: str = "", model: str = "") -> list[str]:
    """Search terms for finding historical comparables for an item.

    Prefers the brand/model pair, then falls back to the longest meaningful
    words in the description.
    """
    terms: list[str] = []
    if brand and model:
        terms.append(f"{brand} {model}".strip())
    if model:
        terms.append(model)
    if brand:
        terms.append(brand)
    stopwords = {
        "with", "and", "the", "for", "inch", "new", "used", "x", "of", "in",
        "assorted", "various", "quantity", "lot", "box", "pallet",
    }
    words = [
        w
        for w in clean_text(text).lower().replace("/", " ").split()
        if len(w) > 3 and w not in stopwords and not w.isdigit()
    ]
    for word in words[:6]:
        if word not in [t.lower() for t in terms]:
            terms.append(word)
    return terms[:8]


def context_for_question(store: Store, question: str, limit: int = 40) -> str:
    """Assemble a compact data context for a free-form operator question."""
    terms = build_comparable_terms(question)
    rows = store.comparable_lots(terms=terms, limit=limit, sold_only=False)
    if not rows:
        rows = store.comparable_lots(terms=[question], limit=limit, sold_only=False)
    lines = [
        " | ".join(
            [
                f"lot {r.get('lot_number', '?')}",
                truncate(str(r.get("description") or ""), 140),
                f"qty {r.get('quantity', 1)}",
                f"current {money(r.get('current_bid'))}",
                f"final {money(r.get('final_bid'))}",
                f"{r.get('bid_count', 0)} bids",
                truncate(str(r.get("auction_title") or ""), 50),
                str(r.get("end_at") or "")[:10],
            ]
        )
        for r in rows
    ]
    stats = store.stats()
    header = (
        f"Dataset: {stats['auctions_tracked']} tracked IT auctions, "
        f"{stats['lots']} lots, {stats['lots_sold']} with recorded final prices."
    )
    return header + "\n" + ("\n".join(lines) if lines else "(no matching lots)")
