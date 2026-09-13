"""Prompt templates.

Kept separate from the calling code so wording can be tuned without touching
logic. Every prompt demands strict JSON so the results can be stored.
"""

from __future__ import annotations

SYSTEM_ANALYST = (
    "You are an IT asset specialist assisting with second-hand equipment "
    "auctions in Australia. You know computer hardware, peripherals, "
    "networking, printers, phones, tablets, displays and audio-visual gear, "
    "including brand model lines and how model numbers map to specifications. "
    "You answer strictly in the JSON schema requested, with no prose, no "
    "markdown fences and no commentary."
)

SYSTEM_VALUER = (
    "You are a cautious second-hand IT equipment valuer for Australian "
    "auctions. You reason from the supplied historical sale evidence rather "
    "than from general intuition, you state your uncertainty honestly, and "
    "you never invent comparable sales that were not provided. You answer "
    "strictly in the JSON schema requested."
)

# ---------------------------------------------------------------------------
# Auction classification
# ---------------------------------------------------------------------------

CLASSIFY_AUCTION = """\
Decide whether this auction is worth tracking for someone who buys IT
equipment: computers, laptops, tablets, phones, servers, networking gear,
monitors/displays, printers/scanners/copiers, storage, components,
peripherals, and closely related electronics or audio-visual equipment.

Treat brand-and-product reasoning as in scope.
IT equipment examples:
- "Brother HL-2270DW" is a laser printer: IT.
- "Gateway notebook" is a laptop computer: IT.
- "EIZO RadiForce medical imaging monitor" is a display: IT.
- "HP Mini Desktop", "ProBook", "EliteDesk" are computers: IT.
- "Cisco Catalyst", "Ubiquiti UniFi" are network switches: IT.
- "Microsoft Surface Pro" is an IT tablet: IT.
- A mixed auction containing a real block of IT lots (e.g. medical clearance with 20+ computer monitors and laptops) IS worth tracking; set "is_it": true and "mixed": true.

Strict NON-IT examples (must be is_it: false):
- Diesel engines, Stamford alternators, induction motors, generators, pumps.
- Mining spares, earthmoving parts, Caterpillar/Komatsu parts or machine cabs (e.g. "surface rust on cab").
- Chemical & cleaning supplies (e.g. "dry chlorine tablets", "drain cleaner").
- Workshop & woodworking tools (e.g. woodworking "routers", "surface grinders", tool racks).
- Gym & fitness equipment (e.g. weight racks, heart rate monitors).
- Marine & recreational vehicles (e.g. "200 HP outboard motors", camper trailers, boats, jet skis).
- Timber, pine, tiles, doors, cattle yards, farm fencing.

AUCTION TITLE: {title}
AUCTION URL: {url}
LOCATION: {location}
STATUS: {status}
DESCRIPTION (may be truncated):
{description}

SAMPLE OF LOT DESCRIPTIONS (may be empty if not yet scraped):
{lot_sample}

Reply with exactly this JSON object:
{{
  "is_it": true or false,
  "confidence": 0.0 to 1.0,
  "mixed": true or false,
  "categories": ["short category labels for the IT content, e.g. laptops, monitors"],
  "reason": "one sentence, at most 200 characters"
}}"""

# ---------------------------------------------------------------------------
# Lot classification
# ---------------------------------------------------------------------------

CLASSIFY_LOTS = """\
Decide for each auction lot below whether it is genuine IT equipment (computers, laptops,
tablets, phones, servers, networking switches/routers, monitors/displays, printers,
scanners, storage drives, components, IT peripherals).

Strict NON-IT items that must be is_it: false:
- "Chlorine tablet", "bactericide tablet" -> false (chemical tablet, NOT computing tablet).
- "200 HP motor", "HP outboard engine", "HP pump" -> false (horsepower, NOT HP computer).
- "Woodworking router", "router bit", "plunge router" -> false (wood tool, NOT network router).
- "Hydraulic ram", "ram cylinder", "ram pump" -> false (hydraulics, NOT computer RAM).
- "Surface rust", "surface grinder", "surface finish" -> false (NOT Microsoft Surface).
- "Squat rack", "drying rack", "towel rack", "pallet rack" -> false (NOT server rack).
- "Treadmill screen", "fly screen", "crusher screen" -> false (NOT computer monitor).
- "Ignition switch", "light switch", "pressure switch" -> false (NOT network switch).
- "Brother sewing machine" -> false.
- "LG dishwasher", "LG washer dryer", "LG refrigerator" -> false (home appliance).
- "Machine cab", "cab assembly" -> false.

LOTS (one per line, "index | description"):
{lots}

Reply with exactly this JSON object:
{{
  "results": [
    {{
      "index": 0,
      "is_it": true or false,
      "reason": "short explanation"
    }}
  ]
}}"""


