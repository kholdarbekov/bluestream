"""The web checkout must default the delivery date to TODAY, in local time.

Source-level, because there is no JS runtime in the backend test environment
(`business_app/static/js/` is served as-is; only `admin_ui/` has a JS test
runner, and this file is not part of that package). The two properties pinned
here are the ones that silently break the whole scheduled-order feature for
every web customer, so a grep-shaped guard is worth more than no guard:

1. **The default is today, not tomorrow.** `delivery_date` decided nothing
   before scheduled orders landed, so a tomorrow default was inert. It is now
   load-bearing: a future-dated order has NO `Delivery` row and is invisible to
   every driver until that morning. `placeOrder()` also blocks submit on a
   missing `deliveryDate`, so the page has no "deliver now" option at all — a
   tomorrow default delays every web order by a day unless the customer
   notices the date picker.

2. **The date is derived locally, not from `toISOString()`.** That method
   renders the UTC calendar day; Tashkent is UTC+5, so for the first five hours
   of every local day it yields yesterday — a past date the backend rejects
   with a 400 on the page's own default value.
"""

import re
from pathlib import Path

CHECKOUT_JS = Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages" / "checkout.js"


def _source() -> str:
    return CHECKOUT_JS.read_text(encoding="utf-8")


def _code_lines(src: str) -> str:
    """Comments explain the trap by name, so they would defeat every assertion
    below. Strip `//` line comments before matching."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def test_checkout_js_exists():
    assert CHECKOUT_JS.is_file(), f"{CHECKOUT_JS} moved — update this guard rather than deleting it"


def test_delivery_date_is_never_derived_from_toisostring():
    code = _code_lines(_source())
    assert "toISOString" not in code, (
        "checkout.js derives a calendar date from toISOString() again. That is the UTC day: "
        "between 00:00 and 05:00 Tashkent it is yesterday, and the backend 400s the page's own default."
    )


def test_delivery_date_default_is_not_tomorrow():
    code = _code_lines(_source())
    assert not re.search(r"getDate\(\)\s*\+\s*1", code), (
        "checkout.js is defaulting the delivery date a day forward again. Every web order would be "
        "withheld from drivers until the next morning, and this page offers no 'deliver now' option."
    )


def test_delivery_date_min_and_value_are_both_todays_local_date():
    code = _code_lines(_source())
    assert re.search(r"localDateString\(new Date\(\)\)", code), "the local-today helper call is gone"
    # Same variable feeds the picker's floor and its preselected value: today
    # is both the earliest allowed day and the default one.
    assert re.search(r"\.min\s*=\s*today\b", code)
    assert re.search(r"\.value\s*=\s*today\b", code)
