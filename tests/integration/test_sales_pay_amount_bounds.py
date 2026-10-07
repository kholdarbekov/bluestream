"""Every pay money input is bounded, so a typo is a 400, never a 500 or a month nobody can close.

Final-review M2. `shared.business_config.MAX_PAY_AMOUNT` (10,000,000,000 UZS per input) is
checked by `pay_rules.check_amount_bound` before any rounding, in each family the admin pay pages
post a sum through: an adjustment (A11), the base salary (A17), a direct penalty (A23), a penalty
type's default (A20) and the plan's new-outlet bonus (A13). Without it:
- "1e15" overflows `Numeric(14,2)` on Postgres and "1e30" makes `round_uzs` raise
  InvalidOperation, a 500 on any database;
- an adjustment of 999,999,999,999 fits alone, but the agent's gross does not, and A3 close
  freezes every agent in one transaction, so the whole month's close 500s for everyone.
S2's page is bounded too: `page=10**19` overflowed the OFFSET.
"""

import pytest

from shared.business_config import MAX_PAY_AMOUNT
from tests.integration.sales_pay_builders import (
    PAY_API,
    PENALTY_TYPE_NAMES,
    add_penalty_type,
    incident_body,
    local_at,
    move_clock,
    pay_call,
    pay_world,
    refusal,
    tier,
    version_payload,
)

pytestmark = pytest.mark.integration

TOO_MUCH = 10**13
LINES = "/api/v1/staff/sales/me/earnings/lines"


@pytest.fixture
def world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    return pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)


def _refused(world, method, path, body, *, field):
    details = refusal(
        world.client, method, f"{PAY_API}{path}", world.headers, body, status=400, code="SALES_PAY_AMOUNT_INVALID"
    )
    assert (details["field"], details["max"]) == (field, MAX_PAY_AMOUNT), details


def test_the_bound_is_ten_billion_uzs():
    assert MAX_PAY_AMOUNT == 10_000_000_000


@pytest.mark.parametrize("amount", [TOO_MUCH, -TOO_MUCH, 1e30, "1E+30"])
def test_an_adjustment_past_the_bound_is_refused_not_a_server_error(world, amount):
    _refused(
        world,
        "POST",
        f"/periods/2026-10/agents/{world.agent.id}/adjustments",
        {"amount": amount, "reason": "Typed one zero too many"},
        field="amount",
    )


def test_an_adjustment_at_the_bound_is_accepted(world):
    pay_call(
        world.client,
        "POST",
        f"{PAY_API}/periods/2026-10/agents/{world.agent.id}/adjustments",
        world.headers,
        {"amount": -MAX_PAY_AMOUNT, "reason": "The largest deduction one input may carry"},
        status=201,
    )


def test_a_base_salary_past_the_bound_is_refused(world):
    _refused(
        world,
        "POST",
        f"/agents/{world.agent.id}/terms",
        {"effective_month": "2026-11", "base_salary": TOO_MUCH, "plan_id": world.plan["id"]},
        field="base_salary",
    )


def test_a_penalty_type_default_past_the_bound_is_refused(world):
    _refused(world, "POST", "/penalty-types", {"names": PENALTY_TYPE_NAMES, "default_amount": TOO_MUCH}, field="default_amount")


def test_a_direct_penalty_past_the_bound_is_refused(world):
    type_id = add_penalty_type(world, default_amount=150000)
    move_clock(world, local_at(2026, 10, 15))
    _refused(world, "POST", "/penalties", incident_body(world.agent, type_id, "2026-10-09", amount=TOO_MUCH), field="amount")


@pytest.mark.parametrize(
    "knob, field",
    [("amount", "new_outlet_bonus.amount"), ("min_combined_total", "new_outlet_bonus.min_combined_total")],
)
def test_a_plan_bonus_past_the_bound_is_refused(world, knob, field):
    version = version_payload("2026-11", world.products.water.id, **{knob: TOO_MUCH})
    _refused(world, "POST", "/plans", {"name": "Typo plan", "version": version}, field=field)


@pytest.mark.parametrize(
    "tiers, field, position",
    [
        pytest.param([tier(1, "per_unit", 1e30)], "rates.tiers.value", 1, id="rate-1e30"),
        pytest.param([tier(1, "per_unit", "1E+30")], "rates.tiers.value", 1, id="rate-1E+30"),
        pytest.param([tier(1, "percent", 3), tier(10**19, "percent", 4)], "rates.tiers.from_unit", 2, id="first-unit-10^19"),
    ],
)
def test_a_tier_typo_is_a_400_not_a_server_error(world, tiers, field, position):
    """A tier's numbers are bounded by `SalesPayPlanService.validate` (rate <= MAX_PER_UNIT_RATE,
    first unit <= MAX_FROM_UNIT) before `round_uzs` or the integer column sees them, so a typo is the
    coded refusal that names the tier and its product, never a 500."""
    version = version_payload("2026-11", world.products.water.id, water_tiers=tiers)

    details = refusal(
        world.client,
        "POST",
        f"{PAY_API}/plans",
        world.headers,
        {"name": "Typo plan", "version": version},
        status=400,
        code="SALES_PAY_PLAN_INVALID",
    )

    assert details == {
        "field": field,
        "reason": "out_of_range",
        "tier": position,
        "product_id": world.products.water.id,
        "validation_errors": [],
    }


def test_an_earnings_page_past_any_real_page_is_a_400_not_an_offset_overflow(world, client, sales_agent_auth_headers):
    response = client.get(f"{LINES}?month=2026-10&page={10**19}", headers=sales_agent_auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
