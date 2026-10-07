"""A sales agent opens My earnings, pages its lists, and follows a pay push into it.

Driven through the real ``Application`` (``tests/staff_bot/ptb_harness.py``) for the reason
``test_sales_stats_journey.py`` is: the way in is a button on a keyboard someone else draws
(the profile hub), every screen is a callback a pattern has to claim, and two of those
callbacks are also the buttons on the pay pushes, which must answer from a cold chat.

What is asserted is the PAYLOAD at both ends: the query the bot sends (S1 and S3 carry none, S2
exactly the `month` and `page` its button drew, S4 the month in its path) and the screen it prints. The S1 fixtures
are the compensation spec's own (§1.2, §5.4): Illustration B, the carry case below 0 and the
leaver's November, and the §7.2 screens are asserted byte for byte. The tier rows are A8's
product shape (§5.2) carrying Illustration B's and C's figures, and S2's rows are v5's, one per
order with its units. Every figure is the backend's, so a row wired to the wrong field, or a
sign the bot decided for itself, fails.

The pushes are posted the way the backend posts them: a real aiohttp server built by
`StaffWebhookServer.setup()`, a signed body, the real rate limiter and the real dedup (Redis
absent, production's degraded path). The message leaves through the harness transport, so
its button is tapped through the real dispatcher.

Copy comes from ``scripts/seed_staff_translations.py`` via ``_curated_value``, the resolver
``seed_translations()`` uses, so a seed edit cannot leave this file asserting strings
production no longer ships.
"""

import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import test_utils

from staff_bot.config import config as staff_config
from staff_bot.webhook_server import StaffWebhookServer
from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)
from tests.staff_bot.test_sales_hub_journey import LOGIN, _SEED, _alerts, _calls, _curated, _rendered, _table
from tests.staff_bot.test_sales_stats_journey import EXTRA_KEYS as PROFILE_KEYS

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROOT = Path(__file__).resolve().parents[2]
EARNINGS = "/api/v1/staff/sales/me/earnings"
LINES = "/api/v1/staff/sales/me/earnings/lines"
STATEMENTS = "/api/v1/staff/sales/me/statements"
SALES_EVENT = "/internal/sales-event"
WEBHOOK_SECRET = "earnings-journey-webhook-secret"
LANGUAGES = ("en", "uz", "ru")
# U+2212, the sign `signed_currency` prints. Spelled out so no expectation below can pass
# with the ASCII hyphen.
MINUS = "−"

PAY_PUSH_KEYS = (
    "staff.sales.notify.pay_penalty_confirmed", "staff.sales.notify.pay_statement_approved",
    "staff.sales.notify.pay_statement_approved_shadow", "staff.sales.notify.penalty_type",
    "staff.sales.notify.penalty_date", "staff.sales.notify.penalty_amount",
    "staff.sales.notify.counts_in", "staff.sales.notify.late", "staff.sales.notify.open_earnings",
    "staff.sales.notify.open_statement",
)
# Every earnings row the seed ships, plus the push rows. Read off the seed rather than listed:
# the expectations below are hand-written English, so a row missing from the seed fails them
# instead of rendering a plausible `humanise_key` word.
EARNINGS_KEYS = tuple(
    f"staff.sales.{suffix}" for suffix in _SEED.SALES_TEXT_TRANSLATIONS if suffix.startswith("earnings.")
) + PAY_PUSH_KEYS


# ---- the tier rows (D-BANDS): A8's `summary.commission.products` shape (§5.2) --------------------

NAMES_19L = {"en": "19L", "uz": "19 l", "ru": "19 л"}
# No Russian name: the screens fall back to English (§7.2), which the Russian card asserts.
NAMES_JUICE = {"en": "Juice 1 L", "uz": "Sharbat 1 l"}


def _tier(from_unit, to_unit, units, mode, value, amount, *, net=0.0, amount_full=None):
    """One published tier. `amount_full` is the tier at full money; it differs from `amount`
    only when an order in the tier was paid on less (D-Q3, V5-B10)."""
    return {"from_unit": from_unit, "to_unit": to_unit, "units": units, "mode": mode, "value": value,
            "net": net, "amount_full": amount if amount_full is None else amount_full, "amount": amount}


def _product(product_id, names, units, total, tiers, *, uses_default=False, next_tier=None):
    return {"product_id": product_id, "product_name": names, "uses_default_tiers": uses_default,
            "units": units, "total": total, "tiers": tiers, "next_tier": next_tier}


# Illustration B (§1.2): 19L on two tiers (1–300 at 1,000, 301+ at 1,500), juice on the default
# 2%. Both products are in their top tier, so neither has a next tier.
ILLUSTRATION_B_PRODUCTS = [
    _product(3, NAMES_19L, 500, 600000, [
        _tier(1, 300, 300, "per_unit", 1000.0, 300000, net=6000000.0),
        _tier(301, None, 200, "per_unit", 1500.0, 300000, net=4000000.0),
    ]),
    _product(9, NAMES_JUICE, 500, 250000, [
        _tier(1, None, 500, "percent", 2.0, 250000, net=12500000.0),
    ], uses_default=True),
]
# Illustration B's one late order is September's SA_000377_26, reversed: 10 bottles on September's
# single 2,000 tier, so its group reads 410 → 400 units and −20,000 (the lines list's reversal).
SEPTEMBER_GROUP = {
    "earned_month": "2026-09", "plan_version_id": 2, "counted": True, "gross": -20000, "multiplier": 1.0,
    "source": "statement", "after_gate": -20000,
    "products": [{"product_id": 3, "product_name": NAMES_19L, "units_before": 410, "units": 400,
                  "total_before": 820000, "total": 800000, "change": -20000,
                  "tiers_before": [_tier(1, None, 410, "per_unit", 2000.0, 820000, net=8200000.0)],
                  "tiers": [_tier(1, None, 400, "per_unit", 2000.0, 800000, net=8000000.0)]}],
}
# Illustration C (§1.2, T-TIER-1/-2): A 300, B 250 and C 150 bottles of 19L at 20,000 in an open
# October, on [(1, 1,000), (501, 1,500), (1,001, 2,000), (2,001, 2,500)] per unit.
NEXT_1001 = {"from_unit": 1001, "units_to_go": 301, "mode": "per_unit", "value": 2000.0}
ILLUSTRATION_C = _product(3, NAMES_19L, 700, 800000, [
    _tier(1, 500, 500, "per_unit", 1000.0, 500000, net=10000000.0),
    _tier(501, 1000, 200, "per_unit", 1500.0, 300000, net=4000000.0),
], next_tier=NEXT_1001)
# T-TIER-5: C paid half (3,000,000 → 1,500,000), so tier 2, which holds C's 150 units, pays 187,500.
ILLUSTRATION_C_HALF_PAID = _product(3, NAMES_19L, 700, 687500, [
    _tier(1, 500, 500, "per_unit", 1000.0, 500000, net=10000000.0),
    _tier(501, 1000, 200, "per_unit", 1500.0, 187500, net=4000000.0, amount_full=300000),
], next_tier=NEXT_1001)
# T-TIER-3's figures: juice at 12,500 on [(1, 2%), (101, 3%)], 110 units; and a juice still below
# its next tier, whose rate is a percent with a decimal.
JUICE_TWO_PERCENT_TIERS = _product(9, NAMES_JUICE, 110, 28750, [
    _tier(1, 100, 100, "percent", 2.0, 25000, net=1250000.0),
    _tier(101, None, 10, "percent", 3.0, 3750, net=125000.0),
], uses_default=True)
JUICE_BELOW_ITS_NEXT_TIER = _product(10, {"en": "Juice 0.5 L", "uz": "Sharbat 0,5 l", "ru": "Сок 0,5 л"},
                                     90, 22500, [_tier(1, 100, 90, "percent", 2.0, 22500, net=1125000.0)],
                                     next_tier={"from_unit": 101, "units_to_go": 11, "mode": "percent",
                                                "value": 2.5})


def _illustration_c_november():
    """Illustration C (b) (§1.2, T-TIER-6 without the juice): October approved at ×0.8, then A's
    cash corrected to 0 on 5 November. November's estimate carries October's group: 19L 700 → 400
    units, 800,000 → 400,000, −320,000 after the gate."""
    group = {"earned_month": "2026-10", "plan_version_id": 3, "counted": True, "gross": -400000, "multiplier": 0.8,
             "source": "statement", "after_gate": -320000,
             "products": [{"product_id": 3, "product_name": NAMES_19L, "units_before": 700, "units": 400,
                           "total_before": 800000, "total": 400000, "change": -400000,
                           "tiers_before": ILLUSTRATION_C["tiers"],
                           "tiers": [_tier(1, 500, 400, "per_unit", 1000.0, 400000, net=8000000.0)]}]}
    return _estimate(**{**_NOTHING_ELSE, "late": {"after_gate": -320000, "lines": 1, "groups": [group]}})


# Review Focus 5: a busy month (§7.2 "Length").
BUSY_MONTHS = ("2026-06", "2026-07", "2026-08", "2026-09")


def _busy_names(index, length):
    return {"en": f"Product {index} " + "Ö" * length, "uz": f"Mahsulot {index} " + "Ö" * length,
            "ru": f"Товар {index} " + "Ж" * length}


def _four_tiers(top):
    """Four per-unit tiers, 50 units in the top one; a `top` below its 125,000 is a top tier
    paid on less money."""
    return [
        _tier(1, 100, 100, "per_unit", 1000.0, 100000),
        _tier(101, 200, 100, "per_unit", 1500.0, 150000),
        _tier(201, 300, 100, "per_unit", 2000.0, 200000),
        _tier(301, None, 50, "per_unit", 2500.0, top, amount_full=125000),
    ]


def _busy_month(name_length):
    """Review Focus 5: six products, three of them on four tiers each (the first with a
    `tier_scaled` top tier), three on a percent tier below their next one, and four late groups
    that each moved all six. That is 6 product lines, 15 tier rows, 3 next-tier rows and 24 late
    rows. The names are the backend's and `name_length` characters long, so the test, not the
    luck of short names, decides how much of the card has to give way."""
    products = [
        _product(1, _busy_names(1, name_length), 350, 512500, _four_tiers(62500)),
        _product(2, _busy_names(2, name_length), 350, 575000, _four_tiers(125000)),
        _product(3, _busy_names(3, name_length), 350, 575000, _four_tiers(125000)),
    ] + [
        _product(index, _busy_names(index, name_length), 40, 30000,
                 [_tier(1, 100, 40, "percent", 3.0, 30000, net=1000000.0)],
                 next_tier={"from_unit": 101, "units_to_go": 61, "mode": "percent", "value": 4.0})
        for index in (4, 5, 6)
    ]
    groups = [
        {"earned_month": month, "plan_version_id": 2, "counted": True, "gross": -60000, "multiplier": 1.0,
         "source": "statement", "after_gate": -60000,
         "products": [{"product_id": index, "product_name": _busy_names(index, name_length),
                       "units_before": 50, "units": 40, "total_before": 50000, "total": 40000,
                       "change": -10000, "tiers_before": [], "tiers": []} for index in range(1, 7)]}
        for month in BUSY_MONTHS
    ]
    return _estimate(commission={"gross": 1752500, "orders": 31, "after_gate": 1402000, "products": products},
                     late={"after_gate": -240000, "lines": 24, "groups": groups})


def _busy_late_rows(language, name_length):
    """The 24 late rows as §7.2 prints them, in the backend's order: month by month, product by
    product."""
    units = {"en": "40 units", "ru": "40 шт."}[language]
    return [f"   · {month[5:]}.{month[:4]} · {_busy_names(index, name_length)[language]}: 50 → {units}"
            for month in BUSY_MONTHS for index in range(1, 7)]


MORE_ROW = re.compile(r"^   (· )?(\+\d+ more|ещё \d+)$")
LATE_ROW = re.compile(r"^   · \d\d\.\d{4} · ")
PRODUCT_WORD = {"en": "Product", "ru": "Товар"}


# ---- S1: the compensation spec's own figures (§1.2, §5.4) ------------------------------------

def _estimate(**over):
    """Illustration B (§1.2): October 2026, base 3,000,000, a holiday on the 1st, unpaid days on
    the 14th and 15th. Every figure differs from every other, so a mislabelled row cannot pass."""
    body = {
        "base": {"monthly": 3000000.0, "amount": 2800000.0, "worked_days": 28, "working_days": 30},
        "commission": {"gross": 850000.0, "orders": 31, "after_gate": 680000.0,
                       "products": ILLUSTRATION_B_PRODUCTS},
        "late": {"after_gate": -20000.0, "lines": 1, "groups": [SEPTEMBER_GROUP]},
        "gate": {"compliance_pct": 75.5, "visits_due": 212, "visits_counted": 160, "multiplier": 0.8,
                 "min_due": 20, "rule": "band", "provisional": True},
        "new_outlets": {"amount": 200000.0, "count": 2},
        "adjustments": {"amount": 50000.0, "items": [{"amount": 50000.0, "reason": "August correction"}]},
        "penalties": {"amount": 150000.0, "count": 1},
        "variable": 760000.0,
        "carry_in": {"amount": 0.0, "from_month": None, "source": None},
        "gross_total": 3560000.0, "total": 3560000.0, "carry_out": 0.0, "owed": 0.0,
    }
    body.update(over)
    return body


# The rows an estimate omits when they carry nothing (the §7.2 "below 0" and leaver screens).
_NOTHING_ELSE = {
    "late": {"after_gate": 0.0, "lines": 0},
    "new_outlets": {"amount": 0.0, "count": 0},
    "adjustments": {"amount": 0.0, "items": []},
    "penalties": {"amount": 0.0, "count": 0},
}


