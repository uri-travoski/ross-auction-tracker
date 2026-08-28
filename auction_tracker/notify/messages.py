"""Building the email bodies.

Pure functions: given the data, return a :class:`Message` with a subject, an
HTML body and a plain-text alternative. No I/O, so every message shape is
unit-testable. The styling deliberately mirrors the auction house's own look
(dark red headers, light grey rules) so the alerts feel of a piece with the
report UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from ..models import Auction, Change, Lot
from ..util import clean_text, humanize_delta, money, now_utc, truncate

BRAND_RED = "#a91b23"
BRAND_DARK = "#2b2b2b"
GREY = "#6b6b6b"
LIGHT = "#f5f5f5"


@dataclass
class Message:
    subject: str
    html: str
    text: str
    event_type: str = "generic"
    auction_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_time(value: datetime | None, tz: ZoneInfo | None = None) -> str:
    if value is None:
        return "—"
    local = value.astimezone(tz) if tz else value
    return local.strftime("%a %d %b %Y, %I:%M%p").replace(" 0", " ").replace("AM", "am").replace("PM", "pm")


def _shell(title: str, body: str, footer: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:{LIGHT};
 font-family:Lato,Helvetica,Arial,sans-serif;color:{BRAND_DARK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
 style="background:{LIGHT};padding:20px 0;"><tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
 style="width:640px;max-width:96%;background:#ffffff;border:1px solid #e0e0e0;">
  <tr><td style="background:{BRAND_RED};padding:14px 20px;">
    <span style="color:#fff;font-size:18px;font-weight:700;
     letter-spacing:.3px;">{escape(title)}</span>
  </td></tr>
  <tr><td style="padding:20px;font-size:14px;line-height:1.5;">{body}</td></tr>
  <tr><td style="background:{LIGHT};padding:12px 20px;border-top:1px solid #e0e0e0;
   color:{GREY};font-size:11px;">
    {footer or "Ross Auction IT Tracker — automated monitoring of auctions.com.au."}
  </td></tr>
</table></td></tr></table></body></html>"""


def _button(url: str, label: str) -> str:
    return (
        f'<a href="{escape(url)}" style="display:inline-block;background:{BRAND_RED};'
        f'color:#fff;text-decoration:none;padding:9px 16px;font-weight:700;'
        f'font-size:13px;">{escape(label)}</a>'
    )


def _kv_table(rows: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><th align="left" style="padding:4px 12px 4px 0;color:{GREY};'
        f'font-weight:400;white-space:nowrap;vertical-align:top;">{escape(k)}</th>'
        f'<td style="padding:4px 0;">{v}</td></tr>'
        for k, v in rows
    )
    return f'<table role="presentation" style="font-size:14px;">{cells}</table>'


def _lot_table(
    lots: Sequence[Lot], tz: ZoneInfo | None = None, notes: dict[str, str] | None = None
) -> str:
    if not lots:
        return f'<p style="color:{GREY};">No lots to show.</p>'
    notes = notes or {}
    header = (
        f'<tr style="background:{LIGHT};">'
        f'<th align="left" style="padding:6px 8px;font-size:12px;">Lot</th>'
        f'<th align="left" style="padding:6px 8px;font-size:12px;">Description</th>'
        f'<th align="right" style="padding:6px 8px;font-size:12px;">Bid</th>'
        f'<th align="right" style="padding:6px 8px;font-size:12px;">Bids</th></tr>'
    )
    rows = []
    for lot in lots:
        note = notes.get(lot.lot_number, "")
        note_html = (
            f'<div style="color:{BRAND_RED};font-size:11px;">{escape(note)}</div>'
            if note
            else ""
        )
        thumb = ""
        if lot.thumbnail_url:
            thumb = (
                f'<img src="{escape(lot.thumbnail_url)}" width="60" '
                f'style="display:block;border:0;margin-right:8px;" alt="">'
            )
        rows.append(
            "<tr style='border-top:1px solid #eee;'>"
            f"<td style='padding:6px 8px;vertical-align:top;white-space:nowrap;'>"
            f"<strong>{escape(lot.lot_number)}</strong></td>"
            f"<td style='padding:6px 8px;vertical-align:top;'>"
            f"<table role='presentation'><tr><td>{thumb}</td>"
            f"<td style='font-size:13px;'>{escape(truncate(lot.description, 130))}"
            f"{note_html}</td></tr></table></td>"
            f"<td align='right' style='padding:6px 8px;vertical-align:top;"
            f"white-space:nowrap;'><strong>{escape(money(lot.current_bid))}</strong></td>"
            f"<td align='right' style='padding:6px 8px;vertical-align:top;'>"
            f"{lot.bid_count}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px;">'
        + header
        + "".join(rows)
        + "</table>"
    )


