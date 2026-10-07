"""A12-A15: the admin pay-plan routes, driven the way the Compensation page's Plans tab drives them.

Spec: §4.10 (validation, editability, resolution), §5.2 (A12-A15, `VersionPayload`, `PlanRow`,
`VersionDetail`), §5.6 (admin-only). The tokens carry the role CLAIM (`admin_claim_headers`):
`super_admin_required` reads it and cross-checks the DB row.

The clock is frozen at Tue 20 October 2026, 10:00 Tashkent. A12 publishes `current_month` and
`editable_from_month` from it, and A13 and A15 judge the effective month by it.

Two layers refuse a bad version, and both are pinned:
- `VersionPayload` refuses a wrong type, an unknown key and a band's or the bonus's single-field
  bound with the uncoded 400 every sales route returns for a malformed body;
- `SalesPayPlanService.validate` refuses the rest of the §4.10 table with SALES_PAY_PLAN_INVALID
  and `details.field`, `details.reason` and, for a list item, `details.index`, or for a tier its
  1-based `details.tier` and the `details.product_id` of the schedule holding it, so the editor can
  point at the input. A schedule's limits (`MAX_TIERS`, `MAX_FROM_UNIT`, the per-unit cap,
  `MAX_PRODUCT_SCHEDULES`) live only there, so a tier past one is named too. It is also driven
  directly over the single-field rows the schema answers first, because it is the one statement of
  the whole table.
"""

import copy
from datetime import datetime
from decimal import Decimal

import pytest

from business_app.models.product import Product
from business_app.models.sales_pay import SalesPayPlan, SalesPayPlanRate, SalesPayPlanVersion
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_rules import GateBand, PlanConfig, Rate, Tier, TierSchedule
from business_app.utils import local_windows
from business_app.utils.exceptions import ValidationError
from tests.integration.sales_pay_builders import freeze_local_now, pay_audit, tier, version_payload
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture
from tests.integration.test_sales_pay_period_service import (
    DEC,
    IN_OCTOBER,
    IN_SEPTEMBER,
    NOV,
    OCT,
    SEP,
    move_period,
)

pytestmark = pytest.mark.integration

PLANS_URL = "/api/v1/admin/sales/pay/plans"
VERSIONS_URL = "/api/v1/admin/sales/pay/plans/{}/versions"
VERSION_URL = "/api/v1/admin/sales/pay/plans/{}/versions/{}"

# The default bonus block, read from the one version builder (plan ruling PR7).
BONUS = version_payload("2026-10")["new_outlet_bonus"]
# 100, 90, ... 10, then 0: well-ordered, but one band more than the 10 a version may have.
ELEVEN_BANDS = [{"min_pct": 100 - 10 * i, "multiplier": 1} for i in range(10)] + [{"min_pct": 0, "multiplier": 1}]
# Illustration B's 19 L schedule (spec §1.2): units 1-300 at 1,000, 301 and above at 1,500.
TWO_TIERS_19L = [tier(1, "per_unit", 1000), tier(301, "per_unit", 1500)]
TWO_TIERS_19L_CONFIG = TierSchedule(
    tiers=(Tier(from_unit=1, rate=Rate("per_unit", Decimal("1000"))), Tier(from_unit=301, rate=Rate("per_unit", Decimal("1500"))))
)
# 1, 101, ... 1001: well-ordered, but one tier more than the 10 a schedule may have.
ELEVEN_TIERS = [tier(1 + 100 * position, "percent", 3) for position in range(11)]
# What A12 publishes about each version's months, beside its number and effective month.
COVERAGE_KEYS = ("version_no", "effective_month", "applies_from", "applies_until", "replaced_by_version_no")


def _single(mode, value):
    return TierSchedule(tiers=(Tier(from_unit=1, rate=Rate(mode, Decimal(value))),))


def plan_version_payload(effective_month="2026-10", **overrides):
    """A `VersionPayload` body as the Plans tab posts it: Example A's plan (spec §1.2) with a single
    3% default tier, the default bands and a 150,000 bonus, from the one version builder
    (`version_payload`, plan ruling PR7). `overrides` replace whole top-level keys; pass
    `rates=[{"product_id": …, "tiers": [tier(…), …]}]` for product schedules."""
    body = version_payload(effective_month)
    body.update(copy.deepcopy(overrides))
    return body


@pytest.fixture
def frozen(monkeypatch):
    return freeze_local_now(monkeypatch, IN_OCTOBER)


@pytest.fixture
def september_open(db, admin_user, frozen):
    """Pay started in September and September is still open, so a version may start in it."""
    SalesPayPeriodService.start(SEP, is_shadow=False, actor_id=admin_user.id, now=IN_SEPTEMBER)
    SalesPayPeriodService.ensure_periods(IN_OCTOBER)


def _assert_published_months_match_resolve(plan, months):
    """Each month is paid on the version A12 says covers it, read two ways from the published
    fields: the one row whose `applies_from`..`applies_until` holds the month (null `applies_until`
    runs on), and the last `timeline` entry from on or before it. Both must be the version
    `SalesPayPlanService.resolve` pays the month on."""
    for month in months:
        name = local_windows.format_month(month)
        covering = [
            row["id"]
            for row in plan["versions"]
            if row["applies_from"] is not None
            and row["applies_from"] <= name
            and (row["applies_until"] is None or name <= row["applies_until"])
        ]
        started = [entry["version_id"] for entry in plan["timeline"] if entry["from_month"] <= name]
        resolved = SalesPayPlanService.resolve(plan["id"], month).version_id
        assert (covering, started[-1]) == ([resolved], resolved), name


@pytest.fixture
def water_10l(db, sample_category):
    """An INACTIVE product: a plan may still rate it (spec §3.2), and A14 says it is inactive."""
    product = Product(
        name="Water 10L",
        category_id=sample_category.id,
        size="10L",
        volume=10.0,
        volume_unit="L",
        base_price=Decimal("9000.00"),
        stock_quantity=0,
        is_active=False,
    )
    db.session.add(product)
    db.session.commit()
    return product


