"""Does this Discord message match a keyword alert?

The rules are the ones the operator already knows from other pinger services,
kept deliberately close so a set of keywords can be pasted over:

    Positiv    #1234;#5678;pokemon+display;charizard
               "#…" entries restrict which channels count. The rest are the
               alternatives: ONE of them has to hit. Inside an alternative,
               "+" joins terms that must ALL appear — "pokemon+display" only
               fires on a message containing both.
    Negativ    sold out;restock
               ANY hit suppresses the alert. Checked after the positives, so a
               single unwanted word beats ten wanted ones — a false ping costs
               attention, a missed one costs a purchase, and these lists exist
               precisely to cut the noise the positives let through.
    Bereich    min50€;max200€;100-150€;42-45;47;US9
               Prices constrain the numbers found in the message. Anything that
               is not a price rule is treated as a size token and must appear.

Everything is matched case-insensitively on the plain message text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def fold(text: str) -> str:
    """Lower-case and strip accents, on both sides of every comparison.

    Shops write "Pokémon", people type "pokemon", and a keyword that misses
    because of an accent is a missed drop with no visible cause at all.
    """
    stripped = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in stripped if not unicodedata.combining(ch)).casefold()


SEPARATOR = ";"
AND_JOINER = "+"
CHANNEL_PREFIX = "#"

# "1.299,00 €" / "€49.99" / "59,95 EUR" — the shapes prices actually take in
# these channels. Kept narrow on purpose: matching bare numbers would let a
# product code or a size decide whether a price rule passes.
_PRICE = re.compile(
    r"(?:€|eur)\s*(\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)"
    r"|(\d{1,4}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|eur)",
    re.I,
)
_MIN = re.compile(r"^min\s*(\d+(?:[.,]\d+)?)\s*€?$", re.I)
_MAX = re.compile(r"^max\s*(\d+(?:[.,]\d+)?)\s*€?$", re.I)
_RANGE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)\s*€$", re.I)


def _number(raw: str) -> float:
    """German and English decimals both appear in the same channel."""
    text = raw.strip().replace(" ", "")
    if "," in text and "." in text:  # 1.299,00 — the dot groups thousands
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def prices_in(text: str) -> list[float]:
    return [_number(a or b) for a, b in _PRICE.findall(text)]


@dataclass(frozen=True)
class Rules:
    """A parsed alert definition."""

    channels: tuple[str, ...] = ()
    positives: tuple[tuple[str, ...], ...] = ()  # OR over AND-groups
    negatives: tuple[str, ...] = ()
    price_min: float | None = None
    price_max: float | None = None
    sizes: tuple[str, ...] = field(default=())


def _entries(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(SEPARATOR) if part.strip()]


def parse_positive(raw: str) -> tuple[tuple[str, ...], list[str]]:
    """-> (alternatives, channel ids). Channel entries are "#<id>"."""
    channels: list[str] = []
    alternatives: list[tuple[str, ...]] = []
    for entry in _entries(raw):
        if entry.startswith(CHANNEL_PREFIX):
            if ident := entry[1:].strip():
                channels.append(ident)
            continue
        terms = tuple(fold(t) for t in entry.split(AND_JOINER) if t.strip())
        if terms:
            alternatives.append(terms)
    return tuple(alternatives), channels


def parse_ranges(raw: str) -> tuple[float | None, float | None, tuple[str, ...]]:
    low: float | None = None
    high: float | None = None
    sizes: list[str] = []
    for entry in _entries(raw):
        if m := _MIN.match(entry):
            low = _number(m.group(1))
        elif m := _MAX.match(entry):
            high = _number(m.group(1))
        elif m := _RANGE.match(entry):
            low, high = _number(m.group(1)), _number(m.group(2))
        else:
            sizes.append(fold(entry))
    return low, high, tuple(sizes)


def parse_rules(positive: str, negative: str = "", ranges: str = "") -> Rules:
    alternatives, channels = parse_positive(positive)
    low, high, sizes = parse_ranges(ranges)
    return Rules(
        channels=tuple(channels),
        positives=alternatives,
        negatives=tuple(fold(t) for t in _entries(negative)),
        price_min=low,
        price_max=high,
        sizes=sizes,
    )


def first_hit(rules: Rules, text: str) -> str | None:
    """Which alternative fired, joined the way it was written.

    The ping says "dein Keyword wurde gepingt: (erste-partner)" — with a dozen
    keywords in one setting, a hit that does not name itself leaves you
    guessing why you were pinged.
    """
    lowered = fold(text)
    for group in rules.positives:
        if all(term in lowered for term in group):
            return AND_JOINER.join(group)
    return None


def filter_text(rules: Rules) -> str:
    """The active price/size limits, for the ping's header line."""
    parts: list[str] = []
    if rules.price_min is not None:
        parts.append(f"min{rules.price_min:g}€")
    if rules.price_max is not None:
        parts.append(f"max{rules.price_max:g}€")
    parts.extend(rules.sizes)
    return "; ".join(parts)


def matches(rules: Rules, text: str, channel_id: str | None = None) -> bool:
    """True when this message should raise the alert."""
    if rules.channels and (channel_id or "") not in rules.channels:
        return False
    lowered = fold(text)
    if not rules.positives:
        return False  # an alert with no wanted words would fire on everything
    if not any(all(term in lowered for term in group) for group in rules.positives):
        return False
    if any(term in lowered for term in rules.negatives):
        return False
    if rules.sizes and not any(size in lowered for size in rules.sizes):
        return False
    if rules.price_min is not None or rules.price_max is not None:
        found = prices_in(lowered)
        if not found:
            # No price in the message. Dropping it would silence the drops that
            # post a picture and a link and nothing else, which are the ones
            # worth having — so a price rule only ever rejects a price it saw.
            return True
        low = rules.price_min if rules.price_min is not None else float("-inf")
        high = rules.price_max if rules.price_max is not None else float("inf")
        if not any(low <= price <= high for price in found):
            return False
    return True