def _text_lots(lots: Sequence[Lot], notes: dict[str, str] | None = None) -> str:
    notes = notes or {}
    lines = []
    for lot in lots:
        note = notes.get(lot.lot_number, "")
        suffix = f"  [{note}]" if note else ""
        lines.append(
            f"  Lot {lot.lot_number}: {truncate(lot.description, 90)} — "
            f"{money(lot.current_bid)} ({lot.bid_count} bids){suffix}"
        )
    return "\n".join(lines) or "  (none)"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def new_auction_message(
    auction: Auction, tz: ZoneInfo | None = None, report_url: str = ""
) -> Message:
    subject = f"New IT auction: {clean_text(auction.title)}"
    rows = [
        ("Auction", f'<a href="{escape(auction.url)}">{escape(auction.title)}</a>'),
        ("Opens", escape(fmt_time(auction.start_at, tz))),
        ("Closes", escape(fmt_time(auction.end_at, tz))),
        ("Location", escape(auction.location or "—")),
        ("Lots", str(auction.lot_count or len(auction.lots))),
        ("Why tracked", escape(auction.it_reason or "matched IT filter")),
    ]
    thumb = (
        f'<p><img src="{escape(auction.thumbnail_url)}" width="300" '
        f'style="border:0;" alt=""></p>'
        if auction.thumbnail_url
        else ""
    )
    top = auction.top_lots(5)
    body = (
        f"<p>A new IT-related auction has been added to tracking.</p>"
        f"{_kv_table(rows)}{thumb}"
        f"{'<h3 style=font-size:15px;>Highest lots so far</h3>' + _lot_table(top, tz) if top else ''}"
        f"<p style='margin-top:18px;'>{_button(auction.url, 'View auction')}"
        f"{'&nbsp;' + _button(report_url, 'Open tracker') if report_url else ''}</p>"
    )
    text = (
        f"New IT auction tracked\n\n{auction.title}\n{auction.url}\n\n"
        f"Opens: {fmt_time(auction.start_at, tz)}\n"
        f"Closes: {fmt_time(auction.end_at, tz)}\n"
        f"Location: {auction.location}\nLots: {auction.lot_count}\n"
        f"Why tracked: {auction.it_reason}\n"
    )
    return Message(subject, _shell("New IT auction", body), text, "new_auction", auction.id)