def _create_plan(client, headers, *, name="Standard", version=None):
    return client.post(PLANS_URL, json={"name": name, "version": version or plan_version_payload()}, headers=headers)


def _plan_id(client, headers, **kwargs):
    response = _create_plan(client, headers, **kwargs)
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["data"]["plan"]["id"]


def _versions(plan_id):
    return SalesPayPlanVersion.query.filter_by(plan_id=plan_id).count()


# --------------------------------------------------------------------------- #
# A12-A15: what the Plans tab reads and writes
# --------------------------------------------------------------------------- #


def test_a12_before_any_plan_publishes_the_vocabularies_the_editor_reads(client, db, admin_claim_headers, frozen):
    response = client.get(PLANS_URL, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"] == {
        "items": [],
        "rate_modes": ["per_unit", "percent"],
        "bonus_rules": ["orders_with_total", "orders_any"],
        "editable_from_month": "2026-10",
        "current_month": "2026-10",
        "default_config": {
            "gate_bands": [
                {"min_pct": 80.0, "multiplier": 1.0},
                {"min_pct": 60.0, "multiplier": 0.8},
                {"min_pct": 0.0, "multiplier": 0.5},
            ],
            "gate_min_visits_due": 20,
            "bonus_window_days": 60,
            "bonus_min_orders_with_total": 2,
            "bonus_min_orders_any_amount": 5,
            "bonus_prior_customer_lookback_days": 180,
        },
    }


def test_a13_creates_the_plan_with_its_first_version_and_a14_echoes_it(
    client, db, admin_user, admin_claim_headers, sample_product, water_10l, frozen, audit_events
):
    """The schedules are posted in reverse product order and come back by product id, each in ascending
    tiers with the derived `to_unit` (I-29). The inactive 10 L product is accepted (T-PLAN-3) and
    labelled inactive. The audit row holds the tiers as stored (§4.17)."""
    version = plan_version_payload(
        rates=[
            {"product_id": water_10l.id, "tiers": [tier(1, "percent", 2.5)]},
            {"product_id": sample_product.id, "tiers": TWO_TIERS_19L},
        ],
        note="launch",
    )

    created = _create_plan(client, admin_claim_headers, version=version)

    assert created.status_code == 201, created.get_data(as_text=True)
    plan = created.get_json()["data"]["plan"]
    version_id = plan["version_in_force"]["id"]
    created_at = plan["versions"][0].pop("created_at")
    # `_iso`/`to_wire` publish `created_at` in UTC, but it is one of the field names
    # `business_app.middleware.timezone_middleware.TimezoneMiddleware` (pre-existing,
    # registered unconditionally in `create_app()`, applies to EVERY JSON response) walks
    # and converts from UTC to the request's display timezone -- Asia/Tashkent here, since
    # no `X-Timezone` header and no stored user preference leaves it at the app default.
    # The value stays a correct, aware instant; only its DISPLAY offset is not 0.
    assert datetime.fromisoformat(created_at).tzinfo is not None
    assert plan == {
        "id": plan["id"],
        "name": "Standard",
        "version_in_force": {"id": version_id, "version_no": 1, "effective_month": "2026-10"},
        "timeline": [{"from_month": "2026-10", "version_id": version_id, "version_no": 1}],
        "versions": [
            {
                "id": version_id,
                "version_no": 1,
                "effective_month": "2026-10",
                "applies_from": "2026-10",
                "applies_until": None,
                "replaced_by_version_no": None,
                "created_by": {"id": admin_user.id, "name": "Admin User"},
                "note": "launch",
            }
        ],
    }

    detail = client.get(VERSION_URL.format(plan["id"], version_id), headers=admin_claim_headers)

    assert detail.status_code == 200, detail.get_data(as_text=True)
    echoed = detail.get_json()["data"]["version"]
    assert echoed.pop("created_at") == created_at
    assert echoed == {
        "id": version_id,
        "plan_id": plan["id"],
        "plan_name": "Standard",
        "version_no": 1,
        "effective_month": "2026-10",
        "default_tiers": [{"from_unit": 1, "to_unit": None, "mode": "percent", "value": 3.0}],
        "rates": [
            {
                "product_id": sample_product.id,
                "product_name": "Pure Water 19L",
                "product_is_active": True,
                "tiers": [
                    {"from_unit": 1, "to_unit": 300, "mode": "per_unit", "value": 1000.0},
                    {"from_unit": 301, "to_unit": None, "mode": "per_unit", "value": 1500.0},
                ],
            },
            {
                "product_id": water_10l.id,
                "product_name": "Water 10L",
                "product_is_active": False,
                "tiers": [{"from_unit": 1, "to_unit": None, "mode": "percent", "value": 2.5}],
            },
        ],
        "gate_bands": [
            {"min_pct": 80.0, "multiplier": 1.0},
            {"min_pct": 60.0, "multiplier": 0.8},
            {"min_pct": 0.0, "multiplier": 0.5},
        ],
        "gate_min_visits_due": 20,
        "new_outlet_bonus": {
            "amount": 150000.0,
            "window_days": 60,
            "prior_customer_lookback_days": 180,
            "min_orders_with_total": 2,
            "min_combined_total": 300000.0,
            "min_orders_any_amount": 5,
        },
        "note": "launch",
        "created_by": {"id": admin_user.id, "name": "Admin User"},
    }
    assert pay_audit(audit_events, "plan_created") == [
        (
            "sales_pay_plan",
            str(plan["id"]),
            None,
            {
                "name": "Standard",
                "version_id": version_id,
                "version_no": 1,
                "effective_month": "2026-10",
                "default_tiers": [{"from_unit": 1, "mode": "percent", "value": "3.00"}],
                "rates": [
                    {
                        "product_id": sample_product.id,
                        "tiers": [
                            {"from_unit": 1, "mode": "per_unit", "value": "1000.00"},
                            {"from_unit": 301, "mode": "per_unit", "value": "1500.00"},
                        ],
                    },
                    {"product_id": water_10l.id, "tiers": [{"from_unit": 1, "mode": "percent", "value": "2.50"}]},
                ],
            },
        )
    ]