def _open_month(month="2026-10", *, end_date="2026-10-31", estimate=None, configured=True, is_shadow=False):
    """One `open_months` entry. `configured: false` is a STATE (no terms in force), not an
    error, and then carries no estimate (§5.4)."""
    if not configured:
        estimate = None
    elif estimate is None:
        estimate = _estimate()
    return {"month": month, "status": "open", "is_shadow": is_shadow, "configured": configured,
            "start_date": f"{month}-01", "end_date": end_date, "gate_window_end": end_date,
            "estimate": estimate}


AUGUST_STATEMENT = {
    "month": "2026-08", "status": "paid", "is_shadow": False, "paid_on": "2026-09-05", "is_latest": True,
    "base": 2800000.0, "gated_commission": 640000.0, "new_outlets": 150000.0, "adjustments": 0.0,
    "penalties": 0.0, "carry_in": 0.0, "total": 3590000.0, "carry_out": 0.0, "owed": 0.0,
    "owed_netted_in": None,
}
PIPELINE = {
    # The backend's own count and total, which cover every waiting order; the list under them
    # is capped (`PIPELINE_SIZE`) and trimmed to two rows here. The screen prints the rows it is
    # handed and names the other five with "+5 more" (T11-R1, T-PIPE-1).
    "count": 7, "estimated_total": 84000.0,
    "items": [
        {"order_number": "SA_000430_26", "outlet_name": "Oasis market", "placed_on": "2026-10-29",
         "waiting_for": "delivery", "total": 160000.0, "estimated_commission": 12000.0},
        # A second order at one outlet on one day, held for a manager (C14).
        {"order_number": "SA_000431_26", "outlet_name": "Baraka", "placed_on": "2026-10-30",
         "waiting_for": "approval", "total": 120000.0, "estimated_commission": 9000.0},
    ],
}
NO_PIPELINE = {"count": 0, "estimated_total": 0.0, "items": []}
PENALTIES = [
    {"id": 17, "incident_date": "2026-10-14",
     "type_names": {"en": "Missed planned visits", "uz": "Rejadagi tashriflar qoldirildi",
                    "ru": "Пропуск плановых визитов"},
     "reason": "Did not visit 5 planned outlets", "amount": 150000.0, "status": "confirmed",
     "posting_month": "2026-10", "is_late": False},
]


def _earnings(**over):
    """S1 on 31.10.2026 20:00 local, exactly the §5.4 example."""
    body = {
        "state": "ok", "as_of": "2026-10-31T15:00:00+00:00",
        "open_months": [_open_month()],
        "in_review": [{"month": "2026-09", "status": "closed"}],
        "last_statement": dict(AUGUST_STATEMENT),
        "pipeline": PIPELINE,
        "penalties": PENALTIES,
    }
    body.update(over)
    return body


def _carry_case_estimate():
    """The §1.2 carry case on 31.10.2026: base 3,000,000 with 2 of 30 days worked, a September
    credit of 300,000 reversed late, one penalty of 30,000. Variable 30,000 − 300,000 − 30,000 =
    −300,000; gross 200,000 − 300,000 = −100,000; paid 0; carry out −100,000."""
    return _estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 3000000.0, "amount": 200000.0, "worked_days": 2, "working_days": 30},
        "commission": {"gross": 30000.0, "orders": 3, "after_gate": 30000.0},
        "late": {"after_gate": -300000.0, "lines": 1},
        "gate": {"compliance_pct": 50.0, "visits_due": 6, "visits_counted": 3, "multiplier": 1.0,
                 "min_due": 20, "rule": "below_min_due", "provisional": True},
        "penalties": {"amount": 30000.0, "count": 1},
        "variable": -300000.0,
        "gross_total": -100000.0, "total": 0.0, "carry_out": -100000.0, "owed": 0.0,
    })


def _october_statement(**over):
    """The leaver's October once approved: paid 0, owed 100,000 (§1.2)."""
    body = {**AUGUST_STATEMENT, "month": "2026-10", "status": "approved", "paid_on": None,
            "base": 200000.0, "gated_commission": 30000.0, "new_outlets": 0.0, "penalties": 30000.0,
            "total": 0.0, "owed": 100000.0, "owed_netted_in": "2026-11"}
    body.update(over)
    return body


def _leaver_november():
    """The §1.2 leaver's November, after October's approval and before November's close:
    0 + 50,000 − 100,000 = −50,000 ⇒ paid 0, owed 50,000; October's 100,000 is being netted."""
    estimate = _estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 3000000.0, "amount": 0.0, "worked_days": 0, "working_days": 30},
        "commission": {"gross": 50000.0, "orders": 2, "after_gate": 50000.0},
        "gate": {"compliance_pct": None, "visits_due": 0, "visits_counted": 0, "multiplier": 1.0,
                 "min_due": 20, "rule": "below_min_due", "provisional": True},
        "variable": 50000.0,
        "carry_in": {"amount": -100000.0, "from_month": "2026-10", "source": "owed"},
        "gross_total": -50000.0, "total": 0.0, "carry_out": 0.0, "owed": 50000.0,
    })
    return _earnings(
        as_of="2026-11-30T15:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30", estimate=estimate)],
        in_review=[], last_statement=_october_statement(), pipeline=NO_PIPELINE, penalties=[],
    )


# ---- S2 ---------------------------------------------------------------------------------------

# S2 v5 (`AGENT_ROW_KEYS`, §5.4): one row per order (its latest event in the month) or bonus, the
# order's units first: the units still counted, or those a reversal took out.
LINES_OCTOBER = {
    ("2026-10", 1): {
        "month": "2026-10", "status": "open", "available": True, "page": 1, "has_more": True,
        "items": [
            {"row": "order", "id": 412, "date": "2026-10-12", "order_number": "SA_000412_26",
             "outlet_name": "Oasis market", "earned_month": "2026-10", "is_late": False, "counted": True,
             "kind": "commission_credit", "cause": None, "tier_shift": False,
             "units": [{"product_name": NAMES_19L, "units": 12}], "amount": 18000},
            {"row": "order", "id": 377, "date": "2026-10-15", "order_number": "SA_000377_26",
             "outlet_name": "Baraka", "earned_month": "2026-09", "is_late": True, "counted": True,
             "kind": "commission_reversal", "cause": "nothing_received", "tier_shift": False,
             "units": [{"product_name": NAMES_19L, "units": 10}], "amount": -20000},
            {"row": "bonus", "id": 903, "date": "2026-10-20", "order_number": None,
             "outlet_name": "Oasis market", "earned_month": "2026-10", "is_late": False, "counted": True,
             "kind": "new_outlet_bonus", "cause": None, "tier_shift": False, "units": [], "amount": 100000},
        ],
    },
    ("2026-10", 2): {
        "month": "2026-10", "status": "open", "available": True, "page": 2, "has_more": False,
        "items": [
            {"row": "order", "id": 398, "date": "2026-10-22", "order_number": "SA_000398_26",
             "outlet_name": "Oasis market", "earned_month": "2026-09", "is_late": True, "counted": True,
             "kind": "commission_difference", "cause": "received_reduced", "tier_shift": False,
             "units": [{"product_name": NAMES_19L, "units": 6}], "amount": -2250},
        ],
    },
}
# T-TIER-20 (e) (OQ-B3, I-41): October approved at ×0.8; on 5 November an admin keeps 1 of A's 300
# bottles. November lists A's `units_reduced` change, with the one bottle still counted, and the two
# orders whose units moved into lower tiers without changing themselves (`tier_shift`: S2 gives
# them no date, kind or cause). The amounts are A9's, before the gate (−399,000 in all).
LINES_NOVEMBER_UNITS_REDUCED = {
    "month": "2026-11", "status": "open", "available": True, "page": 1, "has_more": False,
    "items": [
        {"row": "order", "id": 501, "date": "2026-11-05", "order_number": "SA_000501_26",
         "outlet_name": "Oasis market", "earned_month": "2026-10", "is_late": True, "counted": True,
         "kind": "commission_difference", "cause": "units_reduced", "tier_shift": False,
         "units": [{"product_name": NAMES_19L, "units": 1}], "amount": -299000},
        {"row": "order", "id": 502, "date": None, "order_number": "SA_000502_26",
         "outlet_name": "Baraka", "earned_month": "2026-10", "is_late": True, "counted": True,
         "kind": None, "cause": None, "tier_shift": True,
         "units": [{"product_name": NAMES_19L, "units": 250}], "amount": -25000},
        {"row": "order", "id": 503, "date": None, "order_number": "SA_000503_26",
         "outlet_name": "Chorsu", "earned_month": "2026-10", "is_late": True, "counted": True,
         "kind": None, "cause": None, "tier_shift": True,
         "units": [{"product_name": NAMES_19L, "units": 150}], "amount": -75000},
    ],
}
# Illustration C (b) (§7.2, M13: A is SA_000501_26): A's cash corrected to 0 on 5 November. The
# reversal prints the units it took out (S2 publishes `at_credit` for an order no longer counted).
LINES_NOVEMBER_REVERSAL = {
    "month": "2026-11", "status": "open", "available": True, "page": 1, "has_more": False,
    "items": [
        {"row": "order", "id": 501, "date": "2026-11-05", "order_number": "SA_000501_26",
         "outlet_name": "Oasis market", "earned_month": "2026-10", "is_late": True, "counted": True,
         "kind": "commission_reversal", "cause": "nothing_received", "tier_shift": False,
         "units": [{"product_name": NAMES_19L, "units": 300}], "amount": -300000},
        {"row": "order", "id": 502, "date": None, "order_number": "SA_000502_26",
         "outlet_name": "Baraka", "earned_month": "2026-10", "is_late": True, "counted": True,
         "kind": None, "cause": None, "tier_shift": True,
         "units": [{"product_name": NAMES_19L, "units": 250}], "amount": -25000},
    ],
}


def _lines_route(call):
    # A KeyError here IS an assertion: the bot asked for a month or a page no button offered.
    return LINES_OCTOBER[(call.params["month"], call.params["page"])]


# ---- S3 and S4: the agent's approved and paid statements ----------------------------------------

# A frozen statement's gate: the close settles it, so nothing about it is provisional any more.
FROZEN_GATE = {**_estimate()["gate"], "provisional": False}


def _statement(month="2026-10", *, status="approved", paid_on=None, is_shadow=False, is_latest=True,
               owed=0.0, owed_netted_in=None, statement=None):
    """S4: `_last_statement`'s head for the row (`AGENT_STATEMENT_HEAD_KEYS`) over the frozen
    statement in S1's estimate shape (`_agent_estimate`). Illustration B unless told otherwise."""
    return {"month": month, "status": status, "paid_on": paid_on, "is_shadow": is_shadow,
            "is_latest": is_latest, "owed": owed, "owed_netted_in": owed_netted_in,
            "statement": _estimate(gate=FROZEN_GATE) if statement is None else statement}


def _trial_september():
    """The dev September, as Task 1's S4 test (a) reads it: a trial month on base alone, 23 of
    30 days paid (1,500,000 × 23 / 30), no order credited, 0 of 42 visits at ×0.5 on a commission
    of 0, and one 100,000 penalty. A trial month neither carries nor owes (I-27)."""
    return _statement("2026-09", is_shadow=True, statement=_estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 1500000.0, "amount": 1150000.0, "worked_days": 23, "working_days": 30},
        "commission": {"gross": 0.0, "orders": 0, "after_gate": 0.0, "products": []},
        "late": {"after_gate": 0.0, "lines": 0, "groups": []},
        "gate": {"compliance_pct": 0.0, "visits_due": 42, "visits_counted": 0, "multiplier": 0.5,
                 "min_due": 20, "rule": "band", "provisional": False},
        "penalties": {"amount": 100000.0, "count": 1},
        "variable": -100000.0,
        "gross_total": 1050000.0, "total": 1050000.0, "carry_out": 0.0, "owed": 0.0,
    }))


# Illustration B's October once approved and paid on 5 November.
OCTOBER_STATEMENT = _statement(status="paid", paid_on="2026-11-05")
# S3 (`AGENT_STATEMENT_ITEM_KEYS`), newest month first: the paid October, then the trial September,
# which is approved and never paid (I-16).
PAST_STATEMENTS = {"items": [
    {"month": "2026-10", "total": 3560000.0, "status": "paid", "paid_on": "2026-11-05", "is_shadow": False},
    {"month": "2026-09", "total": 1050000.0, "status": "approved", "paid_on": None, "is_shadow": True},
]}


def _carry_case_frozen(**over):
    """The §1.2 carry case's October as approved: the estimate's figures, the gate settled."""
    return {**_carry_case_estimate(), "gate": {**_carry_case_estimate()["gate"], "provisional": False}, **over}


def _statement_calls(harness):
    """Every S3 and S4 request, as (endpoint, query): S4's month rides in the PATH."""
    return [(c.endpoint, c.params) for c in harness.backend.calls
            if c.method == "GET" and c.endpoint.startswith(STATEMENTS)]


# ---- the pushes (C24's keys, §7.5's figures) -------------------------------------------------

PENALTY_PUSH = {"penalty_id": 17, "month": "2026-10", "incident_date": "2026-10-14",
                "type_names": PENALTIES[0]["type_names"], "reason": "Did not visit 5 planned outlets",
                "amount": 150000, "is_late": False}
# §7.5: the commission is A8's `summary.commission` with `next_tier` null, as `_pushed_product`
# publishes a frozen statement's (it has none), so the push's tier rows are the summary's.
STATEMENT_PUSH = {"statement_id": 55, "month": "2026-10", "is_shadow": False, "base": 2800000,
                  "commission": {"gross": 850000, "products": ILLUSTRATION_B_PRODUCTS},
                  "commission_after_gate": 680000, "late": -20000, "new_outlets": 200000,
                  "adjustments": 50000, "penalties": 150000,
                  "carry_in": {"amount": 0, "from_month": None, "source": None},
                  "total": 3560000, "carry_out": 0, "owed": 0}


