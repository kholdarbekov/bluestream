"""The office's opening hours: ONE value, `COMPANY_OPENING_HOURS`, in schema.org `openingHours` text.

The Store JSON-LD publishes it and the footer and /contact print it, localized, as the office's hours (owner,
2026-11-14: Mon–Sat 9–18 is the office's working time). Delivery has its own line (`landing.footer.delivery_hours`),
never derived from this value.

The value is comma-separated specs (";" is read as a separator too), each some days and one time range:
"Mo-Sa 09:00-18:00", or "Mo-Fr 09:00-18:00,Sa 10:00-16:00". Days are schema.org's two-letter codes, one day, a range or
a comma list of both ("Tu,Th 16:00-20:00", "Mo-We,Fr 09:00-18:00"); times are 24-hour HH:MM. Since the comma separates
days as well as specs, a spec runs up to the first part that carries a time: "Tu,Th 16:00-20:00" is ONE spec, never
"Th 16:00-20:00" with "Tu" dropped. A spec that does not read is left out WHOLE of both the JSON-LD and the footer
(logged once per value), so a typo never reaches a search engine as a shorter schedule; when nothing reads, the footer
has no office-hours line and the JSON-LD no openingHours.

Day names and the time range come from `display_labels` (the website's one label helper), so the footer reads
"пн–сб 09:00–18:00" the way every other weekday span and time window on the site reads.
"""

import logging
import re
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from typing import Optional, Tuple

from business_app.utils import display_labels
from business_app.utils.translations import get_translation

logger = logging.getLogger(__name__)

DAY_CODES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")  # index = Python's weekday(): 0 = Monday
LINE_KEY = "web.shell.footer.office_hours"  # "Office hours: {hours}"

_SEPARATORS = re.compile(r"[,;]")
_TIME = re.compile(r"\d")  # days never hold a digit: the part that does closes its spec
_DAYS = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?"  # one day or a range: "Mo", "Mo-Sa"
_SPEC = re.compile(rf"^(?P<days>{_DAYS}(?:,{_DAYS})*) (?P<opens>\d{{2}}:\d{{2}})-(?P<closes>\d{{2}}:\d{{2}})$")


@dataclass(frozen=True)
class OpeningHours:
    """One spec that reads: its text as configured (what the JSON-LD publishes), its weekdays and its times."""

    spec: str
    weekdays: Tuple[int, ...]
    opens: time
    closes: time


def _clock(text: str) -> Optional[time]:
    try:
        return time.fromisoformat(text)
    except ValueError:  # "24:00", "25:00", "09:60"
        return None


def _rule(spec: str) -> Optional[OpeningHours]:
    match = _SPEC.match(spec)
    if not match:
        return None
    opens, closes = _clock(match["opens"]), _clock(match["closes"])
    if opens is None or closes is None:
        return None
    weekdays = {}  # in the order named, a day named twice counted once
    for item in match["days"].split(","):
        first_code, _, last_code = item.partition("-")
        first, last = DAY_CODES.index(first_code), DAY_CODES.index(last_code or first_code)
        weekdays.update(dict.fromkeys((first + step) % 7 for step in range((last - first) % 7 + 1)))  # "Sa-Mo" wraps
    return OpeningHours(spec=spec, weekdays=tuple(weekdays), opens=opens, closes=closes)


def _spec_texts(raw: Optional[str]):
    """The value cut into specs, spaces folded. A part with no time is days that lead the next part's spec
    ("Tu", "Th 16:00-20:00" -> "Tu,Th 16:00-20:00"); days left over at the end make a spec of their own (unreadable)."""
    spec = []
    for part in _SEPARATORS.split(raw or ""):
        text = " ".join(part.split())
        if not text:
            continue
        spec.append(text)
        if _TIME.search(text):
            yield ",".join(spec)
            spec = []
    if spec:
        yield ",".join(spec)


@lru_cache(maxsize=16)
def parse_opening_hours(raw: Optional[str]) -> Tuple[OpeningHours, ...]:
    """The specs of `raw` that read, in their order; () for an empty or unreadable value. Cached per value, so a bad
    spec is logged once, not on every page."""
    rules = []
    for spec in _spec_texts(raw):
        rule = _rule(spec)
        if rule is None:
            logger.warning(
                "COMPANY_OPENING_HOURS: %r is not '<Mo|Mo-Sa|Tu,Th> HH:MM-HH:MM', so it is not published", spec
            )
        else:
            rules.append(rule)
    return tuple(rules)


def opening_hours_specs(raw: Optional[str]) -> list:
    """The JSON-LD `openingHours` list: the specs that read, as configured."""
    return [rule.spec for rule in parse_opening_hours(raw)]


def opening_hours_text(raw: Optional[str], language: Optional[str] = None) -> str:
    """The hours as a visitor reads them: "Mon–Sat 09:00–18:00" / "пн–сб 09:00–18:00" / "Du–Sh 09:00–18:00", specs
    joined by "; "; "" when nothing reads."""
    return "; ".join(
        f"{display_labels.weekday_span(rule.weekdays, language)} {display_labels.window(rule.opens, rule.closes)}"
        for rule in parse_opening_hours(raw)
    )


def office_hours_line(raw: Optional[str], language: Optional[str] = None) -> str:
    """The footer's and /contact's line: "Office hours: Mon–Sat 09:00–18:00"; "" when nothing reads (no line). Before
    the shell seed has run the label row is missing: the hours then print on their own, never the key's name."""
    hours = opening_hours_text(raw, language)
    if not hours:
        return ""
    line = get_translation(LINE_KEY, language, hours=hours)
    return hours if line == LINE_KEY else line