def test_a_same_month_correction_is_a_newer_version_and_wins(client, db, admin_claim_headers, frozen, audit_events):
    """T-PLAN-2: a correction for October is v2 for October and replaces v1. A version for
    November is v3, and it does not replace the version in force before November. Each audit row
    holds its version's tiers (§4.17)."""
    plan_id = _plan_id(client, admin_claim_headers)

    corrected = client.post(
        VERSIONS_URL.format(plan_id),
        json=plan_version_payload(default_tiers=[tier(1, "percent", 4)]),
        headers=admin_claim_headers,
    )
    ahead = client.post(
        VERSIONS_URL.format(plan_id),
        json=plan_version_payload("2026-11", default_tiers=[tier(1, "percent", 5)]),
        headers=admin_claim_headers,
    )

    assert (corrected.status_code, ahead.status_code) == (201, 201)
    v2, v3 = corrected.get_json()["data"]["version"], ahead.get_json()["data"]["version"]
    assert (v2["version_no"], v2["effective_month"], v2["default_tiers"]) == (
        2,
        "2026-10",
        [{"from_unit": 1, "to_unit": None, "mode": "percent", "value": 4.0}],
    )
    assert (v3["version_no"], v3["effective_month"]) == (3, "2026-11")
    listed = client.get(PLANS_URL, headers=admin_claim_headers).get_json()["data"]["items"][0]
    assert listed["version_in_force"] == {"id": v2["id"], "version_no": 2, "effective_month": "2026-10"}
    assert [row["version_no"] for row in listed["versions"]] == [3, 2, 1]

    october, november = SalesPayPlanService.resolve(plan_id, OCT), SalesPayPlanService.resolve(plan_id, NOV)
    assert (october.version_id, october.version_no, october.config.default_tiers) == (
        v2["id"],
        2,
        _single("percent", "4"),
    )
    assert (november.version_id, november.config.default_tiers) == (v3["id"], _single("percent", "5"))
    assert SalesPayPlanService.resolve(plan_id, SEP) is None
    assert pay_audit(audit_events, "plan_version_created") == [
        (
            "sales_pay_plan",
            str(plan_id),
            None,
            {
                "version_id": v2["id"],
                "version_no": 2,
                "effective_month": "2026-10",
                "default_tiers": [{"from_unit": 1, "mode": "percent", "value": "4.00"}],
                "rates": [],
            },
        ),
        (
            "sales_pay_plan",
            str(plan_id),
            None,
            {
                "version_id": v3["id"],
                "version_no": 3,
                "effective_month": "2026-11",
                "default_tiers": [{"from_unit": 1, "mode": "percent", "value": "5.00"}],
                "rates": [],
            },
        ),
    ]