# ---- the screens, as §7.2 and §7.5 print them ------------------------------------------------

SUMMARY_EN = "\n".join((
    "💰 <b>My earnings — 10.2026</b>",
    "<i>Estimate as of 31.10.2026 20:00. The final amount is set after the month-end review.</i>",
    "",
    "Base: 2,800,000 UZS (3,000,000 UZS · 28 of 30 working days)",
    "Commission: 850,000 UZS · 31 orders",
    "   19L: 500 units · 600,000 UZS",
    "      1–300: 300 × 1,000 = 300,000",
    "      301+: 200 × 1,500 = 300,000",
    "   Juice 1 L: 500 units · 250,000 UZS",
    "      1+: 2% of 12,500,000 = 250,000",
    "Plan vs fact: 75.5% · 160 of 212 visits → ×0.8",
    "Commission after discipline: 680,000 UZS",
    f"Corrections from earlier months: {MINUS}20,000 UZS",
    "   · 09.2026 · 19L: 410 → 400 units",
    "New outlets: +200,000 UZS · 2",
    "Adjustments: +50,000 UZS",
    "   · +50,000 UZS — August correction",
    f"Penalties: {MINUS}150,000 UZS · 1",
    "Variable pay: 760,000 UZS",
    "<b>Total: 3,560,000 UZS</b>",
    "",
    "⏳ 7 orders are waiting for approval, delivery or payment ≈ 84,000 UZS",
    "🔎 09.2026 · Under review",
    "✅ Last statement: 08.2026 · 3,590,000 UZS · Paid · paid on 05.09.2026",
))
BELOW_ZERO_EN = "\n".join((
    "💰 <b>My earnings — 10.2026</b>",
    "<i>Estimate as of 31.10.2026 20:00. The final amount is set after the month-end review.</i>",
    "",
    "Base: 200,000 UZS (3,000,000 UZS · 2 of 30 working days)",
    "Commission: 30,000 UZS · 3 orders",
    "Plan vs fact: 50.0% · 3 of 6 visits → ×1.0 (fewer than 20 visits due so far; settled at month end)",
    "Commission after discipline: 30,000 UZS",
    f"Corrections from earlier months: {MINUS}300,000 UZS",
    f"Penalties: {MINUS}30,000 UZS · 1",
    f"Variable pay: {MINUS}300,000 UZS",
    "<b>Total: 0 UZS</b>",
    "Shortfall of 100,000 UZS will be deducted next month.",
))
LEAVER_EN = "\n".join((
    "💰 <b>My earnings — 11.2026</b>",
    "<i>Estimate as of 30.11.2026 20:00. The final amount is set after the month-end review.</i>",
    "",
    "Base: 0 UZS (3,000,000 UZS · 0 of 30 working days)",
    "Commission: 50,000 UZS · 2 orders",
    "Plan vs fact: — · 0 of 0 visits → ×1.0 (fewer than 20 visits due so far; settled at month end)",
    "Commission after discipline: 50,000 UZS",
    "Variable pay: 50,000 UZS",
    f"Owed from 10.2026: {MINUS}100,000 UZS",
    "<b>Total: 0 UZS</b>",
    "Owed: 50,000 UZS. It will be deducted from your later earnings.",
    "",
    "✅ Last statement: 10.2026 · 0 UZS · Approved",
    "Owed: 100,000 UZS. Being deducted in 11.2026.",
))
LINES_PAGE_1_EN = "\n".join((
    "🧾 <b>Credited orders — 10.2026</b> · Page 1",
    "",
    "12.10 · SA_000412_26 · Oasis market",
    "   19L × 12 · Commission +18,000 UZS",
    "15.10 · SA_000377_26 · Baraka",
    f"   19L × 10 · Reversal {MINUS}20,000 UZS · no payment left on the order · for 09.2026",
    "20.10 · Oasis market",
    "   New outlet bonus +100,000 UZS",
))
LINES_PAGE_2_EN = "\n".join((
    "🧾 <b>Credited orders — 10.2026</b> · Page 2",
    "",
    "22.10 · SA_000398_26 · Oasis market",
    f"   19L × 6 · Commission change {MINUS}2,250 UZS · less money received · for 09.2026",
))
PIPELINE_EN = "\n".join((
    "⏳ <b>Not counted yet</b>",
    "<i>Commission is counted once an order is both delivered and paid. Amounts are estimates.</i>",
    "",
    "SA_000430_26 · Oasis market · waiting for delivery ≈ 12,000 UZS",
    "SA_000431_26 · Baraka · waiting for manager approval ≈ 9,000 UZS",
    "+5 more",
))
PENALTIES_EN = "\n".join((
    "⚠️ <b>Penalties</b>",
    "",
    f"14.10.2026 · Missed planned visits · {MINUS}150,000 UZS",
    "   10.2026 · Confirmed",
    "   Reason: Did not visit 5 planned outlets",
))
PENALTY_PUSH_EN = "\n".join((
    "⚠️ <b>Penalty confirmed</b>",
    "Type: Missed planned visits",
    "Date: 14.10.2026",
    f"Amount: {MINUS}150,000 UZS",
    "Counts in 10.2026",
    "Reason: Did not visit 5 planned outlets",
))
STATEMENT_PUSH_EN = "\n".join((
    "✅ <b>Pay for 10.2026 is approved</b>",
    "Base: 2,800,000 UZS",
    "Commission: 850,000 UZS",
    "   19L: 500 units · 600,000 UZS",
    "      1–300: 300 × 1,000 = 300,000",
    "      301+: 200 × 1,500 = 300,000",
    "   Juice 1 L: 500 units · 250,000 UZS",
    "      1+: 2% of 12,500,000 = 250,000",
    "Commission after discipline: 680,000 UZS",
    f"Corrections from earlier months: {MINUS}20,000 UZS",
    "New outlets: +200,000 UZS",
    "Adjustments: +50,000 UZS",
    f"Penalties: {MINUS}150,000 UZS",
    "<b>Total: 3,560,000 UZS</b>",
))
# S4's screen: the summary's own rows for the frozen month, under its month and status, and no
# "Estimate as of" line (the figures are final).
TRIAL_STATEMENT_EN = "\n".join((
    "📄 <b>Statement — 09.2026</b> · Approved",
    "Trial month: these numbers are shown for review; pay is made the old way.",
    "",
    "Base: 1,150,000 UZS (1,500,000 UZS · 23 of 30 working days)",
    "Commission: 0 UZS · 0 orders",
    "Plan vs fact: 0.0% · 0 of 42 visits → ×0.5",
    "Commission after discipline: 0 UZS",
    f"Penalties: {MINUS}100,000 UZS · 1",
    f"Variable pay: {MINUS}100,000 UZS",
    "<b>Total: 1,050,000 UZS</b>",
))
OCTOBER_STATEMENT_EN = "\n".join((
    "📄 <b>Statement — 10.2026</b> · Paid · paid on 05.11.2026",
    "",
    "Base: 2,800,000 UZS (3,000,000 UZS · 28 of 30 working days)",
    "Commission: 850,000 UZS · 31 orders",
    "   19L: 500 units · 600,000 UZS",
    "      1–300: 300 × 1,000 = 300,000",
    "      301+: 200 × 1,500 = 300,000",
    "   Juice 1 L: 500 units · 250,000 UZS",
    "      1+: 2% of 12,500,000 = 250,000",
    "Plan vs fact: 75.5% · 160 of 212 visits → ×0.8",
    "Commission after discipline: 680,000 UZS",
    f"Corrections from earlier months: {MINUS}20,000 UZS",
    "   · 09.2026 · 19L: 410 → 400 units",
    "New outlets: +200,000 UZS · 2",
    "Adjustments: +50,000 UZS",
    "   · +50,000 UZS — August correction",
    f"Penalties: {MINUS}150,000 UZS · 1",
    "Variable pay: 760,000 UZS",
    "<b>Total: 3,560,000 UZS</b>",
))
# The §1.2 carry case's October, frozen: the rows of BELOW_ZERO_EN with the settled gate's words.
CARRY_CASE_STATEMENT_ROWS = (
    "Base: 200,000 UZS (3,000,000 UZS · 2 of 30 working days)",
    "Commission: 30,000 UZS · 3 orders",
    "Plan vs fact: 50.0% · 3 of 6 visits → ×1.0 (fewer than 20 visits due, no reduction)",
    "Commission after discipline: 30,000 UZS",
    f"Corrections from earlier months: {MINUS}300,000 UZS",
    f"Penalties: {MINUS}30,000 UZS · 1",
    f"Variable pay: {MINUS}300,000 UZS",
    "<b>Total: 0 UZS</b>",
)
PAST_STATEMENTS_EN = "📚 <b>Past statements</b>"
STATEMENT_BUTTONS = ["🧾 Credited orders 10.2026", "📚 Past statements", "⬅️ Back"]
STATEMENT_CALLBACKS = ["staff_sales_earn_l_202610_1", "staff_sales_earn_h", "staff_sales_earn"]


# ---- the harness ------------------------------------------------------------------------------

def _full_table():
    table = _table()
    for key in PROFILE_KEYS + EARNINGS_KEYS:
        for lang in LANGUAGES:
            table[(lang, key)] = _curated(key, lang)
    return table


async def _staff(monkeypatch, *, roles=("sales_agent",), language="en"):
    sells = "sales_agent" in roles
    row = {"id": 55, "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID), "first_name": "Sardor",
           "last_name": "Agent", "phone": "+998901234577", "preferred_language": language,
           "role": "sales_agent" if sells else "delivery", "status": "active",
           "staff_roles": json.dumps(list(roles)), "staff_bot_state": "{}"}
    harness = await build_staff_harness(monkeypatch, translations=_full_table(),
                                        database=FakeStaffDatabase(staff_user=row))
    harness.backend.route("POST", LOGIN, lambda _c: {
        "access_token": "staff-access-token", "refresh_token": "staff-refresh-token",
        "expires_in": 3600,
        "user": {"id": 55, "first_name": "Sardor", "last_name": "Agent",
                 "phone": "+998901234577", "preferred_language": language,
                 "staff_roles": list(roles),
                 "delivery_person_id": None if sells else 7,
                 "sales_agent_profile_id": 3 if sells else None},
    })
    ops = harness.updates()
    await harness.send(ops.command("start"))
    harness.telegram.reset()
    harness.backend.calls.clear()
    return harness, ops


@asynccontextmanager
async def _webhook(monkeypatch, harness):
    """The staff bot's webhook server, serving HTTP, sending through the harness Application.

    Redis is absent (production's degraded path), so the dedup is the in-memory key this
    route builds, which is the key a retry has to find released.
    """
    monkeypatch.setattr(staff_config.security, "webhook_secret", WEBHOOK_SECRET)
    server = StaffWebhookServer()

    async def _no_redis():
        server._redis, server._redis_connected = None, False

    monkeypatch.setattr(server, "_init_redis", _no_redis)
    server.set_application(harness.application)
    await server.setup()
    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:
        yield client


async def _push(client, body):
    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    response = await client.post(
        SALES_EVENT, data=raw,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature},
    )
    return response.status, await response.json()


# ---- the way in ---------------------------------------------------------------------------------

async def test_the_profile_hub_offers_my_earnings_directly_under_sales_stats(monkeypatch):
    """§7.1: beside My stats, because C4 ties the two screens together ("the same %")."""
    harness, ops = await _staff(monkeypatch)

    await harness.send(ops.tap("staff_profile"))

    hub = harness.telegram.last_shown()
    assert hub.callback_data() == ["staff_sales_stats", "staff_sales_earn", "staff_back_to_main"]
    assert "💰 My earnings" in hub.button_labels()


async def test_a_driver_neither_sees_nor_can_open_my_earnings(monkeypatch):
    harness, ops = await _staff(monkeypatch, roles=("delivery_driver",))
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())

    await harness.send(ops.tap("staff_profile"))
    assert "staff_sales_earn" not in harness.telegram.last_shown().callback_data()

    await harness.send(ops.tap("staff_sales_earn"))
    assert _curated("staff.unauthorized") in _alerts(harness)
    assert not _calls(harness, "GET", EARNINGS)


# ---- the summary and its variants ---------------------------------------------------------------

async def test_the_summary_prints_every_figure_the_backend_published(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())

    await harness.send(ops.tap("staff_profile"))
    await harness.send(ops.tap("staff_sales_earn"))

    # No query at all: the token says whose earnings, and the backend picks the months.
    assert [c.params for c in _calls(harness, "GET", EARNINGS)] == [None]
    card = harness.telegram.last_shown()
    assert card.text == SUMMARY_EN
    assert card.params["parse_mode"] == "HTML"
    assert card.callback_data() == [
        "staff_sales_earn_l_202610_1", "staff_sales_earn_p", "staff_sales_earn_x",
        "staff_sales_earn_s_202608", "staff_sales_earn_h", "staff_sales_earn", "staff_profile",
    ]
    assert card.button_labels() == [
        "🧾 Credited orders 10.2026", "⏳ Not counted yet", "⚠️ Penalties",
        "📄 Statement 08.2026", "📚 Past statements", "🔄 Refresh", "⬅️ Back",
    ]


async def test_pay_that_has_not_started_says_so_and_offers_only_refresh(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: {
        "state": "not_started", "as_of": "2026-09-20T06:00:00+00:00", "open_months": [],
        "in_review": [], "last_statement": None, "pipeline": None, "penalties": [],
    })

    await harness.send(ops.tap("staff_sales_earn"))

    card = harness.telegram.last_shown()
    assert card.text == "💰 <b>My earnings</b>\nPay tracking has not started yet."
    assert card.callback_data() == ["staff_sales_earn", "staff_profile"]


