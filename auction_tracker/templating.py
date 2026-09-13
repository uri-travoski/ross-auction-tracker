"""Shared Jinja2 environment for the web UI and the exported HTML reports.

Both use the same templates and filters, so a static export looks exactly like
the live page.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .util import clean_text, humanize_delta, money, now_utc, truncate

TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"
STATIC_DIR = Path(__file__).parent / "web" / "static"

# Colour coding used for highlighted changes, matching the email styling.
CHANGE_STYLES: dict[str, tuple[str, str]] = {
    "NEW_LOT": ("new", "New lot"),
    "REMOVED": ("removed", "Removed"),
    "BID_CHANGED": ("bid", "Bid changed"),
    "BID_COUNT_CHANGED": ("bid", "Bid count"),
    "DESCRIPTION_CHANGED": ("desc", "Description edited"),
    "IMAGE_CHANGED": ("desc", "Photos changed"),
    "STATUS_CHANGED": ("status", "Status"),
    "QUANTITY_CHANGED": ("desc", "Quantity"),
    "RESERVE_MET": ("reserve", "Reserve met"),
    "CLOSE_TIME_CHANGED": ("status", "Close time"),
}


def _local(value: Any, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, datetime):
        from .util import from_iso

        value = from_iso(str(value)) if value else None
    if value is None:
        return None
    return value.astimezone(tz)


def build_environment(config: Config) -> Environment:
    tz = config.timezone
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def fmt_datetime(value: Any, pattern: str = "%a %d %b %Y, %-I:%M%p") -> str:
        local = _local(value, tz)
        if local is None:
            return "—"
        try:
            return local.strftime(pattern).replace("AM", "am").replace("PM", "pm")
        except ValueError:  # platforms without %-I
            return local.strftime("%a %d %b %Y, %I:%M%p").lstrip("0")

    def fmt_date(value: Any) -> str:
        local = _local(value, tz)
        return local.strftime("%d/%m/%Y") if local else "—"

    def fmt_short(value: Any) -> str:
        local = _local(value, tz)
        if local is None:
            return "—"
        return local.strftime("%d/%m/%y %H:%M")

    def countdown(value: Any) -> str:
        local = _local(value, tz)
        if local is None:
            return "—"
        return humanize_delta(local - now_utc().astimezone(tz))

    def is_soon(value: Any, minutes: int = 180) -> bool:
        local = _local(value, tz)
        if local is None:
            return False
        delta = local - now_utc().astimezone(tz)
        return timedelta(0) < delta <= timedelta(minutes=minutes)

    def change_class(change_type: str) -> str:
        return CHANGE_STYLES.get(str(change_type), ("other", ""))[0]

    def change_label(change_type: str) -> str:
        return CHANGE_STYLES.get(str(change_type), ("other", str(change_type)))[1]

    def query(base: dict[str, Any], **overrides: Any) -> str:
        merged = {**base, **overrides}
        clean = {
            k: v
            for k, v in merged.items()
            if v not in (None, "", [], "all") or k in {"scope"}
        }
        return "?" + urlencode(clean, doseq=True) if clean else ""

    def pct(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        return f"{number:+.1f}%"

    def safe_money(value: Any, currency: str = "AUD") -> str:
        if value is None or value == "":
            return "—"
        try:
            return money(float(value), currency)
        except (TypeError, ValueError):
            return str(value)

    def fmt_change_val(value: Any, change_type: str = "") -> str:
        if value is None or value == "":
            return "—"
        s = str(value)
        if "bid" in str(change_type).lower():
            try:
                return money(float(s))
            except (TypeError, ValueError):
                pass
        return truncate(s, 160)

    env.filters.update(
        {
            "money": safe_money,
            "fmt_change_val": fmt_change_val,
            "dt": fmt_datetime,
            "date": fmt_date,
            "short": fmt_short,
            "countdown": countdown,
            "truncate_text": truncate,
            "clean": clean_text,
            "pct": pct,
            "change_class": change_class,
            "change_label": change_label,
        }
    )
    env.tests["soon"] = is_soon
    env.globals.update(
        {
            "site_title": str(config.get("web.title", "Ross Auction IT Tracker")),
            "page_size_options": config.get("web.page_size_options", [25, 50, 100]),
            "default_page_size": int(config.get("web.page_size", 100)),
            "timezone_name": str(config.get("site.timezone", "UTC")),
            "query": query,
            "now": now_utc,
            "final_stretch_minutes": int(
                config.get("schedule.final_stretch_minutes", 180)
            ),
        }
    )
    return env


def inline_css() -> str:
    """The stylesheet, for embedding in standalone exported reports."""
    path = STATIC_DIR / "style.css"
    return path.read_text(encoding="utf-8") if path.is_file() else ""
