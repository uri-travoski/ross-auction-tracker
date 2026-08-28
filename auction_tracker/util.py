"""Small shared helpers: time, money, text, hashing.

Everything time-related is timezone-aware. The auction site publishes Perth
local times in its HTML and matching epoch seconds in its JSON feeds, so the
epoch values are always preferred when both are available.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"

# "05:00pm" / "5:00 pm" / "12:30AM"
_TIME_RE = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?", re.I)
# "Friday, 21/08/26" / "21/08/2026" / "21-08-26"
_DATE_RE = re.compile(r"(?P<d>\d{1,2})[/-](?P<mo>\d{1,2})[/-](?P<y>\d{2,4})")

_MONEY_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    """Serialise to ISO 8601 with offset. All DB timestamps use this."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, assuming UTC when no offset is present."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def from_epoch(value: Any, tz: ZoneInfo | None = None) -> datetime | None:
    """Convert epoch seconds (int/str) to an aware datetime."""
    if value in (None, "", 0, "0"):
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    dt = datetime.fromtimestamp(seconds, timezone.utc)
    return dt.astimezone(tz) if tz else dt


def parse_site_datetime(
    time_text: str | None, date_text: str | None, tz: ZoneInfo
) -> datetime | None:
    """Parse the site's split ``<span class="time">``/``<span class="date">``.

    Example: ``("06:30pm ", "Monday, 31/08/26")`` in ``Australia/Perth``.
    Two-digit years are interpreted as 2000+YY.
    """
    if not date_text:
        return None
    dm = _DATE_RE.search(date_text)
    if not dm:
        return None
    day, month = int(dm.group("d")), int(dm.group("mo"))
    year = int(dm.group("y"))
    if year < 100:
        year += 2000

    hour = minute = 0
    if time_text:
        tm = _TIME_RE.search(time_text)
        if tm:
            hour, minute = int(tm.group("h")), int(tm.group("m"))
            ampm = (tm.group("ampm") or "").replace(".", "").lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None


def humanize_delta(delta: timedelta | None) -> str:
    """"3 days 1 hr 47 mins" style countdown, matching the site's phrasing."""
    if delta is None:
        return "—"
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "closed"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hr{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} min{'s' if minutes != 1 else ''}")
    return " ".join(parts) or "under a minute"


# ---------------------------------------------------------------------------
# Numbers and money
# ---------------------------------------------------------------------------


def parse_money(text: Any) -> float | None:
    """Extract a float from ``"$1,234.50"``, ``"575"``, ``"575.00"``, ``""``."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = _MONEY_RE.search(str(text).replace("$", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_int(text: Any, default: int = 0) -> int:
    value = parse_money(text)
    return default if value is None else int(value)


def pct_change(previous: float | None, current: float | None) -> float | None:
    """Percentage increase from ``previous`` to ``current``.

    Returns ``None`` when it is not meaningful. Growth from zero/no-bid is
    reported as ``float("inf")`` so callers can treat "first bid" specially.
    """
    if current is None:
        return None
    if previous is None or previous == 0:
        return float("inf") if current > 0 else None
    return (current - previous) / previous * 100.0


def money(value: float | None, currency: str = "AUD") -> str:
    if value is None:
        return "—"
    symbol = "$" if currency in {"AUD", "USD", "NZD", ""} else f"{currency} "
    return f"{symbol}{value:,.2f}".replace(".00", "")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def clean_text(value: str | None) -> str:
    """Collapse whitespace and normalise entities/unicode for stable diffing."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def slugify(value: str, max_length: int = 80) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_length] or "item"


def truncate(value: str, limit: int = 120) -> str:
    text = clean_text(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Hashing / ids
# ---------------------------------------------------------------------------


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def new_cycle_id() -> str:
    return uuid.uuid4().hex


def stable_key(*parts: Any) -> str:
    """Short deterministic key for caching (AI responses, image names)."""
    return sha256_text("|".join("" if p is None else str(p) for p in parts))[:32]