async def test_a_month_without_terms_asks_for_an_administrator_and_draws_no_lines_button(monkeypatch):
    """`configured: false` is a state, not an error (§5.4): no estimate, no pipeline line,
    and the months under review and the last statement still show."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(configured=False)]))

    await harness.send(ops.tap("staff_sales_earn"))

    card = harness.telegram.last_shown()
    assert card.text == "\n".join((
        "💰 <b>My earnings — 10.2026</b>",
        "Your pay terms are not set up yet. Please ask an administrator.",
        "",
        "🔎 09.2026 · Under review",
        "✅ Last statement: 08.2026 · 3,590,000 UZS · Paid · paid on 05.09.2026",
    ))
    assert card.callback_data() == [
        "staff_sales_earn_p", "staff_sales_earn_x", "staff_sales_earn_s_202608", "staff_sales_earn_h",
        "staff_sales_earn", "staff_profile",
    ]


async def test_the_below_zero_screen_carries_the_shortfall_into_next_month(monkeypatch):
    """The §7.2 "below 0" screen, byte for byte: a negative variable printed with its minus
    (never floored, C1 v3), the provisional minimum-due wording, and the carry note."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        open_months=[_open_month(estimate=_carry_case_estimate())], in_review=[],
        last_statement=None, pipeline=NO_PIPELINE,
        penalties=[{**PENALTIES[0], "amount": 30000.0}],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    assert harness.telegram.last_shown().text == BELOW_ZERO_EN


async def test_a_settled_minimum_due_says_no_reduction_not_settled_at_month_end(monkeypatch):
    """Review money M3: only a provisional gate promises to settle later."""
    settled = {**_carry_case_estimate()["gate"], "provisional": False}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        open_months=[_open_month(estimate={**_carry_case_estimate(), "gate": settled})],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    assert "Plan vs fact: 50.0% · 3 of 6 visits → ×1.0 (fewer than 20 visits due, no reduction)" in lines


async def test_the_leavers_november_nets_octobers_balance_and_says_where(monkeypatch):
    """The §7.2 leaver screen, byte for byte: the `owed_in` row (source "owed"), the estimate's
    owed note, and, under the last statement, `owed_netted_note` because November's estimate
    is the one deducting October's balance (`owed_netted_in`). The balance is read once:
    the screen owes 50,000, never 150,000. No plan-vs-fact figure exists (nothing was due),
    so the row prints My stats' dash."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _leaver_november())

    await harness.send(ops.tap("staff_sales_earn"))

    assert harness.telegram.last_shown().text == LEAVER_EN


async def test_a_shortfall_carried_forward_is_its_own_row_above_the_total(monkeypatch):
    """An employed agent's November after a −100,000 October (§1.2): the `carry_in` row, labelled
    by its source "carry_forward", and no owed or carry sentence under the total."""
    november = _estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 3000000.0, "amount": 3000000.0, "worked_days": 30, "working_days": 30},
        "commission": {"gross": 450000.0, "orders": 12, "after_gate": 450000.0},
        "gate": {"compliance_pct": 82.5, "visits_due": 40, "visits_counted": 33, "multiplier": 1.0,
                 "min_due": 20, "rule": "band", "provisional": True},
        "variable": 450000.0,
        "carry_in": {"amount": -100000.0, "from_month": "2026-10", "source": "carry_forward"},
        "gross_total": 3350000.0, "total": 3350000.0, "carry_out": 0.0, "owed": 0.0,
    })
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-11-30T15:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30", estimate=november)],
        in_review=[], pipeline=NO_PIPELINE, penalties=[],
        last_statement=_october_statement(owed=0.0, owed_netted_in=None, carry_out=-100000.0),
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    total = lines.index("<b>Total: 3,350,000 UZS</b>")
    assert lines[total - 1] == f"Shortfall from 10.2026: {MINUS}100,000 UZS"
    assert lines[total + 1] == ""
    assert not [line for line in lines if line.startswith("Owed")]


async def test_a_balance_nobody_is_netting_yet_is_owed_from_later_earnings(monkeypatch):
    """After November's close and before its approval: November is under review (no numbers,
    I-14), so `owed_netted_in` is null and the last-statement line promises a LATER deduction."""
    december = _estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 3000000.0, "amount": 0.0, "worked_days": 0, "working_days": 31},
        "commission": {"gross": 0.0, "orders": 0, "after_gate": 0.0},
        "gate": {"compliance_pct": None, "visits_due": 0, "visits_counted": 0, "multiplier": 1.0,
                 "min_due": 20, "rule": "below_min_due", "provisional": True},
        "variable": 0.0, "gross_total": 0.0, "total": 0.0,
    })
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-12-02T06:00:00+00:00",
        open_months=[_open_month("2026-12", end_date="2026-12-31", estimate=december)],
        in_review=[{"month": "2026-11", "status": "closed"}],
        last_statement=_october_statement(owed_netted_in=None),
        pipeline=NO_PIPELINE, penalties=[],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    text = harness.telegram.last_shown().text
    assert text.split("\n")[-3:] == [
        "🔎 11.2026 · Under review",
        "✅ Last statement: 10.2026 · 0 UZS · Approved",
        "Owed: 100,000 UZS. It will be deducted from your later earnings.",
    ]
    assert "Being deducted" not in text


async def test_a_trial_last_statement_says_it_was_not_paid_this_way(monkeypatch):
    """T11-R2 (I-16, I-27): the approved trial month is the last statement until a real month is
    approved. Its line must not read like a paid statement while its push said "(not paid)",
    so the trial month's own note follows it, the words §7.2 already uses for a trial month.
    Only the epoch can be a trial month, so the open month above it is a real one and the note
    is not printed twice."""
    trial = {**AUGUST_STATEMENT, "month": "2026-09", "status": "approved", "is_shadow": True,
             "paid_on": None}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(in_review=[], last_statement=trial))

    await harness.send(ops.tap("staff_sales_earn"))

    text = harness.telegram.last_shown().text
    assert text.split("\n")[-2:] == [
        "✅ Last statement: 09.2026 · 3,590,000 UZS · Approved",
        "Trial month: these numbers are shown for review; pay is made the old way.",
    ]
    assert text.count("Trial month") == 1


async def test_a_trial_month_says_so_under_the_header(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(is_shadow=True)]))

    await harness.send(ops.tap("staff_sales_earn"))

    assert harness.telegram.last_shown().text.split("\n")[:3] == [
        "💰 <b>My earnings — 10.2026</b>",
        "Trial month: these numbers are shown for review; pay is made the old way.",
        "<i>Estimate as of 31.10.2026 20:00. The final amount is set after the month-end review.</i>",
    ]


async def test_review_focus_3_a_sunday_holiday_and_an_unpaid_sunday_print_29_of_30(monkeypatch):
    """Review Focus 3, the bot half: S1's `base` exactly as
    tests/integration/test_sales_pay_seven_day_week_api.py reads it for a Sunday holiday (the 4th)
    and an unpaid Sunday (the 18th): 3,000,000 x 29 / 30 = 2,900,000. The bot prints the published
    days through `earnings.days_worked` and counts none itself."""
    estimate = _estimate(base={"monthly": 3000000.0, "amount": 2900000.0, "worked_days": 29, "working_days": 30})
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=estimate)]))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    assert "Base: 2,900,000 UZS (3,000,000 UZS · 29 of 30 working days)" in lines


async def test_two_open_months_render_the_newest_and_list_the_older(monkeypatch):
    """The days between a month's end and its close (Review Focus 4): November in full,
    October on one line with its own lines button, September under review with none."""
    november = _estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 3000000.0, "amount": 3000000.0, "worked_days": 30, "working_days": 30},
        "commission": {"gross": 36000.0, "orders": 2, "after_gate": 36000.0},
        "gate": {"compliance_pct": 100.0, "visits_due": 4, "visits_counted": 4, "multiplier": 1.0,
                 "min_due": 20, "rule": "below_min_due", "provisional": True},
        "variable": 36000.0, "gross_total": 3036000.0, "total": 3036000.0,
    })
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-11-02T06:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30", estimate=november), _open_month()],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    card = harness.telegram.last_shown()
    assert card.text.split("\n")[0] == "💰 <b>My earnings — 11.2026</b>"
    assert card.text.split("\n\n")[-1].split("\n") == [
        "⏳ 7 orders are waiting for approval, delivery or payment ≈ 84,000 UZS",
        "🧾 10.2026 · Open · ≈ 3,560,000 UZS",
        "🔎 09.2026 · Under review",
        "✅ Last statement: 08.2026 · 3,590,000 UZS · Paid · paid on 05.09.2026",
    ]
    assert card.callback_data() == [
        "staff_sales_earn_l_202611_1", "staff_sales_earn_l_202610_1",
        "staff_sales_earn_p", "staff_sales_earn_x", "staff_sales_earn_s_202608", "staff_sales_earn_h",
        "staff_sales_earn", "staff_profile",
    ]
    assert card.button_labels()[:2] == ["🧾 Credited orders 11.2026", "🧾 10.2026"]
    # I-14: a month under review has no numbers anywhere, and no button.
    assert not [data for data in card.callback_data() if "202609" in data]


@pytest.mark.parametrize("language", LANGUAGES)
async def test_an_older_open_trial_month_is_marked_not_counted(monkeypatch, language):
    """Final-review M5 (I-16): early in November, before the trial October closes, the older
    month's line carries an estimate. The trial month is never paid, so the line says so with
    the backend's published `is_shadow`, never reading as pay due."""
    november = _estimate(**{
        **_NOTHING_ELSE,
        "base": {"monthly": 3000000.0, "amount": 3000000.0, "worked_days": 30, "working_days": 30},
        "commission": {"gross": 36000.0, "orders": 2, "after_gate": 36000.0},
        "variable": 36000.0, "gross_total": 3036000.0, "total": 3036000.0,
    })
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-11-02T06:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30", estimate=november), _open_month(is_shadow=True)],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    (october,) = [line for line in harness.telegram.last_shown().text.split("\n") if line.startswith("🧾 10.2026")]
    marker = _curated("staff.sales.earnings.not_counted_shadow", language)
    assert october.endswith(f" · {marker}"), october
    if language == "en":
        assert october == "🧾 10.2026 · Open · ≈ 3,560,000 UZS · trial month, not counted"


# ---- the tier rows (D-BANDS, §7.2) -----------------------------------------------------------------

@pytest.mark.parametrize("language, expected", [
    ("en", (
        "Commission: 800,000 UZS · 3 orders",
        "   19L: 700 units · 800,000 UZS",
        "      1–500: 500 × 1,000 = 500,000",
        "      501–1,000: 200 × 1,500 = 300,000",
        "      🎯 next tier from unit 1,001 (2,000 UZS per unit): 301 more units",
    )),
    ("ru", (
        "Комиссия: 800,000 сум · заказов: 3",
        "   19 л: 700 шт. · 800,000 сум",
        "      1–500: 500 × 1,000 = 500,000",
        "      501–1,000: 200 × 1,500 = 300,000",
        "      🎯 следующая ступень с 1,001-й шт. (2,000 сум за шт.): ещё 301 шт.",
    )),
])
async def test_the_illustration_c_estimate_prints_each_tier_and_the_next_one(monkeypatch, language, expected):
    """§7.2, Illustration C (T-TIER-2's S1): 700 units fill 1–500 at 1,000 and 501–1,000 at 1,500,
    and the open estimate names the next tier and how far it is. The ranges are built from the
    published bounds, every figure is a field, and the gate row follows the block directly."""
    estimate = _estimate(commission={"gross": 800000, "orders": 3, "after_gate": 640000,
                                     "products": [ILLUSTRATION_C]})
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=estimate)]))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    start = lines.index(expected[0])
    assert tuple(lines[start:start + len(expected)]) == expected
    assert lines[start + len(expected)].startswith(_curated("staff.sales.stats.metric_plan_vs_fact_pct", language))


@pytest.mark.parametrize("language, expected", [
    ("en", (
        "   19L: 700 units · 687,500 UZS",
        "      1–500: 500 × 1,000 = 500,000",
        "      501–1,000: 200 × 1,500 = 300,000 · counted 187,500, less money received",
        "      🎯 next tier from unit 1,001 (2,000 UZS per unit): 301 more units",
    )),
    ("ru", (
        "   19 л: 700 шт. · 687,500 сум",
        "      1–500: 500 × 1,000 = 500,000",
        "      501–1,000: 200 × 1,500 = 300,000 · учтено 187,500, получено меньше денег",
        "      🎯 следующая ступень с 1,001-й шт. (2,000 сум за шт.): ещё 301 шт.",
    )),
])
async def test_a_tier_paid_on_less_money_says_what_it_counted(monkeypatch, language, expected):
    """§7.2, T-TIER-5: C was paid half, so the tier holding its 150 units pays 187,500 rather
    than the 300,000 its units × rate read. The row keeps "units × rate = amount_full" true and
    says what it counted (`amount` ≠ `amount_full`, V5-B10); the untouched tier adds nothing."""
    estimate = _estimate(commission={"gross": 687500, "orders": 3, "after_gate": 550000,
                                     "products": [ILLUSTRATION_C_HALF_PAID]})
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=estimate)]))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    start = lines.index(expected[0])
    assert tuple(lines[start:start + len(expected)]) == expected