def reminder_message(
    auction: Auction,
    lead_minutes: int,
    *,
    hot_lots: Sequence[tuple[Lot, float, int]] = (),
    tz: ZoneInfo | None = None,
    report_url: str = "",
) -> Message:
    """Pre-close reminder (sent at 3 hours and again at 30 minutes)."""
    lead_label = (
        f"{lead_minutes // 60} hour{'s' if lead_minutes // 60 != 1 else ''}"
        if lead_minutes >= 60 and lead_minutes % 60 == 0
        else f"{lead_minutes} minutes"
    )
    subject = (
        f"Closing in {lead_label}: {clean_text(auction.title)} "
        f"({auction.total_bids} bids)"
    )
    top = auction.top_lots(3)
    hot_notes = {
        lot.lot_number: f"+{money(delta)} and {count} new bids in the last hour"
        for lot, delta, count in hot_lots
    }
    no_bids = auction.lots_without_bids
    at_reserve = sum(1 for lot in auction.lots if lot.met_reserve)
    summary = (
        f"{len(auction.lots)} lots, {auction.total_bids} bids in total, "
        f"{money(auction.total_current_bids)} on the table. "
        f"{at_reserve} lot{'s' if at_reserve != 1 else ''} at or above reserve, "
        f"{no_bids} with no bids yet."
    )
    rows = [
        ("Auction", f'<a href="{escape(auction.url)}">{escape(auction.title)}</a>'),
        ("Closes", escape(fmt_time(auction.end_at, tz))),
        ("Time left", escape(auction.countdown())),
        ("Lots", str(len(auction.lots))),
        ("Total bids", str(auction.total_bids)),
    ]
    if auction.was_extended:
        rows.append(
            ("Note", f'<span style="color:{BRAND_RED};">Extended from '
             f"{escape(fmt_time(auction.end_at_original, tz))}</span>")
        )
    hot_section = ""
    if hot_lots:
        hot_section = (
            '<h3 style="font-size:15px;margin-bottom:6px;">Hot in the last hour</h3>'
            + _lot_table([lot for lot, _, _ in hot_lots], tz, hot_notes)
        )
    body = (
        f'<p style="font-size:15px;"><strong>This auction closes in '
        f"{escape(lead_label)}.</strong></p>"
        f"{_kv_table(rows)}"
        f'<p style="margin-top:14px;color:{GREY};">{escape(summary)}</p>'
        f'<h3 style="font-size:15px;margin-bottom:6px;">Top lots by current bid</h3>'
        f"{_lot_table(top, tz)}"
        f"{hot_section}"
        f"<p style='margin-top:18px;'>{_button(auction.url, 'Bid / view auction')}"
        f"{'&nbsp;' + _button(report_url, 'Open tracker') if report_url else ''}</p>"
    )
    text = (
        f"AUCTION CLOSING IN {lead_label.upper()}\n\n{auction.title}\n{auction.url}\n\n"
        f"Closes: {fmt_time(auction.end_at, tz)} ({auction.countdown()} left)\n"
        f"{summary}\n\nTop lots by current bid:\n{_text_lots(top)}\n"
        + (
            f"\nHot in the last hour:\n{_text_lots([l for l, _, _ in hot_lots], hot_notes)}\n"
            if hot_lots
            else ""
        )
    )
    return Message(
        subject,
        _shell(f"Closing in {lead_label}", body),
        text,
        "reminder",
        auction.id,
        {"lead_minutes": lead_minutes},
    )


def final_stretch_message(
    auction: Auction, tz: ZoneInfo | None = None, report_url: str = ""
) -> Message:
    subject = f"Final stretch: {clean_text(auction.title)} closes {fmt_time(auction.end_at, tz)}"
    body = (
        f"<p>This auction has entered its final hours; monitoring has switched to "
        f"30-minute polling.</p>"
        + _kv_table(
            [
                ("Auction", f'<a href="{escape(auction.url)}">{escape(auction.title)}</a>'),
                ("Closes", escape(fmt_time(auction.end_at, tz))),
                ("Time left", escape(auction.countdown())),
                ("Lots", str(len(auction.lots))),
                ("Total bids", str(auction.total_bids)),
            ]
        )
        + _lot_table(auction.top_lots(5), tz)
        + f"<p style='margin-top:18px;'>{_button(auction.url, 'View auction')}</p>"
    )
    text = (
        f"Final stretch: {auction.title}\n{auction.url}\n"
        f"Closes {fmt_time(auction.end_at, tz)} ({auction.countdown()} left)\n"
        f"{len(auction.lots)} lots, {auction.total_bids} bids\n"
    )
    return Message(
        subject, _shell("Final stretch", body), text, "final_stretch", auction.id
    )


def extension_message(
    auction: Auction,
    old_end: datetime | None,
    new_end: datetime | None,
    tz: ZoneInfo | None = None,
) -> Message:
    subject = f"Auction extended: {clean_text(auction.title)}"
    body = (
        "<p>The closing time for this auction has moved.</p>"
        + _kv_table(
            [
                ("Auction", f'<a href="{escape(auction.url)}">{escape(auction.title)}</a>'),
                ("Was closing", escape(fmt_time(old_end, tz))),
                ("Now closing", escape(fmt_time(new_end, tz))),
                ("Time left", escape(auction.countdown())),
            ]
        )
        + "<p style='color:#6b6b6b;'>Reminders have been rescheduled for the new "
        "closing time.</p>"
    )
    text = (
        f"Auction extended: {auction.title}\n{auction.url}\n"
        f"Was: {fmt_time(old_end, tz)}\nNow: {fmt_time(new_end, tz)}\n"
    )
    return Message(subject, _shell("Auction extended", body), text, "extension", auction.id)