# ---------------------------------------------------------------------------
# Lot specification extraction
# ---------------------------------------------------------------------------

EXTRACT_SPECS = """\
Extract structured attributes for each auction lot below. Infer the product
type from brand and model knowledge; for example "Brother HL-2270DW" is a
laser printer, "Latitude 7490" is a business laptop, "RadiForce RX340" is a
medical display. Leave a field as an empty string when the text genuinely does
not support a value. Never guess a specification that is not implied by the
model number or the text.

LOTS (one per line, "index | quantity | description"):
{lots}

Reply with exactly this JSON object, one entry per input index:
{{
  "lots": [
    {{
      "index": 0,
      "is_it": true or false,
      "category": "laptop | desktop | mini pc | server | monitor | printer | scanner | phone | tablet | networking | storage | component | peripheral | av | other | non-it",
      "brand": "manufacturer or empty",
      "model": "model line and number, or empty",
      "specs": {{
        "cpu": "", "ram": "", "storage": "", "screen_size": "",
        "resolution": "", "condition": "", "year": "", "notes": ""
      }}
    }}
  ]
}}"""

# ---------------------------------------------------------------------------
# Per-scan narrative
# ---------------------------------------------------------------------------

SUMMARIZE_SCAN = """\
You are writing the analyst note for one monitoring pass over tracked IT
auctions. Be specific and quantitative; the reader wants to know whether
bidding is heating up or going quiet, and what deserves attention before the
auctions close.

SCAN TYPE: {cycle_type}
RUN AT: {timestamp}
AUCTIONS INSPECTED: {auction_count}
LOTS INSPECTED: {lot_count}

CHANGES OBSERVED THIS PASS ({change_count} total, sample below):
{changes}

AUCTION STATE:
{auction_state}

Reply with exactly this JSON object:
{{
  "headline": "at most 120 characters",
  "summary": "2 to 5 sentences of plain-text analysis",
  "momentum": "heating_up | steady | cooling_off | no_activity",
  "watch_items": ["at most 5 short, specific bullet points"]
}}"""

# ---------------------------------------------------------------------------
# Price estimation / operator Q&A
# ---------------------------------------------------------------------------

ESTIMATE_PRICE = """\
The operator wants a maximum bid recommendation, based on historical results
from this auction house.

OPERATOR QUESTION:
{question}

TARGET ITEM:
{target}

HISTORICAL EVIDENCE FROM PAST AUCTIONS AT THIS HOUSE
(each row: lot, description, quantity, final sale price in AUD, bid count,
auction, close date; "final" is the hammer price excluding buyer's premium
and GST):
{comparables}

Rules:
- Base the recommendation on the evidence above. If the evidence is thin or
  the items are not truly comparable, say so and lower your confidence.
- Note when prices are per-lot rather than per-unit; a lot of 10 monitors is
  not comparable to a single monitor without dividing through.
- Remember the buyer pays a buyer's premium and GST on top of the hammer
  price, so a "maximum bid" should sit below the operator's true walk-away
  total cost. Mention this when relevant.
- Adjust for specification differences (CPU generation, RAM, storage, screen
  size, condition) and for the age of the comparable sale.

Reply with exactly this JSON object:
{{
  "max_bid_aud": number or null,
  "fair_range_aud": [low number, high number] or null,
  "confidence": "high | medium | low",
  "comparables_used": ["short references to the rows you relied on"],
  "reasoning": "3 to 6 sentences of plain text",
  "caveats": ["short warnings, at most 4"]
}}"""

ASK_GENERAL = """\
Answer the operator's question about the auction data below. Be concrete and
cite lot numbers, prices and dates from the data. If the data does not contain
the answer, say exactly that instead of speculating.

QUESTION:
{question}

DATA:
{context}

Reply with exactly this JSON object:
{{
  "answer": "plain text answer, at most 250 words",
  "confidence": "high | medium | low",
  "data_gaps": ["what extra data would improve the answer, at most 3"]
}}"""