@pytest.mark.parametrize("language, expected", [
    ("en", (
        "   Juice 1 L: 110 units · 28,750 UZS",
        "      1–100: 2% of 1,250,000 = 25,000",
        "      101+: 3% of 125,000 = 3,750",
        "   Juice 0.5 L: 90 units · 22,500 UZS",
        "      1–100: 2% of 1,125,000 = 22,500",
        "      🎯 next tier from unit 101 (2.5%): 11 more units",
    )),
    ("uz", (
        "   Sharbat 1 l: 110 dona · 28,750 som",
        "      1–100: 1,250,000 dan 2% = 25,000",
        "      101+: 125,000 dan 3% = 3,750",
        "   Sharbat 0,5 l: 90 dona · 22,500 som",
        "      1–100: 1,125,000 dan 2% = 22,500",
        "      🎯 keyingi pog'ona 101-donadan (2.5%): yana 11 dona",
    )),
])
async def test_percent_tiers_print_their_share_of_the_net(monkeypatch, language, expected):
    """§7.2 with T-TIER-3's figures: a percent tier reads "{value}% of {net} = amount", and a
    percent next tier names its rate as "{value}%". 2.0 prints as 2 and 2.5 as 2.5: the published
    value, never re-scaled."""
    estimate = _estimate(commission={"gross": 51250, "orders": 5, "after_gate": 41000,
                                     "products": [JUICE_TWO_PERCENT_TIERS, JUICE_BELOW_ITS_NEXT_TIER]})
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=estimate)]))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    start = lines.index(expected[0])
    assert tuple(lines[start:start + len(expected)]) == expected


@pytest.mark.parametrize("language, expected", [
    ("en", [f"Corrections from earlier months: {MINUS}320,000 UZS", "   · 10.2026 · 19L: 700 → 400 units"]),
    ("ru", [f"Корректировки за прошлые месяцы: {MINUS}320,000 сум", "   · 10.2026 · 19 л: 700 → 400 шт."]),
])
async def test_a_late_group_names_each_product_it_moved(monkeypatch, language, expected):
    """§7.2 late groups, Illustration C (b): under the corrections row, one row per product the
    group moved, "{month} · {name}: {units_before} → {units}". The gated figure stays on the
    corrections row itself."""
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-11-05T15:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30", estimate=_illustration_c_november())],
        in_review=[], pipeline=NO_PIPELINE, penalties=[],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    start = lines.index(expected[0])
    assert lines[start:start + 2] == expected
    assert not lines[start + 2].startswith("   ·")


@pytest.mark.parametrize("language", LANGUAGES)
async def test_a_trial_month_late_group_is_marked_not_counted(monkeypatch, language):
    """I-16, S1 `late.groups[].counted`: the trial September is approved and never paid, so a
    group re-reading it in November is shown at its own tiers and adds 0 after the gate. Its
    rows carry the trial month's own marker, the words S2 prints on a not-counted line, so a
    "410 → 400 units" that moves no money is explained; the counted October group's row has none."""
    trial = {**SEPTEMBER_GROUP, "counted": False, "after_gate": 0}
    october = _illustration_c_november()["late"]["groups"][0]
    estimate = _estimate(**{**_NOTHING_ELSE, "late": {"after_gate": -320000, "lines": 1, "groups": [trial, october]}})
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-11-05T15:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30", estimate=estimate)],
        in_review=[], pipeline=NO_PIPELINE, penalties=[],
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    shadow, counted = [line for line in lines if LATE_ROW.match(line)]
    marker = _curated("staff.sales.earnings.not_counted_shadow", language)
    assert shadow.endswith(f" · {marker}"), shadow
    assert marker not in counted
    if language == "en":
        assert [shadow, counted] == [
            "   · 09.2026 · 19L: 410 → 400 units · trial month, not counted",
            "   · 10.2026 · 19L: 700 → 400 units",
        ]


async def test_product_names_are_escaped_on_the_summary_and_the_lines(monkeypatch):
    """§7.2 presentation (T-HTML-1): a product name is the backend's text placed in an HTML
    message, on the tier rows, the late rows and the credited-order units alike."""
    names = {"en": "19L <b>promo</b> & co"}
    product = {**ILLUSTRATION_B_PRODUCTS[0], "product_name": names}
    group = {**SEPTEMBER_GROUP, "products": [{**SEPTEMBER_GROUP["products"][0], "product_name": names}]}
    estimate = _estimate(commission={"gross": 600000.0, "orders": 31, "after_gate": 480000.0, "products": [product]},
                         late={"after_gate": -20000.0, "lines": 1, "groups": [group]})
    page = LINES_OCTOBER[("2026-10", 1)]
    credit = {**page["items"][0], "units": [{"product_name": names, "units": 12}]}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=estimate)]))
    harness.backend.route("GET", LINES, lambda _c: {**page, "has_more": False, "items": [credit]})

    await harness.send(ops.tap("staff_sales_earn"))
    summary = harness.telegram.last_shown().text
    await harness.send(ops.tap("staff_sales_earn_l_202610_1"))
    listed = harness.telegram.last_shown().text

    assert "   19L &lt;b&gt;promo&lt;/b&gt; &amp; co: 500 units · 600,000 UZS" in summary.split("\n")
    assert "   · 09.2026 · 19L &lt;b&gt;promo&lt;/b&gt; &amp; co: 410 → 400 units" in summary.split("\n")
    assert "   19L &lt;b&gt;promo&lt;/b&gt; &amp; co × 12 · Commission +18,000 UZS" in listed.split("\n")
    assert "<b>promo" not in summary + listed


# ---- the lists ----------------------------------------------------------------------------------

async def test_the_credited_orders_page_through_the_backends_lines(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())
    harness.backend.route("GET", LINES, _lines_route)

    await harness.send(ops.tap("staff_sales_earn"))
    await harness.send(ops.tap("staff_sales_earn_l_202610_1"))

    first = harness.telegram.last_shown()
    assert first.text == LINES_PAGE_1_EN
    # Next only while the backend says there is more; no previous page on page 1.
    assert first.callback_data() == ["staff_sales_earn_l_202610_2", "staff_sales_earn"]
    assert first.button_labels() == [f"{_curated('staff.sales.list.next')} ▶️", "⬅️ Back"]

    await harness.send(ops.tap("staff_sales_earn_l_202610_2"))

    second = harness.telegram.last_shown()
    assert second.text == LINES_PAGE_2_EN
    assert second.callback_data() == ["staff_sales_earn_l_202610_1", "staff_sales_earn"]
    assert second.button_labels() == [f"◀️ {_curated('staff.sales.list.prev')}", "⬅️ Back"]

    await harness.send(ops.tap("staff_sales_earn"))

    assert harness.telegram.last_shown().text == SUMMARY_EN
    assert [c.params for c in _calls(harness, "GET", LINES)] == [
        {"month": "2026-10", "page": 1}, {"month": "2026-10", "page": 2},
    ]


NOVEMBER_WITH_A_TRIAL_CORRECTION = {
    "month": "2026-11", "status": "open", "available": True, "page": 1, "has_more": False,
    "items": [
        # Credited in the trial October, returned on 5 November after October closed: the sync
        # writes the reversal into November, and S2 publishes it with `counted: false`.
        {"row": "order", "id": 377, "date": "2026-11-05", "order_number": "SA_000377_26",
         "outlet_name": "Oasis market", "earned_month": "2026-10", "is_late": True, "counted": False,
         "kind": "commission_reversal", "cause": "not_delivered", "tier_shift": False,
         "units": [{"product_name": NAMES_19L, "units": 12}], "amount": -18000},
        {"row": "order", "id": 420, "date": "2026-11-06", "order_number": "SA_000420_26",
         "outlet_name": "Baraka", "earned_month": "2026-11", "is_late": False, "counted": True,
         "kind": "commission_credit", "cause": None, "tier_shift": False,
         "units": [{"product_name": NAMES_19L, "units": 6}], "amount": 9000},
    ],
}


@pytest.mark.parametrize("language", LANGUAGES)
async def test_a_trial_month_correction_listed_in_november_is_marked_not_counted(monkeypatch, language):
    """Final-review I2: S2 publishes `counted`, and a line it marks false does not change this
    month's pay (the estimate excludes it). The bot says so on that line, and only on it."""
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", LINES, lambda _c: NOVEMBER_WITH_A_TRIAL_CORRECTION)

    await harness.send(ops.tap("staff_sales_earn_l_202611_1"))

    lines = harness.telegram.last_shown().text.split("\n")
    reversal, credit = lines[3], lines[5]
    marker = _curated("staff.sales.earnings.not_counted_shadow", language)
    assert reversal.endswith(f" · {marker}"), reversal
    assert marker not in credit
    if language == "en":
        assert lines[2:] == [
            "05.11 · SA_000377_26 · Oasis market",
            f"   19L × 12 · Reversal {MINUS}18,000 UZS · order no longer delivered · for 10.2026 · trial month, not counted",
            "06.11 · SA_000420_26 · Baraka",
            "   19L × 6 · Commission +9,000 UZS",
        ]


@pytest.mark.parametrize("language, expected", [
    ("en", [
        "🧾 <b>Credited orders — 11.2026</b> · Page 1",
        "",
        "05.11 · SA_000501_26 · Oasis market",
        f"   19L × 1 · Commission change {MINUS}299,000 UZS · fewer units on the order · for 10.2026",
        "SA_000502_26 · Baraka",
        f"   19L × 250 · Tier change {MINUS}25,000 UZS · for 10.2026",
        "SA_000503_26 · Chorsu",
        f"   19L × 150 · Tier change {MINUS}75,000 UZS · for 10.2026",
    ]),
    ("ru", [
        "🧾 <b>Начисленные заказы — 11.2026</b> · Страница 1",
        "",
        "05.11 · SA_000501_26 · Oasis market",
        f"   19 л × 1 · Изменение комиссии {MINUS}299,000 сум · в заказе меньше единиц · за 10.2026",
        "SA_000502_26 · Baraka",
        f"   19 л × 250 · Смена ступени {MINUS}25,000 сум · за 10.2026",
        "SA_000503_26 · Chorsu",
        f"   19 л × 150 · Смена ступени {MINUS}75,000 сум · за 10.2026",
    ]),
])
async def test_a_removed_bottle_and_the_orders_it_moved_read_as_the_spec_prints_them(monkeypatch, language, expected):
    """§10.4 (OQ-B3), T-TIER-20 (e): the `units_reduced` row prints the units still counted and
    its cause; a tier-change row prints its units and "Tier change", with no date, because the
    order itself did not change."""
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", LINES, lambda _c: LINES_NOVEMBER_UNITS_REDUCED)

    await harness.send(ops.tap("staff_sales_earn_l_202611_1"))

    screen = harness.telegram.last_shown()
    assert screen.text.split("\n") == expected
    assert [c.params for c in _calls(harness, "GET", LINES)] == [{"month": "2026-11", "page": 1}]
    assert screen.callback_data() == ["staff_sales_earn"]


async def test_a_late_reversal_lists_the_units_it_took_out_and_a_tier_change_has_no_date(monkeypatch):
    """§7.2, Illustration C (b)'s November list, byte for byte."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", LINES, lambda _c: LINES_NOVEMBER_REVERSAL)

    await harness.send(ops.tap("staff_sales_earn_l_202611_1"))

    assert harness.telegram.last_shown().text.split("\n")[2:] == [
        "05.11 · SA_000501_26 · Oasis market",
        f"   19L × 300 · Reversal {MINUS}300,000 UZS · no payment left on the order · for 10.2026",
        "SA_000502_26 · Baraka",
        f"   19L × 250 · Tier change {MINUS}25,000 UZS · for 10.2026",
    ]


async def test_a_months_lines_tapped_after_its_close_say_it_is_under_review_and_raise_no_error(monkeypatch):
    """Review Focus 4, bot half. The button was drawn while October was open; October closed
    before the tap, so S2 publishes nothing for it (I-14) and the bot says why, from S2's own
    `status` and `available`: the month is under review and its orders come back once it is
    approved. No figure and no alert."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        as_of="2026-11-02T06:00:00+00:00",
        open_months=[_open_month("2026-11", end_date="2026-11-30"), _open_month()],
    ))
    await harness.send(ops.tap("staff_sales_earn"))
    assert "staff_sales_earn_l_202610_1" in harness.telegram.last_shown().callback_data()

    harness.backend.route("GET", LINES, lambda _c: {
        "month": "2026-10", "status": "closed", "available": False, "page": 1, "has_more": False, "items": [],
    })
    harness.telegram.reset()
    await harness.send(ops.tap("staff_sales_earn_l_202610_1"))

    screen = harness.telegram.last_shown()
    assert screen.text == "\n".join((
        "🧾 <b>Credited orders — 10.2026</b> · Page 1",
        "",
        "10.2026 is under review — its orders appear after approval.",
    ))
    assert screen.callback_data() == ["staff_sales_earn"]
    assert _alerts(harness) == []


@pytest.mark.parametrize("answer, expected", [
    # The dev September (Task 1 test (a)): approved, available, and nothing was credited in it.
    ({"month": "2026-09", "status": "approved", "available": True, "page": 1, "has_more": False, "items": []},
     "No credited orders or new-outlet bonuses in 09.2026."),
    ({"month": "2026-09", "status": "paid", "available": True, "page": 1, "has_more": False, "items": []},
     "No credited orders or new-outlet bonuses in 09.2026."),
    # No period at all for the month: S2 knows nothing to say about it, so neither does the bot.
    ({"month": "2026-09", "status": None, "available": False, "page": 1, "has_more": False, "items": []},
     "Nothing here yet."),
], ids=["approved", "paid", "no_period"])
async def test_an_empty_credited_orders_page_says_why_from_s2s_own_fields(monkeypatch, answer, expected):
    """S2 publishes `status` and `available`, so an empty page names its reason rather than the
    generic "Nothing here yet.": a month that is readable and empty had no credited order or
    new-outlet bonus in it. Only a month S2 has no period for keeps the generic sentence."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", LINES, lambda _c: answer)

    await harness.send(ops.tap("staff_sales_earn_l_202609_1"))

    screen = harness.telegram.last_shown()
    assert screen.text == f"🧾 <b>Credited orders — 09.2026</b> · Page 1\n\n{expected}"
    assert screen.callback_data() == ["staff_sales_earn"]
    assert [c.params for c in _calls(harness, "GET", LINES)] == [{"month": "2026-09", "page": 1}]


async def test_the_russian_empty_page_names_its_orders_as_its_title_does(monkeypatch):
    """The Russian sentence uses the title's own word: "Начисленные заказы" above, "начисленных
    заказов" below, so an empty page never reads as a second kind of order."""
    harness, ops = await _staff(monkeypatch, language="ru")
    harness.backend.route("GET", LINES, lambda _c: {
        "month": "2026-09", "status": "approved", "available": True, "page": 1, "has_more": False, "items": [],
    })

    await harness.send(ops.tap("staff_sales_earn_l_202609_1"))

    assert harness.telegram.last_shown().text == (
        "🧾 <b>Начисленные заказы — 09.2026</b> · Страница 1\n\n"
        "В 09.2026 нет начисленных заказов и бонусов за новые точки."
    )