def changes_message(
    auction: Auction,
    changes: Sequence[Change],
    tz: ZoneInfo | None = None,
    report_url: str = "",
) -> Message:
    count = len(changes)
    subject = (
        f"{count} significant change{'s' if count != 1 else ''}: "
        f"{clean_text(auction.title)}"
    )
    items = "".join(
        f'<li style="margin-bottom:4px;">{escape(c.describe())}'
        + (f' <span style="color:{GREY};">({escape(c.note)})</span>' if c.note else "")
        + "</li>"
        for c in list(changes)[:40]
    )
    more = (
        f'<p style="color:{GREY};">…and {count - 40} more.</p>' if count > 40 else ""
    )
    body = (
        _kv_table(
            [
                ("Auction", f'<a href="{escape(auction.url)}">{escape(auction.title)}</a>'),
                ("Closes", escape(fmt_time(auction.end_at, tz))),
                ("Time left", escape(auction.countdown())),
            ]
        )
        + f'<h3 style="font-size:15px;">What changed</h3><ul style="padding-left:18px;">{items}</ul>{more}'
        + f"<p style='margin-top:18px;'>{_button(auction.url, 'View auction')}"
        + f"{'&nbsp;' + _button(report_url, 'Open tracker') if report_url else ''}</p>"
    )
    text = (
        f"{count} significant changes: {auction.title}\n{auction.url}\n\n"
        + "\n".join(f"- {c.describe()}" for c in list(changes)[:40])
        + "\n"
    )
    return Message(
        subject, _shell("Significant changes", body), text, "changes", auction.id
    )


def finalized_message(
    auction: Auction,
    *,
    sold_lots: int = 0,
    total_value: float = 0.0,
    report_url: str = "",
    tz: ZoneInfo | None = None,
) -> Message:
    subject = (
        f"Final results: {clean_text(auction.title)} — "
        f"{money(total_value)} across {sold_lots} lots"
    )
    top = sorted(
        auction.lots, key=lambda l: (l.final_bid or l.current_bid or 0), reverse=True
    )[:10]
    body = (
        "<p>This auction has closed and its final results are recorded.</p>"
        + _kv_table(
            [
                ("Auction", f'<a href="{escape(auction.url)}">{escape(auction.title)}</a>'),
                ("Closed", escape(fmt_time(auction.end_at, tz))),
                ("Lots", str(len(auction.lots))),
                ("Lots with bids", str(sold_lots)),
                ("Total of final bids", escape(money(total_value))),
                ("Total bids placed", str(auction.total_bids)),
            ]
        )
        + '<h3 style="font-size:15px;">Highest results</h3>'
        + _lot_table(top, tz)
        + (f"<p style='margin-top:18px;'>{_button(report_url, 'Open full report')}</p>" if report_url else "")
    )
    text = (
        f"Final results: {auction.title}\n{auction.url}\n\n"
        f"{sold_lots} lots with bids, {money(total_value)} total.\n\n"
        f"Highest results:\n{_text_lots(top)}\n"
    )
    return Message(subject, _shell("Auction finalised", body), text, "finalized", auction.id)


def digest_message(
    lines: Sequence[str], *, headline: str = "", summary: str = "", report_url: str = ""
) -> Message:
    subject = headline or f"Auction tracker digest — {now_utc():%d %b %Y %H:%M} UTC"
    items = "".join(f"<li>{escape(line)}</li>" for line in lines)
    body = (
        (f"<p><strong>{escape(headline)}</strong></p>" if headline else "")
        + (f"<p>{escape(summary)}</p>" if summary else "")
        + f'<ul style="padding-left:18px;">{items}</ul>'
        + (f"<p>{_button(report_url, 'Open tracker')}</p>" if report_url else "")
    )
    text = (
        (headline + "\n\n" if headline else "")
        + (summary + "\n\n" if summary else "")
        + "\n".join(f"- {line}" for line in lines)
    )
    return Message(subject, _shell("Tracker digest", body), text, "digest")


def test_message(recipients: Sequence[str]) -> Message:
    body = (
        "<p>This is a test message from the Ross Auction IT Tracker.</p>"
        "<p>If you can read this, SMTP delivery is working and these "
        "addresses will receive new-auction alerts, change alerts and the "
        "pre-close reminders (3 hours and 30 minutes before each auction "
        "closes).</p>"
        f'<p style="color:{GREY};">Recipients: {escape(", ".join(recipients))}</p>'
    )
    return Message(
        "Test message — auction tracker email is working",
        _shell("SMTP test", body),
        "Test message from the Ross Auction IT Tracker. SMTP delivery works.\n"
        f"Recipients: {', '.join(recipients)}\n",
        "test",
    )