def test_a12_publishes_the_months_each_version_applies_to_and_the_plan_timeline(
    client, db, admin_claim_headers, september_open
):
    """The dev shape behind the 2026-10-07 report: v1, v2, v4 and v5 effective September and v3
    effective October. In each month the highest `version_no` wins, so v5 is September's only
    month and v1, v2 and v4 apply to no month; v3 takes over from October, open-ended. A12
    publishes those months and the timeline by `resolve`'s rule, so the Plans tab renders them and
    re-derives nothing. A plan with one version applies from its month on. Pay started in
    September and September is still open, so the versions go through A13 and A15."""
    plan_id = _plan_id(client, admin_claim_headers, version=plan_version_payload("2026-09"))
    for month in ("2026-09", "2026-10", "2026-09", "2026-09"):
        created = client.post(
            VERSIONS_URL.format(plan_id), json=plan_version_payload(month), headers=admin_claim_headers
        )
        assert created.status_code == 201, created.get_data(as_text=True)
    _plan_id(client, admin_claim_headers, name="Premium", version=plan_version_payload("2026-10"))

    response = client.get(PLANS_URL, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    premium, standard = response.get_json()["data"]["items"]
    ids = {row["version_no"]: row["id"] for row in standard["versions"]}
    assert standard["timeline"] == [
        {"from_month": "2026-09", "version_id": ids[5], "version_no": 5},
        {"from_month": "2026-10", "version_id": ids[3], "version_no": 3},
    ]
    assert [tuple(row[key] for key in COVERAGE_KEYS) for row in standard["versions"]] == [
        (5, "2026-09", "2026-09", "2026-09", None),
        (4, "2026-09", None, None, 5),
        (3, "2026-10", "2026-10", None, None),
        (2, "2026-09", None, None, 5),
        (1, "2026-09", None, None, 5),
    ]
    assert standard["version_in_force"] == {"id": ids[3], "version_no": 3, "effective_month": "2026-10"}
    # September to December, the open-ended tail included: each month's published version is `resolve`'s.
    _assert_published_months_match_resolve(standard, (SEP, OCT, NOV, DEC))
    (only,) = premium["versions"]
    assert premium["timeline"] == [{"from_month": "2026-10", "version_id": only["id"], "version_no": 1}]
    assert (only["applies_from"], only["applies_until"], only["replaced_by_version_no"]) == ("2026-10", None, None)
    _assert_published_months_match_resolve(premium, (OCT, NOV, DEC))


def test_a12_a_version_covers_the_months_until_a_later_effective_month(client, db, admin_claim_headers, september_open):
    """A gap between effective months stays on the earlier version: v1 from September and v2 from
    December leave October and November on v1, so v1 applies to 09.2026 – 11.2026 and v2 from
    12.2026 on, as `resolve` pays them."""
    plan_id = _plan_id(client, admin_claim_headers, version=plan_version_payload("2026-09"))
    created = client.post(
        VERSIONS_URL.format(plan_id), json=plan_version_payload("2026-12"), headers=admin_claim_headers
    )
    assert created.status_code == 201, created.get_data(as_text=True)

    response = client.get(PLANS_URL, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    (plan,) = response.get_json()["data"]["items"]
    december, september = plan["versions"]
    assert plan["timeline"] == [
        {"from_month": "2026-09", "version_id": september["id"], "version_no": 1},
        {"from_month": "2026-12", "version_id": december["id"], "version_no": 2},
    ]
    assert [tuple(row[key] for key in COVERAGE_KEYS) for row in plan["versions"]] == [
        (2, "2026-12", "2026-12", None, None),
        (1, "2026-09", "2026-09", "2026-11", None),
    ]
    _assert_published_months_match_resolve(plan, (SEP, OCT, NOV, DEC))


def test_each_month_reads_the_tiers_of_its_own_version(client, db, admin_claim_headers, sample_product, frozen):
    """T-PLAN-1, plan half. v1 from October pays 19 L in Illustration B's two tiers, and v2 from
    November in one tier at 2,000. In October A12 keeps v1 in force, A14 echoes each version's own
    tiers, and `resolve` hands October v1's schedule and November v2's. The statement half (October
    recalculated on v1 after v2 exists) is Task 4's."""
    plan_id = _plan_id(
        client,
        admin_claim_headers,
        version=plan_version_payload(rates=[{"product_id": sample_product.id, "tiers": TWO_TIERS_19L}]),
    )
    published = client.post(
        VERSIONS_URL.format(plan_id),
        json=plan_version_payload("2026-11", rates=[{"product_id": sample_product.id, "tiers": [tier(1, "per_unit", 2000)]}]),
        headers=admin_claim_headers,
    )
    assert published.status_code == 201, published.get_data(as_text=True)
    v2 = published.get_json()["data"]["version"]
    in_force = client.get(PLANS_URL, headers=admin_claim_headers).get_json()["data"]["items"][0]["version_in_force"]
    v1 = client.get(VERSION_URL.format(plan_id, in_force["id"]), headers=admin_claim_headers).get_json()["data"]["version"]

    assert (in_force["version_no"], in_force["effective_month"]) == (1, "2026-10")
    assert [rate["tiers"] for rate in v1["rates"]] == [
        [
            {"from_unit": 1, "to_unit": 300, "mode": "per_unit", "value": 1000.0},
            {"from_unit": 301, "to_unit": None, "mode": "per_unit", "value": 1500.0},
        ]
    ]
    assert [rate["tiers"] for rate in v2["rates"]] == [[{"from_unit": 1, "to_unit": None, "mode": "per_unit", "value": 2000.0}]]
    october, november = (SalesPayPlanService.resolve(plan_id, month).config for month in (OCT, NOV))
    assert october.schedule_for(sample_product.id) == (TWO_TIERS_19L_CONFIG, False)
    assert november.schedule_for(sample_product.id) == (_single("per_unit", "2000"), False)


def test_a15_saves_a_mixed_two_tier_version_and_a14_publishes_each_upper_bound(
    client, db, admin_claim_headers, sample_product, frozen
):
    """T-TIER-8's valid save: a default schedule mixing modes (I-35) and a two-tier 19 L schedule. A14
    echoes `to_unit` 1000 / null and 300 / null. This is also T-MONEY-1 for the tier fields: first and
    last units are ints (`to_unit` null on the top tier), and a rate is a float, whole for
    `per_unit`. Rates are not pay money, so the golden walker's money keys never list `value`."""
    plan_id = _plan_id(client, admin_claim_headers)

    response = client.post(
        VERSIONS_URL.format(plan_id),
        json=plan_version_payload(
            default_tiers=[tier(1, "percent", 2), tier(1001, "per_unit", 500)],
            rates=[{"product_id": sample_product.id, "tiers": TWO_TIERS_19L}],
        ),
        headers=admin_claim_headers,
    )

    assert response.status_code == 201, response.get_data(as_text=True)
    created = response.get_json()["data"]["version"]
    echoed = client.get(VERSION_URL.format(plan_id, created["id"]), headers=admin_claim_headers).get_json()["data"]
    assert echoed["version"] == created
    assert created["default_tiers"] == [
        {"from_unit": 1, "to_unit": 1000, "mode": "percent", "value": 2.0},
        {"from_unit": 1001, "to_unit": None, "mode": "per_unit", "value": 500.0},
    ]
    assert [(row["from_unit"], row["to_unit"]) for row in created["rates"][0]["tiers"]] == [(1, 300), (301, None)]
    tiers = [*created["default_tiers"], *created["rates"][0]["tiers"]]
    assert all(type(row["from_unit"]) is int for row in tiers)
    assert all(row["to_unit"] is None or type(row["to_unit"]) is int for row in tiers)
    assert all(type(row["value"]) is float for row in tiers)
    assert all(row["value"] == int(row["value"]) for row in tiers if row["mode"] == "per_unit")


def test_a_version_for_a_month_before_the_first_editable_one_is_refused(
    client, db, admin_user, admin_claim_headers, frozen
):
    """Before pay starts, the first editable month is the current one. Once pay has started it is
    the earliest open month, here October, because September is closed (spec §4.10)."""
    refused = _create_plan(client, admin_claim_headers, version=plan_version_payload("2026-09"))

    assert refused.status_code == 409, refused.get_data(as_text=True)
    body = refused.get_json()
    assert (body["error_code"], body["details"]) == (
        "SALES_PAY_MONTH_LOCKED",
        {"month": "2026-09", "status": None, "editable_from_month": "2026-10"},
    )
    assert SalesPayPlan.query.count() == 0

    plan_id = _plan_id(client, admin_claim_headers)
    SalesPayPeriodService.start(SEP, is_shadow=False, actor_id=admin_user.id, now=IN_SEPTEMBER)
    months = {period.month_start: period for period in SalesPayPeriodService.ensure_periods(IN_OCTOBER)}
    move_period(db, months[SEP], "closed", admin_user)

    locked = client.post(
        VERSIONS_URL.format(plan_id), json=plan_version_payload("2026-09"), headers=admin_claim_headers
    )

    assert locked.status_code == 409, locked.get_data(as_text=True)
    body = locked.get_json()
    assert (body["error_code"], body["details"]) == (
        "SALES_PAY_MONTH_LOCKED",
        {"month": "2026-09", "status": "closed", "editable_from_month": "2026-10"},
    )
    assert _versions(plan_id) == 1


# --------------------------------------------------------------------------- #
# The §4.10 table, where the service decides
# --------------------------------------------------------------------------- #


CODED_REFUSALS = [
    # T-TIER-8: a schedule's tiers (§4.10)
    pytest.param(
        lambda pid: {"default_tiers": [tier(2, "percent", 3)]},
        lambda pid: {"field": "default_tiers.from_unit", "reason": "first_not_one", "tier": 1},
        id="default-schedule-starts-past-unit-1",
    ),
    pytest.param(
        lambda pid: {"default_tiers": [tier(1, "percent", 2), tier(501, "percent", 3), tier(301, "percent", 4)]},
        lambda pid: {"field": "default_tiers.from_unit", "reason": "not_increasing", "tier": 3},
        id="default-tiers-out-of-order",
    ),
    pytest.param(
        lambda pid: {
            "rates": [
                {
                    "product_id": pid,
                    "tiers": [tier(1, "per_unit", 1000), tier(501, "per_unit", 1500), tier(501, "per_unit", 2000)],
                }
            ]
        },
        lambda pid: {"field": "rates.tiers.from_unit", "reason": "not_increasing", "tier": 3, "product_id": pid},
        id="product-tier-repeats-a-first-unit",
    ),
    pytest.param(
        lambda pid: {"rates": [{"product_id": pid, "tiers": [tier(1, "per_unit", 1000), tier(301, "per_unit", 1500.5)]}]},
        lambda pid: {"field": "rates.tiers.value", "reason": "not_whole", "tier": 2, "product_id": pid},
        id="per-unit-tier-not-whole-uzs",
    ),
    pytest.param(
        lambda pid: {"default_tiers": [tier(1, "percent", 100.5)]},
        lambda pid: {"field": "default_tiers.value", "reason": "out_of_range", "tier": 1},
        id="percent-tier-over-100",
    ),
    pytest.param(
        lambda pid: {"default_tiers": [tier(1, "percent", 2.555)]},
        lambda pid: {"field": "default_tiers.value", "reason": "too_many_decimals", "tier": 1},
        id="percent-tier-with-three-decimals",
    ),
    # T-TIER-8: a schedule's limits. `validate` is their one home (no bound in `TierPayload`), so the
    # editor is told which tier of which product is past one.
    pytest.param(
        lambda pid: {"default_tiers": []},
        lambda pid: {"field": "default_tiers", "reason": "required"},
        id="no-default-tiers",
    ),
    pytest.param(
        lambda pid: {"default_tiers": ELEVEN_TIERS},
        lambda pid: {"field": "default_tiers", "reason": "too_many"},
        id="eleven-default-tiers",
    ),
    pytest.param(
        lambda pid: {"rates": [{"product_id": pid, "tiers": ELEVEN_TIERS}]},
        lambda pid: {"field": "rates.tiers", "reason": "too_many", "product_id": pid},
        id="eleven-product-tiers",
    ),
    pytest.param(
        lambda pid: {"rates": [{"product_id": pid, "tiers": []}]},
        lambda pid: {"field": "rates.tiers", "reason": "required", "product_id": pid},
        id="product-schedule-without-tiers",
    ),
    pytest.param(
        lambda pid: {"default_tiers": [tier(0, "percent", 3)]},
        lambda pid: {"field": "default_tiers.from_unit", "reason": "out_of_range", "tier": 1},
        id="tier-before-unit-1",
    ),
    pytest.param(
        lambda pid: {"rates": [{"product_id": pid, "tiers": [tier(1, "per_unit", 1000), tier(1_000_001, "per_unit", 1500)]}]},
        lambda pid: {"field": "rates.tiers.from_unit", "reason": "out_of_range", "tier": 2, "product_id": pid},
        id="first-unit-past-a-million",
    ),
    pytest.param(
        lambda pid: {"rates": [{"product_id": pid, "tiers": [tier(1, "per_unit", 1000), tier(301, "per_unit", 10_000_001)]}]},
        lambda pid: {"field": "rates.tiers.value", "reason": "out_of_range", "tier": 2, "product_id": pid},
        id="per-unit-rate-over-ten-million",
    ),
    pytest.param(
        lambda pid: {"default_tiers": [tier(1, "percent", -1)]},
        lambda pid: {"field": "default_tiers.value", "reason": "out_of_range", "tier": 1},
        id="negative-percent",
    ),
    pytest.param(
        lambda pid: {"rates": [{"product_id": i, "tiers": [tier(1, "per_unit", 1)]} for i in range(1, 202)]},
        lambda pid: {"field": "rates", "reason": "too_many"},
        id="201-product-schedules",
    ),
    # product schedules
    pytest.param(
        lambda pid: {
            "rates": [
                {"product_id": pid, "tiers": [tier(1, "per_unit", 1500)]},
                {"product_id": pid, "tiers": [tier(1, "percent", 2)]},
            ]
        },
        lambda pid: {"field": "rates.product_id", "reason": "duplicate_product", "index": 1},
        id="product-scheduled-twice",
    ),
    pytest.param(
        lambda pid: {
            "rates": [
                {"product_id": pid, "tiers": [tier(1, "per_unit", 1500)]},
                {"product_id": pid + 1000, "tiers": [tier(1, "percent", 2)]},
            ]
        },
        lambda pid: {"field": "rates.product_id", "reason": "unknown_product", "index": 1},
        id="unknown-product",
    ),
    # gate bands
    pytest.param(
        lambda pid: {"gate_bands": [{"min_pct": 80, "multiplier": 1}, {"min_pct": 80, "multiplier": 0.8},
                                    {"min_pct": 0, "multiplier": 0.5}]},
        lambda pid: {"field": "gate_bands.min_pct", "reason": "not_descending", "index": 1},
        id="bands-not-strictly-descending",
    ),
    pytest.param(
        lambda pid: {"gate_bands": [{"min_pct": 80.05, "multiplier": 1}, {"min_pct": 0, "multiplier": 0.5}]},
        lambda pid: {"field": "gate_bands.min_pct", "reason": "too_many_decimals", "index": 0},
        id="band-percent-with-two-decimals",
    ),
    pytest.param(
        lambda pid: {"gate_bands": [{"min_pct": 80, "multiplier": 1}, {"min_pct": 60, "multiplier": 0.8}]},
        lambda pid: {"field": "gate_bands.min_pct", "reason": "last_not_zero", "index": 1},
        id="last-band-not-zero",
    ),
    pytest.param(
        lambda pid: {"gate_bands": [{"min_pct": 80, "multiplier": 1}, {"min_pct": 0, "multiplier": 0.5005}]},
        lambda pid: {"field": "gate_bands.multiplier", "reason": "too_many_decimals", "index": 1},
        id="multiplier-with-four-decimals",
    ),
    pytest.param(
        lambda pid: {"gate_bands": [{"min_pct": 80, "multiplier": 0.8}, {"min_pct": 0, "multiplier": 1}]},
        lambda pid: {"field": "gate_bands.multiplier", "reason": "multiplier_increases", "index": 1},
        id="multiplier-rises-as-compliance-falls",
    ),
    # the new-outlet bonus
    pytest.param(
        lambda pid: {"new_outlet_bonus": {**BONUS, "amount": 150000.5}},
        lambda pid: {"field": "new_outlet_bonus.amount", "reason": "not_whole"},
        id="bonus-not-whole-uzs",
    ),
    pytest.param(
        lambda pid: {"new_outlet_bonus": {**BONUS, "min_combined_total": 300000.25}},
        lambda pid: {"field": "new_outlet_bonus.min_combined_total", "reason": "not_whole"},
        id="combined-total-not-whole-uzs",
    ),
    pytest.param(
        lambda pid: {"new_outlet_bonus": {**BONUS, "min_orders_with_total": 3, "min_orders_any_amount": 2}},
        lambda pid: {"field": "new_outlet_bonus.min_orders_any_amount", "reason": "below_min_orders_with_total"},
        id="any-amount-rule-below-the-total-rule",
    ),
]


@pytest.mark.parametrize("overrides,expected", CODED_REFUSALS)
def test_a15_refuses_what_the_table_forbids_with_the_field_to_fix(
    client, db, admin_claim_headers, sample_product, frozen, overrides, expected
):
    """T-TIER-8's coded rows among them: `details.tier` is the tier's 1-based position, and
    `details.product_id` names the product whose schedule holds it."""
    plan_id = _plan_id(client, admin_claim_headers)

    response = client.post(
        VERSIONS_URL.format(plan_id),
        json=plan_version_payload(**overrides(sample_product.id)),
        headers=admin_claim_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert (body["error_code"], body["details"]) == (
        "SALES_PAY_PLAN_INVALID",
        {**expected(sample_product.id), "validation_errors": []},
    )
    assert _versions(plan_id) == 1


def test_a13_runs_the_same_validator_before_it_writes_anything(client, db, admin_claim_headers, frozen):
    response = _create_plan(
        client,
        admin_claim_headers,
        version=plan_version_payload(
            gate_bands=[{"min_pct": 80, "multiplier": 0.8}, {"min_pct": 0, "multiplier": 1}]
        ),
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert (body["error_code"], body["details"]) == (
        "SALES_PAY_PLAN_INVALID",
        {"field": "gate_bands.multiplier", "reason": "multiplier_increases", "index": 1, "validation_errors": []},
    )
    assert (SalesPayPlan.query.count(), SalesPayPlanVersion.query.count()) == (0, 0)


def test_a13_refuses_a_blank_or_taken_name_and_writes_nothing(client, db, admin_claim_headers, frozen):
    _plan_id(client, admin_claim_headers, name="Standard")

    for name, reason in (("   ", "length"), ("Standard", "duplicate")):
        response = _create_plan(client, admin_claim_headers, name=name)
        body = response.get_json()
        assert (response.status_code, body["error_code"], body["details"]) == (
            400,
            "SALES_PAY_PLAN_INVALID",
            {"field": "name", "reason": reason, "validation_errors": []},
        ), name

    assert SalesPayPlan.query.count() == 1


# --------------------------------------------------------------------------- #
# What the request schema refuses before the service sees it
# --------------------------------------------------------------------------- #


SCHEMA_REFUSALS = [
    # T-TIER-8: a tier's keys and types `TierPayload` / `ProductTiersPayload` refuse (its limits are
    # the service's: CODED_REFUSALS)
    pytest.param({"default_tiers": [tier(1, "fixed", 3)]}, id="unknown-rate-mode"),
    pytest.param({"default_tiers": [tier(1, "percent", "NaN")]}, id="rate-not-a-finite-number"),
    pytest.param({"default_tiers": [tier(1.5, "percent", 3)]}, id="first-unit-not-an-integer"),
    pytest.param({"default_tiers": [{**tier(1, "percent", 3), "to_unit": 500}]}, id="to-unit-is-derived-never-sent"),
    pytest.param({"default_tiers": [{**tier(1, "percent", 3), "amount": 3}]}, id="unknown-key-in-a-tier"),
    pytest.param({"default_rate": {"mode": "percent", "value": 3}}, id="v4-default-rate"),
    pytest.param({"rates": [{"product_id": 1, "mode": "per_unit", "value": 1500}]}, id="v4-flat-product-rate"),
    pytest.param({"rates": [{"tiers": [tier(1, "per_unit", 1500)]}]}, id="product-schedule-without-a-product"),
    # bands, the bonus, the month and unknown keys (v4, unchanged)
    pytest.param(
        {"gate_bands": [{"min_pct": 100.5, "multiplier": 1}, {"min_pct": 0, "multiplier": 0.5}]},
        id="band-over-100-percent",
    ),
    pytest.param(
        {"gate_bands": [{"min_pct": 80, "multiplier": 1.2}, {"min_pct": 0, "multiplier": 0.5}]},
        id="multiplier-over-one",
    ),
    pytest.param({"gate_bands": []}, id="no-bands"),
    pytest.param({"gate_bands": ELEVEN_BANDS}, id="eleven-bands"),
    pytest.param({"gate_min_visits_due": -1}, id="negative-minimum-due"),
    pytest.param({"new_outlet_bonus": {**BONUS, "window_days": 0}}, id="zero-day-window"),
    pytest.param({"new_outlet_bonus": {**BONUS, "prior_customer_lookback_days": 731}}, id="lookback-over-730-days"),
    pytest.param({"effective_month": "2026-13"}, id="month-13"),
    pytest.param({"bonus": 150000}, id="unknown-key"),
]


@pytest.mark.parametrize("overrides", SCHEMA_REFUSALS)
def test_a15_refuses_a_malformed_body_before_the_service_sees_it(client, db, admin_claim_headers, frozen, overrides):
    """T-TIER-8 and T-PLAN-3's extra keys included (v4 `default_rate` and flat `rates[]`, and `to_unit`):
    `_PayPayload` forbids unknown keys, so a mistyped money key is refused, never ignored."""
    plan_id = _plan_id(client, admin_claim_headers)

    response = client.post(
        VERSIONS_URL.format(plan_id), json=plan_version_payload(**overrides), headers=admin_claim_headers
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert (body["success"], body["message"]) == (False, "Validation failed")
    assert "error_code" not in body
    assert _versions(plan_id) == 1


SINGLE_FIELD_ROWS = [
    pytest.param({"default_tiers": [{"mode": "percent", "value": 3}]},
                 {"field": "default_tiers.from_unit", "reason": "required", "tier": 1}, id="tier-without-a-first-unit"),
    pytest.param({"default_tiers": [tier(1, "fixed", 3)]},
                 {"field": "default_tiers.mode", "reason": "mode_invalid", "tier": 1}, id="unknown-rate-mode"),
    pytest.param({"rates": [{"tiers": [tier(1, "per_unit", 1500)]}]},
                 {"field": "rates.product_id", "reason": "required", "index": 0},
                 id="product-schedule-without-a-product"),
    pytest.param({"gate_bands": []}, {"field": "gate_bands", "reason": "count"}, id="no-bands"),
    pytest.param({"gate_bands": ELEVEN_BANDS}, {"field": "gate_bands", "reason": "count"}, id="eleven-bands"),
    pytest.param({"gate_bands": [{"min_pct": 100.5, "multiplier": 1}, {"min_pct": 0, "multiplier": 0.5}]},
                 {"field": "gate_bands.min_pct", "reason": "out_of_range", "index": 0}, id="band-over-100-percent"),
    pytest.param({"gate_bands": [{"min_pct": 80, "multiplier": 1.2}, {"min_pct": 0, "multiplier": 0.5}]},
                 {"field": "gate_bands.multiplier", "reason": "out_of_range", "index": 0}, id="multiplier-over-one"),
    pytest.param({"gate_min_visits_due": 10_001}, {"field": "gate_min_visits_due", "reason": "out_of_range"},
                 id="minimum-due-over-10000"),
    pytest.param({"new_outlet_bonus": {**BONUS, "amount": -1}},
                 {"field": "new_outlet_bonus.amount", "reason": "out_of_range"}, id="negative-bonus"),
    pytest.param({"new_outlet_bonus": {**BONUS, "window_days": 366}},
                 {"field": "new_outlet_bonus.window_days", "reason": "out_of_range"}, id="window-over-365-days"),
    pytest.param({"new_outlet_bonus": {**BONUS, "prior_customer_lookback_days": -1}},
                 {"field": "new_outlet_bonus.prior_customer_lookback_days", "reason": "out_of_range"},
                 id="negative-lookback"),
    pytest.param({"new_outlet_bonus": {**BONUS, "min_orders_with_total": 0}},
                 {"field": "new_outlet_bonus.min_orders_with_total", "reason": "out_of_range"},
                 id="zero-orders-with-total"),
]


@pytest.mark.parametrize("overrides,expected", SINGLE_FIELD_ROWS)
def test_validate_refuses_the_single_field_rows_too(db, overrides, expected):
    """The schema answers these over HTTP first. `validate` is still the one statement of the
    §4.10 table, so it refuses them itself for any caller that is not a route."""
    with pytest.raises(ValidationError) as refused:
        SalesPayPlanService.validate(plan_version_payload(**overrides))

    assert (refused.value.error_code, refused.value.details) == ("SALES_PAY_PLAN_INVALID", expected)


def test_validate_returns_the_config_the_resolver_rebuilds(db, admin_user, sample_product):
    """T-TIER-8's round trip: what `validate` accepts is stored and read back as the same `PlanConfig`.
    Tiers ascend, a percent keeps 2 decimals and a per-unit rate is whole, one schedule may mix
    modes (I-35), and bands keep 1 and 3 decimals, in descending order."""
    version = plan_version_payload(
        default_tiers=[tier(1, "percent", 2), tier(1001, "per_unit", 500)],
        rates=[{"product_id": sample_product.id, "tiers": TWO_TIERS_19L}],
    )

    config = SalesPayPlanService.validate(version)

    assert config == PlanConfig(
        default_tiers=TierSchedule(
            tiers=(
                Tier(from_unit=1, rate=Rate(mode="percent", value=Decimal("2.00"))),
                Tier(from_unit=1001, rate=Rate(mode="per_unit", value=Decimal("500"))),
            )
        ),
        product_tiers={sample_product.id: TWO_TIERS_19L_CONFIG},
        gate_bands=(
            GateBand(min_pct=Decimal("80.0"), multiplier=Decimal("1.000")),
            GateBand(min_pct=Decimal("60.0"), multiplier=Decimal("0.800")),
            GateBand(min_pct=Decimal("0.0"), multiplier=Decimal("0.500")),
        ),
        gate_min_visits_due=20,
        bonus_amount=Decimal("150000"),
        bonus_window_days=60,
        bonus_min_orders_with_total=2,
        bonus_min_combined_total=Decimal("300000"),
        bonus_min_orders_any_amount=5,
        bonus_prior_customer_lookback_days=180,
    )
    assert config.schedule_for(sample_product.id) == (TWO_TIERS_19L_CONFIG, False)
    plan = SalesPayPlanService.create_plan("Standard", version, actor_id=admin_user.id, now=IN_OCTOBER)
    resolved = SalesPayPlanService.resolve(plan.id, OCT)
    assert (resolved.plan_name, resolved.version_no, resolved.effective_month, resolved.config) == (
        "Standard",
        1,
        OCT,
        config,
    )
    assert SalesPayPlanService.resolve_version(resolved.version_id).config == config


def test_a_stored_schedule_that_breaks_the_cross_row_rules_is_never_rated(
    client, db, admin_claim_headers, sample_product, frozen
):
    """§3.2: "a default schedule exists" and "a schedule starts at unit 1" span rows, so no CHECK holds
    them and `validate` is their guard. A version written around it is refused when read: A14
    answers an uncoded 500, and `resolve_version` raises rather than rate it. (A repeated first unit
    cannot be stored at all: both uniques refuse it.)"""
    plan_id = _plan_id(client, admin_claim_headers)
    version_id = SalesPayPlanVersion.query.filter_by(plan_id=plan_id).one().id
    db.session.add(
        SalesPayPlanRate(
            plan_version_id=version_id,
            product_id=sample_product.id,
            from_unit=2,
            rate_mode="per_unit",
            rate_value=Decimal("1500"),
        )
    )
    db.session.commit()

    response = client.get(VERSION_URL.format(plan_id, version_id), headers=admin_claim_headers)

    assert response.status_code == 500, response.get_data(as_text=True)
    assert response.get_json().get("error_code") is None
    with pytest.raises(
        RuntimeError,
        match=rf"plan version {version_id}: the product {sample_product.id} schedule is corrupt \(first units \[2\]\)",
    ):
        SalesPayPlanService.resolve_version(version_id)

    SalesPayPlanRate.query.filter_by(plan_version_id=version_id).delete()
    db.session.commit()
    with pytest.raises(RuntimeError, match=rf"plan version {version_id} has no default schedule"):
        SalesPayPlanService.resolve_version(version_id)


# --------------------------------------------------------------------------- #
# Not found, and who may call
# --------------------------------------------------------------------------- #


def test_a14_and_a15_name_what_they_could_not_find(client, db, admin_claim_headers, frozen):
    plan_id = _plan_id(client, admin_claim_headers)
    other_plan_id = _plan_id(client, admin_claim_headers, name="Premium")
    version_id = SalesPayPlanVersion.query.filter_by(plan_id=plan_id).one().id

    missing_plan = client.post(VERSIONS_URL.format(999999), json=plan_version_payload(), headers=admin_claim_headers)
    foreign = client.get(VERSION_URL.format(other_plan_id, version_id), headers=admin_claim_headers)
    missing_version = client.get(VERSION_URL.format(plan_id, 999999), headers=admin_claim_headers)

    answers = [
        (response.status_code, response.get_json()["error_code"], response.get_json()["details"])
        for response in (missing_plan, foreign, missing_version)
    ]
    assert answers == [
        (404, "SALES_PAY_NOT_FOUND", {"resource": "plan", "id": 999999}),
        (404, "SALES_PAY_NOT_FOUND", {"resource": "version", "id": version_id}),
        (404, "SALES_PAY_NOT_FOUND", {"resource": "version", "id": 999999}),
    ]


@pytest.mark.parametrize(
    "headers_fixture", ["manager_claim_headers", "sales_agent_auth_headers", "admin_auth_headers"],
    ids=["manager", "sales-agent", "admin-without-the-role-claim"],
)
def test_only_an_admin_token_reaches_the_plan_routes(request, client, db, admin_claim_headers, frozen, headers_fixture):
    """C11: a plan is pay. A manager never sees it (spec §5.6)."""
    plan_id = _plan_id(client, admin_claim_headers)
    version_id = SalesPayPlanVersion.query.filter_by(plan_id=plan_id).one().id
    headers = request.getfixturevalue(headers_fixture)

    statuses = [
        client.get(PLANS_URL, headers=headers).status_code,
        client.post(PLANS_URL, json={"name": "Shadow", "version": plan_version_payload()}, headers=headers).status_code,
        client.get(VERSION_URL.format(plan_id, version_id), headers=headers).status_code,
        client.post(VERSIONS_URL.format(plan_id), json=plan_version_payload(), headers=headers).status_code,
    ]

    assert statuses == [403, 403, 403, 403]
    assert (SalesPayPlan.query.count(), SalesPayPlanVersion.query.count()) == (1, 1)