async def test_the_pipeline_and_penalties_screens_read_the_same_answer(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())

    await harness.send(ops.tap("staff_sales_earn"))
    await harness.send(ops.tap("staff_sales_earn_p"))

    pipeline = harness.telegram.last_shown()
    assert pipeline.text == PIPELINE_EN
    assert pipeline.callback_data() == ["staff_sales_earn"]

    await harness.send(ops.tap("staff_sales_earn_x"))

    penalties = harness.telegram.last_shown()
    assert penalties.text == PENALTIES_EN
    assert penalties.callback_data() == ["staff_sales_earn"]
    # No route of their own (§7.2): each screen re-reads S1.
    assert len(_calls(harness, "GET", EARNINGS)) == 3
    assert not _calls(harness, "GET", LINES)


async def test_the_pipeline_names_the_orders_the_backend_did_not_list(monkeypatch):
    """T11-R1 (spec T-PIPE-1): the backend lists the 20 newest waiting orders (`PIPELINE_SIZE`)
    and its `count` covers every one. With 25 waiting, the screen shows the 20 it was handed
    and says the other 5 exist, instead of reading as the whole list."""
    pipeline = {"count": 25, "estimated_total": 300000.0, "items": [
        {"order_number": f"SA_{700 + index:06d}_26", "outlet_name": "Oasis market",
         "placed_on": "2026-10-29", "waiting_for": "delivery", "total": 100000.0,
         "estimated_commission": 12000.0}
        for index in range(20)
    ]}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(pipeline=pipeline))

    await harness.send(ops.tap("staff_sales_earn_p"))

    screen = harness.telegram.last_shown().text.split("\n")
    assert len([line for line in screen if line.startswith("SA_")]) == 20
    assert screen[-1] == "+5 more"


async def test_a_pipeline_order_without_an_estimate_prints_no_figure(monkeypatch):
    """T11-R3: with no terms in force (`configured: false`) no plan resolves, so the backend
    publishes `estimated_commission: null`, and the pipeline button is still drawn. The row
    names the order and its wait and prints no "≈" figure: "≈ 0 UZS" would be an estimate the
    backend never made."""
    pipeline = {"count": 2, "estimated_total": 0.0, "items": [
        {**item, "estimated_commission": None} for item in PIPELINE["items"]
    ]}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        open_months=[_open_month(configured=False)], pipeline=pipeline,
    ))

    await harness.send(ops.tap("staff_sales_earn"))
    assert "staff_sales_earn_p" in harness.telegram.last_shown().callback_data()
    await harness.send(ops.tap("staff_sales_earn_p"))

    assert harness.telegram.last_shown().text == "\n".join((
        "⏳ <b>Not counted yet</b>",
        "<i>Commission is counted once an order is both delivered and paid. Amounts are estimates.</i>",
        "",
        "SA_000430_26 · Oasis market · waiting for delivery",
        "SA_000431_26 · Baraka · waiting for manager approval",
    ))


async def test_an_empty_list_says_nothing_here_yet(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(pipeline=NO_PIPELINE, penalties=[]))

    await harness.send(ops.tap("staff_sales_earn_p"))
    assert harness.telegram.last_shown().text.split("\n")[-1] == "Nothing here yet."

    await harness.send(ops.tap("staff_sales_earn_x"))
    assert harness.telegram.last_shown().text == "⚠️ <b>Penalties</b>\n\nNothing here yet."


async def test_backend_text_is_escaped_and_long_lists_are_cut(monkeypatch):
    """T-HTML-1: an adjustment reason is whatever an admin typed, and every screen here is
    sent with parse_mode=HTML. A raw tag would either inject a link or make Telegram refuse
    the whole card. Four adjustments show three, each cut to 80 characters, then "+1 more";
    a pipeline of 25 shows 20 and counts the rest."""
    adjustments = {"amount": 30000.0, "items": [
        {"amount": 10000.0, "reason": '<a href="https://x">Open</a> & <b>'},
        {"amount": 5000.0, "reason": "x" * 100},
        {"amount": 7000.0, "reason": "Fuel"},
        {"amount": 8000.0, "reason": "Phone top-up"},
    ]}
    pipeline = {"count": 25, "estimated_total": 300000.0, "items": [
        {"order_number": f"SA_{500 + index:06d}_26", "outlet_name": "Oasis <market> & Co",
         "placed_on": "2026-10-29", "waiting_for": "payment", "total": 100000.0,
         "estimated_commission": 12000.0}
        for index in range(25)
    ]}
    penalties = [{**PENALTIES[0], "type_names": {"en": "Late <b>check-in</b>", "uz": "Kech", "ru": "Поздно"},
                  "reason": "Shop said: <i>closed</i> & gone"}]
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(
        open_months=[_open_month(estimate=_estimate(adjustments=adjustments))],
        pipeline=pipeline, penalties=penalties,
    ))

    await harness.send(ops.tap("staff_sales_earn"))

    lines = harness.telegram.last_shown().text.split("\n")
    assert "Adjustments: +30,000 UZS" in lines
    assert '   · +10,000 UZS — &lt;a href="https://x"&gt;Open&lt;/a&gt; &amp; &lt;b&gt;' in lines
    assert "   · +5,000 UZS — " + "x" * 80 in lines
    assert "   · +7,000 UZS — Fuel" in lines
    assert "   · +1 more" in lines
    assert "Phone top-up" not in "\n".join(lines)
    assert "<a href" not in "\n".join(lines)

    await harness.send(ops.tap("staff_sales_earn_p"))

    screen = harness.telegram.last_shown().text.split("\n")
    rows = [line for line in screen if line.startswith("SA_")]
    assert len(rows) == 20
    assert rows[0] == "SA_000500_26 · Oasis &lt;market&gt; &amp; Co · waiting for payment ≈ 12,000 UZS"
    assert screen[-1] == "+5 more"

    await harness.send(ops.tap("staff_sales_earn_x"))

    penalty = harness.telegram.last_shown().text.split("\n")
    assert penalty[2] == f"14.10.2026 · Late &lt;b&gt;check-in&lt;/b&gt; · {MINUS}150,000 UZS"
    assert penalty[4] == "   Reason: Shop said: &lt;i&gt;closed&lt;/i&gt; &amp; gone"


async def test_a_list_past_telegrams_limit_is_cut_from_the_end(monkeypatch):
    """Telegram refuses a message over 4096 characters. Names are the backend's, 200 characters
    each here, so the list is cut from the END and the cut is counted, never a mid-line cut."""
    pipeline = {"count": 20, "estimated_total": 240000.0, "items": [
        {"order_number": f"SA_{600 + index:06d}_26", "outlet_name": "Ö" * 200, "placed_on": "2026-10-29",
         "waiting_for": "payment", "total": 100000.0, "estimated_commission": 12000.0}
        for index in range(20)
    ]}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(pipeline=pipeline))

    await harness.send(ops.tap("staff_sales_earn_p"))

    text = harness.telegram.last_shown().text
    rows = [line for line in text.split("\n") if line.startswith("SA_")]
    assert len(text) <= 4096
    assert 0 < len(rows) < 20
    assert text.split("\n")[-1] == f"+{20 - len(rows)} more"


@pytest.mark.parametrize("language", ("en", "ru"))
async def test_review_focus_5_a_busy_month_collapses_its_tier_rows_before_cutting_anything(monkeypatch, language):
    """Review Focus 5, §7.2 "Length", first step. With 60-character names the full card is over
    Telegram's 4,096 and the card without its tier rows is under it. So the tier rows (and the
    🎯 rows) go and nothing else does: all six product lines, all 24 late rows in order, and no
    "+N more" row. The payload HAS tier rows, so their absence is the collapse."""
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=_busy_month(60))]))

    await harness.send(ops.tap("staff_sales_earn"))

    text = harness.telegram.last_shown().text
    lines = text.split("\n")
    assert len(text) <= 4096
    assert not [line for line in lines if line.startswith("      ")]
    assert "🎯" not in text
    assert len([line for line in lines if line.startswith(f"   {PRODUCT_WORD[language]} ")]) == 6
    assert [line for line in lines if LATE_ROW.match(line)] == _busy_late_rows(language, 60)
    assert not [line for line in lines if MORE_ROW.match(line)]
    assert _alerts(harness) == []


@pytest.mark.parametrize("language", ("en", "ru"))
async def test_review_focus_5_a_month_still_too_long_cuts_late_rows_from_the_end_and_counts_them(monkeypatch, language):
    """Review Focus 5, §7.2 "Length", second step. With 150-character names even the collapsed
    card is over 4,096, so the late rows, the last list on the card, are cut from the END, whole
    rows only, and the cut is counted under the rows kept. The six product lines and every
    formula row stay, and Telegram is never handed a message it refuses ("message is too
    long")."""
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=_busy_month(150))]))

    await harness.send(ops.tap("staff_sales_earn"))

    text = harness.telegram.last_shown().text
    lines = text.split("\n")
    late = [line for line in lines if LATE_ROW.match(line)]
    assert len(text) <= 4096
    assert not [line for line in lines if line.startswith("      ")]
    assert len([line for line in lines if line.startswith(f"   {PRODUCT_WORD[language]} ")]) == 6
    assert 0 < len(late) < 24
    assert late == _busy_late_rows(language, 150)[:len(late)]
    cut = 24 - len(late)
    below = lines.index(late[-1]) + 1
    assert lines[below] == {"en": f"   · +{cut} more", "ru": f"   · ещё {cut}"}[language]
    assert lines[below + 1] == {"en": "New outlets: +200,000 UZS · 2", "ru": "Новые точки: +200,000 сум · 2"}[language]
    assert {"en": "<b>Total: 3,560,000 UZS</b>", "ru": "<b>Итого: 3,560,000 сум</b>"}[language] in lines
    assert _alerts(harness) == []


def _many_products(name_length, count=30):
    """`count` products on one per-unit tier each, every name `name_length` characters long."""
    return [_product(index, _busy_names(index, name_length), 40, 30000,
                     [_tier(1, None, 40, "per_unit", 750.0, 30000, net=800000.0)])
            for index in range(1, count + 1)]


@pytest.mark.parametrize("language", ("en", "ru"))
async def test_review_focus_5_a_month_with_more_products_than_fit_cuts_product_lines_and_counts_them(
        monkeypatch, language):
    """§7.2 "Length", third step. Thirty products with 150-character names are past 4,096 even
    as product lines alone, so once the one late row is cut (the late rows go first) the product
    lines are cut from the END, whole rows only, and the cut is counted in "+N more" right under
    the ones kept, above the plan-vs-fact row. Every formula row stays."""
    estimate = _estimate(commission={"gross": 900000.0, "orders": 30, "after_gate": 720000.0,
                                     "products": _many_products(150)})
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(open_months=[_open_month(estimate=estimate)]))

    await harness.send(ops.tap("staff_sales_earn"))

    text = harness.telegram.last_shown().text
    lines = text.split("\n")
    units = {"en": "40 units · 30,000 UZS", "ru": "40 шт. · 30,000 сум"}[language]
    expected = [f"   {_busy_names(index, 150)[language]}: {units}" for index in range(1, 31)]
    kept = [line for line in lines if line.startswith(f"   {PRODUCT_WORD[language]} ")]
    commission = lines.index(f"{_curated('staff.sales.earnings.commission', language)}: "
                             f"{ {'en': '900,000 UZS · 30 orders', 'ru': '900,000 сум · заказов: 30'}[language] }")
    assert len(text) <= 4096
    assert 0 < len(kept) < 30
    assert lines[commission + 1:commission + 1 + len(kept)] == expected[:len(kept)]
    assert lines[commission + 1 + len(kept)] == {"en": f"   +{30 - len(kept)} more",
                                                 "ru": f"   ещё {30 - len(kept)}"}[language]
    assert lines[commission + 2 + len(kept)].startswith(
        _curated("staff.sales.stats.metric_plan_vs_fact_pct", language))
    late = lines.index({"en": f"Corrections from earlier months: {MINUS}20,000 UZS",
                        "ru": f"Корректировки за прошлые месяцы: {MINUS}20,000 сум"}[language])
    assert lines[late + 1] == {"en": "   · +1 more", "ru": "   · ещё 1"}[language]
    assert not [line for line in lines if LATE_ROW.match(line) or line.startswith("      ")]
    assert {"en": "<b>Total: 3,560,000 UZS</b>", "ru": "<b>Итого: 3,560,000 сум</b>"}[language] in lines
    assert _alerts(harness) == []


async def test_the_length_rule_stops_once_every_product_row_is_cut(monkeypatch):
    """§7.2 "Length", the loop's exit: the rows it may cut are the per-product ones, and once all
    of them are gone a further cut changes nothing, so it stops there instead of spinning. Here
    the footer alone is past 4,096 (200 months under review, a shape S1 never publishes, built
    only to reach the exit), so the card is still over the limit: what this pins is that the
    screen is drawn, with every product line and late row counted, never removed silently."""
    in_review = [{"month": f"{2000 + index // 12}-{index % 12 + 1:02d}", "status": "closed"} for index in range(200)]
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(in_review=in_review))

    await harness.send(ops.tap("staff_sales_earn"))

    text = harness.telegram.last_shown().text
    lines = text.split("\n")
    commission = lines.index("Commission: 850,000 UZS · 31 orders")
    assert len(text) > 4096
    assert lines[commission + 1:commission + 3] == ["   +2 more", "Plan vs fact: 75.5% · 160 of 212 visits → ×0.8"]
    late = lines.index(f"Corrections from earlier months: {MINUS}20,000 UZS")
    assert lines[late + 1:late + 3] == ["   · +1 more", "New outlets: +200,000 UZS · 2"]
    assert "<b>Total: 3,560,000 UZS</b>" in lines
    assert len([line for line in lines if line.startswith("🔎 ")]) == 200
    assert _alerts(harness) == []


async def test_the_russian_card_reads_sum_and_a_true_minus(monkeypatch):
    """Illustration B in Russian: money in сум with the true minus, the tier rows in шт., the
    juice named in English because the backend published no Russian name (§7.2's fallback), and
    each credited order's units before its kind."""
    harness, ops = await _staff(monkeypatch, language="ru")
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())
    harness.backend.route("GET", LINES, _lines_route)

    await harness.send(ops.tap("staff_sales_earn"))

    card = harness.telegram.last_shown().text
    assert "UZS" not in card
    lines = card.split("\n")
    commission = lines.index("Комиссия: 850,000 сум · заказов: 31")
    assert lines[commission + 1:commission + 6] == [
        "   19 л: 500 шт. · 600,000 сум",
        "      1–300: 300 × 1,000 = 300,000",
        "      301+: 200 × 1,500 = 300,000",
        "   Juice 1 L: 500 шт. · 250,000 сум",
        "      1+: 2% от 12,500,000 = 250,000",
    ]
    late = lines.index(f"Корректировки за прошлые месяцы: {MINUS}20,000 сум")
    assert lines[late + 1] == "   · 09.2026 · 19 л: 410 → 400 шт."
    assert f"Штрафы: {MINUS}150,000 сум · 1" in lines
    assert "<b>Итого: 3,560,000 сум</b>" in lines

    await harness.send(ops.tap("staff_sales_earn_l_202610_1"))

    first = harness.telegram.last_shown().text.split("\n")
    assert first[0] == "🧾 <b>Начисленные заказы — 10.2026</b> · Страница 1"
    assert "   19 л × 12 · Комиссия +18,000 сум" in first
    assert f"   19 л × 10 · Сторно {MINUS}20,000 сум · по заказу не осталось оплаты · за 09.2026" in first

    await harness.send(ops.tap("staff_sales_earn_l_202610_2"))

    second = harness.telegram.last_shown().text.split("\n")
    assert f"   19 л × 6 · Изменение комиссии {MINUS}2,250 сум · получено меньше денег · за 09.2026" in second


# ---- the statements (S3, S4) --------------------------------------------------------------------

async def test_the_statement_button_opens_the_months_statement(monkeypatch):
    """The last statement is the trial September (the dev shape). Its "📄 Statement 09.2026"
    button opens that month's Statement, S4, edited in place: the summary's own rows for the
    frozen month (base with its days, the commission of 0, plan vs fact, the penalty, the total),
    no "Estimate as of" line, and the trial note under the title, as the summary prints a trial
    month's. The month rides in S4's path, exactly as S1 published it."""
    trial = {**AUGUST_STATEMENT, "month": "2026-09", "status": "approved", "paid_on": None, "is_shadow": True,
             "base": 1150000.0, "gated_commission": 0.0, "new_outlets": 0.0, "penalties": 100000.0,
             "total": 1050000.0}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings(in_review=[], last_statement=trial))
    harness.backend.route("GET", f"{STATEMENTS}/2026-09", lambda _c: _trial_september())

    await harness.send(ops.tap("staff_sales_earn"))
    card = harness.telegram.last_shown()
    statement_button = card.button_labels().index("📄 Statement 09.2026")
    assert card.callback_data()[statement_button] == "staff_sales_earn_s_202609"
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_s_202609"))

    assert _statement_calls(harness) == [(f"{STATEMENTS}/2026-09", None)]
    assert harness.telegram.of("sendMessage") == []
    (screen,) = harness.telegram.of("editMessageText")
    assert screen.text == TRIAL_STATEMENT_EN
    assert screen.params["parse_mode"] == "HTML"
    assert screen.button_labels() == ["🧾 Credited orders 09.2026", "📚 Past statements", "⬅️ Back"]
    assert screen.callback_data() == ["staff_sales_earn_l_202609_1", "staff_sales_earn_h", "staff_sales_earn"]


async def test_a_commission_months_statement_prints_its_tiers_corrections_and_adjustment(monkeypatch):
    """Illustration B's October, approved and paid: every row the live estimate printed for it
    (§7.2), the tier rows and the adjustment's reason among them, under "Paid · paid on", the
    words the last-statement line uses."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", f"{STATEMENTS}/2026-10", lambda _c: OCTOBER_STATEMENT)

    await harness.send(ops.tap("staff_sales_earn_s_202610"))

    screen = harness.telegram.last_shown()
    assert screen.text == OCTOBER_STATEMENT_EN
    assert screen.button_labels() == STATEMENT_BUTTONS
    assert screen.callback_data() == STATEMENT_CALLBACKS
    assert _statement_calls(harness) == [(f"{STATEMENTS}/2026-10", None)]
    # The same rows the summary drew for the month while it was open: one renderer, two screens.
    rows = SUMMARY_EN.split("\n")
    first, total = rows.index(OCTOBER_STATEMENT_EN.split("\n")[2]), rows.index("<b>Total: 3,560,000 UZS</b>")
    assert screen.text.split("\n")[2:] == rows[first:total + 1]


async def test_the_statement_screens_buttons_open_its_orders_past_statements_and_the_summary(monkeypatch):
    """Each of the three buttons under a Statement lands where it says: that month's Credited
    orders (S2, page 1), Past statements (S3) and Back to the summary (S1)."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())
    harness.backend.route("GET", f"{STATEMENTS}/2026-10", lambda _c: OCTOBER_STATEMENT)
    harness.backend.route("GET", STATEMENTS, lambda _c: PAST_STATEMENTS)
    harness.backend.route("GET", LINES, _lines_route)

    for data, expected in zip(STATEMENT_CALLBACKS, (LINES_PAGE_1_EN, PAST_STATEMENTS_EN, SUMMARY_EN)):
        await harness.send(ops.tap("staff_sales_earn_s_202610"))
        assert harness.telegram.last_shown().callback_data() == STATEMENT_CALLBACKS
        await harness.send(ops.tap(data))
        assert harness.telegram.last_shown().text == expected, data

    assert [c.params for c in _calls(harness, "GET", LINES)] == [{"month": "2026-10", "page": 1}]
    statement = (f"{STATEMENTS}/2026-10", None)
    assert _statement_calls(harness) == [statement, statement, (STATEMENTS, None), statement]
    assert len(_calls(harness, "GET", EARNINGS)) == 1


async def test_past_statements_lists_each_month_and_opens_the_one_tapped(monkeypatch):
    """S3's months, newest first, one button each: "MM.YYYY · total · status", the words the
    last-statement line uses, and the trial month carrying the marker the summary gives a trial
    month. A tap opens that month's Statement in place."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", STATEMENTS, lambda _c: PAST_STATEMENTS)
    harness.backend.route("GET", f"{STATEMENTS}/2026-09", lambda _c: _trial_september())

    await harness.send(ops.tap("staff_sales_earn_h"))

    listing = harness.telegram.last_shown()
    assert listing.text == PAST_STATEMENTS_EN
    assert listing.button_labels() == [
        "10.2026 · 3,560,000 UZS · Paid",
        "09.2026 · 1,050,000 UZS · Approved · trial month, not counted",
        "⬅️ Back",
    ]
    assert listing.callback_data() == ["staff_sales_earn_s_202610", "staff_sales_earn_s_202609", "staff_sales_earn"]
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_s_202609"))

    assert harness.telegram.of("sendMessage") == []
    (screen,) = harness.telegram.of("editMessageText")
    assert screen.text == TRIAL_STATEMENT_EN
    assert _statement_calls(harness) == [(STATEMENTS, None), (f"{STATEMENTS}/2026-09", None)]


async def test_past_statements_with_none_approved_says_so(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", STATEMENTS, lambda _c: {"items": []})

    await harness.send(ops.tap("staff_sales_earn_h"))

    screen = harness.telegram.last_shown()
    assert screen.text == "📚 <b>Past statements</b>\n\nNo approved statements yet."
    assert screen.callback_data() == ["staff_sales_earn"]


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_statement_screens_read_in_the_agents_language(monkeypatch, language):
    """The new rows are seeded in all three languages and reach the screens as the agent's
    words, never a humanised key: the title, the Past statements button and screen."""
    harness, ops = await _staff(monkeypatch, language=language)
    harness.backend.route("GET", f"{STATEMENTS}/2026-10", lambda _c: OCTOBER_STATEMENT)
    harness.backend.route("GET", STATEMENTS, lambda _c: {"items": []})

    await harness.send(ops.tap("staff_sales_earn_s_202610"))
    screen = harness.telegram.last_shown()
    title = _rendered("staff.sales.earnings.statement_title", language, month="10.2026")
    status = _curated("staff.sales.earnings.status.paid", language)
    paid_on = _rendered("staff.sales.earnings.paid_on", language, date="05.11.2026")
    assert screen.text.split("\n")[0] == f"📄 <b>{title}</b> · {status} · {paid_on}"
    past = _curated("staff.sales.earnings.past_statements", language)
    assert screen.button_labels()[1] == f"📚 {past}"

    await harness.send(ops.tap("staff_sales_earn_h"))
    empty = _curated("staff.sales.earnings.past_empty", language)
    assert harness.telegram.last_shown().text == f"📚 <b>{past}</b>\n\n{empty}"
    if language == "ru":
        assert title == "Расчётный лист — 10.2026"


# The one-balance-sentence rule (controller ruling, Task 1 review). The carry case's October and the
# leaver's: paid 0; the employed agent carries 100,000 forward, the leaver owes it.
@pytest.mark.parametrize("head, statement, sentence", [
    # The latest statement: what it says will happen next is still true (`format_pay_balance_note`).
    ({"is_latest": True}, _carry_case_frozen(carry_out=-100000.0, owed=0.0),
     "Shortfall of 100,000 UZS will be deducted next month."),
    # An open estimate is netting the leaver's balance (`owed_netted_in`): it says where, and the
    # row's own "deducted from your later earnings" is not printed beside it.
    ({"is_latest": True, "owed": 100000.0, "owed_netted_in": "2026-11"},
     _carry_case_frozen(carry_out=0.0, owed=100000.0),
     "Owed: 100,000 UZS. Being deducted in 11.2026."),
    # An older statement: its balance has since been carried or netted, so no forward-looking
    # sentence, and its rows have no owed row: the next statement shows it as its carry-in row.
    ({"is_latest": False, "owed": 100000.0}, _carry_case_frozen(carry_out=0.0, owed=100000.0), None),
], ids=["latest_carries", "netted", "older_owed"])
async def test_a_statement_prints_at_most_one_balance_sentence(monkeypatch, head, statement, sentence):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", f"{STATEMENTS}/2026-10", lambda _c: _statement(statement=statement, **head))

    await harness.send(ops.tap("staff_sales_earn_s_202610"))

    lines = harness.telegram.last_shown().text.split("\n")
    assert lines == ["📄 <b>Statement — 10.2026</b> · Approved", "", *CARRY_CASE_STATEMENT_ROWS,
                     *([sentence] if sentence else [])]
    balance = [line for line in lines if line.startswith(("Shortfall of ", "Owed: "))]
    assert balance == ([sentence] if sentence else [])


async def test_a_busy_months_statement_fits_one_message_as_the_summary_does(monkeypatch):
    """Review Focus 5's busy month, frozen: the Statement prints the summary's rows, so it is
    fitted by the summary's own "Length" rule. With 60-character names the tier rows collapse to
    their product lines and nothing else goes."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", f"{STATEMENTS}/2026-10", lambda _c: _statement(statement=_busy_month(60)))

    await harness.send(ops.tap("staff_sales_earn_s_202610"))

    text = harness.telegram.last_shown().text
    lines = text.split("\n")
    assert len(text) <= 4096
    assert not [line for line in lines if line.startswith("      ")]
    assert len([line for line in lines if line.startswith("   Product ")]) == 6
    assert [line for line in lines if LATE_ROW.match(line)] == _busy_late_rows("en", 60)
    assert not [line for line in lines if MORE_ROW.match(line)]
    assert _alerts(harness) == []


@pytest.mark.parametrize("data, drawn, untouched", [
    # A Statement or Past statements button: the summary is drawn in the screen's place.
    ("staff_sales_earn_s_202608", "editMessageText", "sendMessage"),
    # The approval push's button: the summary arrives as a NEW message, and the push stays (M9).
    ("staff_sales_earn_sn_202608", "sendMessage", "editMessageText"),
], ids=["statement_button", "push_button"])
async def test_a_statement_the_backend_will_not_open_says_why_and_lands_on_the_summary(
        monkeypatch, data, drawn, untouched):
    """A stale button: S4 answers 404 SALES_PAY_STATEMENT_NOT_AVAILABLE for a month that is not
    approved. The tap shows the mapped copy (nothing answered it yet, so it is the popup) and the
    summary, re-read, takes the failed screen's place rather than leaving the agent on the button
    that failed: edited in place under a screen's own button, sent as a new message under the
    push's, which is never written over."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())
    harness.backend.route("GET", f"{STATEMENTS}/2026-08", lambda _c: staff_backend_failure(
        "There is no approved statement for this month", 404,
        error_code="SALES_PAY_STATEMENT_NOT_AVAILABLE", details={"month": "2026-08"},
    ))
    await harness.send(ops.tap("staff_sales_earn"))
    harness.telegram.reset()

    await harness.send(ops.tap(data))

    assert [call.params.get("text") for call in harness.telegram.visible_answers] == [
        f"❌ {_curated('staff.sales.earnings.statement_unavailable')}",
    ]
    (screen,) = harness.telegram.of(drawn)
    assert screen.text == SUMMARY_EN
    assert harness.telegram.of(untouched) == []
    assert _statement_calls(harness) == [(f"{STATEMENTS}/2026-08", None)]
    assert len(_calls(harness, "GET", EARNINGS)) == 2


@pytest.mark.parametrize("data", [
    "staff_sales_earn_s_202613",   # month 13
    "staff_sales_earn_s_2026101",  # seven digits
    "staff_sales_earn_sn_202600",  # month 0, on the push's own button
])
async def test_a_statement_button_the_bot_never_drew_is_answered_not_sent(monkeypatch, data):
    r"""The patterns `^staff_sales_earn_s_\d+$` and `^staff_sales_earn_sn_\d+$` guarantee digits,
    not a month: the lines buttons' rule, so a month no keyboard drew never reaches S4."""
    harness, ops = await _staff(monkeypatch)

    await harness.send(ops.tap(data))

    assert _statement_calls(harness) == []
    assert len(harness.telegram.of("answerCallbackQuery")) == 1
    assert _alerts(harness) == []


# ---- refusals and re-taps -----------------------------------------------------------------------

async def test_a_backend_failure_is_an_alert_not_a_blank_card(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: staff_backend_failure("boom", 500))

    await harness.send(ops.tap("staff_sales_earn"))

    assert f"❌ {_curated('staff.error.api.service_unavailable')}" in _alerts(harness)


@pytest.mark.parametrize("data", [
    "staff_sales_earn_l_209913_1",   # month 13
    "staff_sales_earn_l_202610_0",   # page 0
    "staff_sales_earn_l_2026101_1",  # seven digits
])
async def test_a_lines_button_the_bot_never_drew_is_answered_not_sent(monkeypatch, data):
    r"""The registered pattern is `^staff_sales_earn_l_\d+_\d+$`, which guarantees digits and
    not a RANGE. A month or page the keyboard never drew is answered (the button stops
    spinning) and never sent, the `stats.py` rule."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", LINES, _lines_route)

    await harness.send(ops.tap(data))

    assert not _calls(harness, "GET", LINES)
    assert len(harness.telegram.of("answerCallbackQuery")) == 1
    assert _alerts(harness) == []


async def test_a_retap_refetches_and_an_identical_card_raises_no_alert(monkeypatch):
    """Refresh is a gesture here (§7.2): every tap re-reads S1, one GET per tap. When nothing
    changed, Telegram answers the edit "not modified", which is Telegram agreeing, not a
    failure: no alert, no "something went wrong"."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())
    await harness.send(ops.tap("staff_sales_earn"))
    harness.telegram.reset()
    harness.telegram.fail("editMessageText", "Message is not modified", 400)

    await harness.send(ops.tap("staff_sales_earn"))

    assert len(_calls(harness, "GET", EARNINGS)) == 2
    assert len(harness.telegram.of("editMessageText")) == 1
    assert len(harness.telegram.of("answerCallbackQuery")) == 1
    assert _alerts(harness) == []


async def test_no_retired_pay_word_reaches_the_bot():
    """v2's floor and carried-from rows and v3's summed owed-to-date were removed before they
    ever shipped (spec §8.2, Appendices C and D), and v5 retired the `plan_changed` cause: a plan
    change re-rates an open month and writes no line (§4.10). None may come back through a
    fixture, a renderer or a seeded row."""
    fixtures = json.dumps([_earnings(), _leaver_november(), list(LINES_OCTOBER.values()),
                           LINES_NOVEMBER_UNITS_REDUCED, LINES_NOVEMBER_REVERSAL,
                           PENALTY_PUSH, STATEMENT_PUSH, OCTOBER_STATEMENT, _trial_september(), PAST_STATEMENTS])
    sources = [ROOT / "staff_bot" / path for path in (
        "handlers/sales/earnings.py", "keyboards/sales.py", "utils/formatters.py", "webhook_server.py",
    )]
    for word in ("floor_note", "carried_from", "owed_to_date", "plan_changed"):
        assert word not in fixtures, word
        for path in sources:
            assert word not in path.read_text(encoding="utf-8"), (word, path)
        assert not [suffix for suffix in _SEED.SALES_TEXT_TRANSLATIONS if word in suffix], word


# ---- the pushes, over the real webhook route ----------------------------------------------------

async def test_the_penalty_push_lands_on_the_penalties_screen_from_a_cold_chat(monkeypatch):
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())

    async with _webhook(monkeypatch, harness) as client:
        answer = await _push(client, {
            "event_id": "sales-event:penalty-confirmed:17", "telegram_id": DEFAULT_DRIVER_TELEGRAM_ID,
            "event": "pay_penalty_confirmed", "payload": PENALTY_PUSH,
        })

    assert answer == (200, {"success": True, "message": "Notification sent"})
    (push,) = harness.telegram.of("sendMessage")
    assert push.params["chat_id"] == DEFAULT_DRIVER_TELEGRAM_ID
    assert push.text == PENALTY_PUSH_EN
    assert push.button_labels() == ["💰 Open my earnings"]
    assert push.callback_data() == ["staff_sales_earn_xn"]
    # Pay news makes a sound (§7.5): it arrives while the phone is in a pocket.
    assert push.params["disable_notification"] is False
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_xn"))

    # Final-review M9 (owner decision 2026-10-05): the push stays in the chat; the screen
    # arrives as a NEW message under it.
    assert harness.telegram.of("editMessageText") == []
    (screen,) = harness.telegram.of("sendMessage")
    assert screen.text == PENALTIES_EN
    assert screen.callback_data() == ["staff_sales_earn"]


async def test_the_approval_push_opens_its_months_statement_as_a_new_message(monkeypatch):
    """The push's button is "📄 Open statement" for the month it announced. Final-review M9: the
    approved month's breakdown stays in the chat, that month's Statement (S4) arrives as a NEW
    message under it, and navigating from it edits that new message in place."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", f"{STATEMENTS}/2026-10", lambda _c: OCTOBER_STATEMENT)
    harness.backend.route("GET", STATEMENTS, lambda _c: PAST_STATEMENTS)

    async with _webhook(monkeypatch, harness) as client:
        answer = await _push(client, {
            "event_id": "sales-event:statement-approved:55", "telegram_id": DEFAULT_DRIVER_TELEGRAM_ID,
            "event": "pay_statement_approved", "payload": STATEMENT_PUSH,
        })

    assert answer == (200, {"success": True, "message": "Notification sent"})
    (push,) = harness.telegram.of("sendMessage")
    assert push.text == STATEMENT_PUSH_EN
    assert push.button_labels() == ["📄 Open statement"]
    assert push.callback_data() == ["staff_sales_earn_sn_202610"]
    assert push.params["disable_notification"] is False
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_sn_202610"))

    assert harness.telegram.of("editMessageText") == []
    (screen,) = harness.telegram.of("sendMessage")
    assert screen.text == OCTOBER_STATEMENT_EN
    assert screen.callback_data() == STATEMENT_CALLBACKS
    assert _statement_calls(harness) == [(f"{STATEMENTS}/2026-10", None)]
    assert not _calls(harness, "GET", EARNINGS)
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_h"))

    assert harness.telegram.of("sendMessage") == []
    (edited,) = harness.telegram.of("editMessageText")
    assert edited.text == PAST_STATEMENTS_EN


async def test_a_trial_months_push_ends_with_its_note_and_opens_its_statement(monkeypatch):
    """The trial September's approval (I-16): the headline says "(not paid)", the push ends with
    the trial note My earnings prints for the month, and its button opens September's Statement
    as a new message under it."""
    trial_push = {**STATEMENT_PUSH, "statement_id": 56, "month": "2026-09", "is_shadow": True, "base": 1150000,
                  "commission": {"gross": 0, "products": []}, "commission_after_gate": 0, "late": 0,
                  "new_outlets": 0, "adjustments": 0, "penalties": 100000, "total": 1050000}
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", f"{STATEMENTS}/2026-09", lambda _c: _trial_september())

    async with _webhook(monkeypatch, harness) as client:
        answer = await _push(client, {
            "event_id": "sales-event:statement-approved:56", "telegram_id": DEFAULT_DRIVER_TELEGRAM_ID,
            "event": "pay_statement_approved", "payload": trial_push,
        })

    assert answer == (200, {"success": True, "message": "Notification sent"})
    (push,) = harness.telegram.of("sendMessage")
    assert push.text == "\n".join((
        "✅ <b>Trial statement for 09.2026 is approved (not paid)</b>",
        "Base: 1,150,000 UZS",
        "Commission: 0 UZS",
        "Commission after discipline: 0 UZS",
        f"Penalties: {MINUS}100,000 UZS",
        "<b>Total: 1,050,000 UZS</b>",
        "Trial month: these numbers are shown for review; pay is made the old way.",
    ))
    assert push.button_labels() == ["📄 Open statement"]
    assert push.callback_data() == ["staff_sales_earn_sn_202609"]
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_sn_202609"))

    assert harness.telegram.of("editMessageText") == []
    (screen,) = harness.telegram.of("sendMessage")
    assert screen.text == TRIAL_STATEMENT_EN
    assert _statement_calls(harness) == [(f"{STATEMENTS}/2026-09", None)]


async def test_an_approval_push_already_in_a_chat_still_opens_the_summary(monkeypatch):
    """Pushes sent before the Statement screen existed carry `staff_sales_earn_n`, and they stay
    in agents' chats, so that callback stays registered: the live summary as a NEW message (M9),
    and navigating from it edits that new message in place."""
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())

    await harness.send(ops.tap("staff_sales_earn_n"))

    assert harness.telegram.of("editMessageText") == []
    (screen,) = harness.telegram.of("sendMessage")
    assert screen.text == SUMMARY_EN
    assert "staff_sales_earn_x" in screen.callback_data()
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_earn_x"))

    assert harness.telegram.of("sendMessage") == []
    (edited,) = harness.telegram.of("editMessageText")
    assert edited.text == PENALTIES_EN


async def test_the_summary_and_the_approval_push_draw_tiers_through_one_helper(monkeypatch):
    """§7.2/§10.4: one helper renders the tier rows for the summary and for the approval push, so
    the two cannot drift. Both modules hold the formatters' own function, not a copy. A spy that
    renders through it sees the push ask without the next tier (a frozen statement has none) and
    the summary ask with it, and both messages carry the same rows."""
    from staff_bot import webhook_server
    from staff_bot.handlers.sales import earnings
    from staff_bot.utils import formatters

    assert earnings.format_pay_tiers is formatters.format_pay_tiers
    assert webhook_server.format_pay_tiers is formatters.format_pay_tiers
    real, asked = formatters.format_pay_tiers, []

    def spy(product, language, *, with_next):
        asked.append((product["product_id"], language, with_next))
        return real(product, language, with_next=with_next)

    monkeypatch.setattr(earnings, "format_pay_tiers", spy)
    monkeypatch.setattr(webhook_server, "format_pay_tiers", spy)
    harness, ops = await _staff(monkeypatch)
    harness.backend.route("GET", EARNINGS, lambda _c: _earnings())

    async with _webhook(monkeypatch, harness) as client:
        await _push(client, {
            "event_id": "sales-event:statement-approved:55", "telegram_id": DEFAULT_DRIVER_TELEGRAM_ID,
            "event": "pay_statement_approved", "payload": STATEMENT_PUSH,
        })
    await harness.send(ops.tap("staff_sales_earn_n"))

    assert asked == [(3, "en", False), (9, "en", False), (3, "en", True), (9, "en", True)]
    push, screen = (call.text.split("\n") for call in harness.telegram.of("sendMessage"))
    rows = push[push.index("Commission: 850,000 UZS") + 1:push.index("Commission after discipline: 680,000 UZS")]
    start = screen.index(rows[0])
    assert len(rows) == 5 and screen[start:start + 5] == rows


async def test_a_push_whose_send_failed_is_delivered_by_the_retry(monkeypatch):
    """S-18: the slot is claimed before the send, and `push_sales_event` retries only on a
    non-200. A failed send that kept the slot would answer the retry "Already processed": the
    agent's pay news lost, and reported delivered."""
    harness, ops = await _staff(monkeypatch)
    body = {"event_id": "sales-event:statement-approved:55", "telegram_id": DEFAULT_DRIVER_TELEGRAM_ID,
            "event": "pay_statement_approved", "payload": STATEMENT_PUSH}

    async with _webhook(monkeypatch, harness) as client:
        harness.telegram.fail("sendMessage", "Bad Gateway", status=502)
        first = await _push(client, body)
        harness.telegram.clear_failures()
        harness.telegram.reset()
        retry = await _push(client, body)
        replay = await _push(client, body)

    assert first == (502, {"success": False, "message": "Send failed"})
    assert retry == (200, {"success": True, "message": "Notification sent"})
    assert replay == (200, {"success": True, "message": "Already processed"})
    assert [call.text for call in harness.telegram.of("sendMessage")] == [STATEMENT_PUSH_EN]
