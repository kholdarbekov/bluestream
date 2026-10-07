"""A16-A18: the agent's Pay tab (§4.10, §5.2), and the closed-month re-freeze behind A7, A10
and A18 (§4.9 "Re-freeze on input change").

An employment change touches every month whose worked days change AND every month whose
carry-versus-owed answer flips (`pay_calculator.employed_next_month`, I-26). An affected
closed month is re-frozen in the same transaction, and an affected approved month refuses the
whole change (`SALES_PAY_MONTH_LOCKED`, `details.months`).
"""

from datetime import date

import pytest

from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesPayStatement
from tests.integration.sales_pay_builders import (
    PAY_API,
    TEACHERS_DAY,
    below_zero_october,
    freeze_local_now,
    local_at,
    make_product,
    move_clock,
    pay_call,
    pay_route,
    pay_world,
    version_payload,
    water_order,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def make_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    def make(**options):
        return pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, **options)

    return make


def october(world) -> dict:
    return pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}")


def test_the_pay_tab_round_trip(client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    """A16 before anything; A17 refused without an employment start; A18 sets it; A17 adds
    terms (201, the A16 shape) and refuses a fractional base, an unknown plan, a user with no
    agent profile and a mistyped key; A16 of an unknown user is a 404."""
    freeze_local_now(monkeypatch, local_at(2026, 10, 1, 9))
    agent = sales_agent_user.id
    tab = f"{PAY_API}/agents/{agent}"
    water = make_product(db, name="Water 19L", price="20000")
    plan = pay_call(
        client, "POST", f"{PAY_API}/plans", admin_claim_headers,
        {"name": "Standard", "version": version_payload("2026-10", water.id)}, status=201,
    )["plan"]
    terms = {"effective_month": "2026-10", "base_salary": "3000000", "plan_id": plan["id"]}

    empty = pay_call(client, "GET", f"{tab}/terms", admin_claim_headers)
    no_start = pay_call(client, "POST", f"{tab}/terms", admin_claim_headers, terms, status=400)
    employed = pay_call(client, "PUT", f"{tab}/employment", admin_claim_headers, {"start": "2026-10-01", "end": None})
    added = pay_call(client, "POST", f"{tab}/terms", admin_claim_headers, {**terms, "note": "Hired on 3,000,000"}, status=201)
    fractional = pay_call(client, "POST", f"{tab}/terms", admin_claim_headers, {**terms, "base_salary": "1.5"}, status=400)
    unknown_plan = pay_call(client, "POST", f"{tab}/terms", admin_claim_headers, {**terms, "plan_id": 999999}, status=404)
    not_an_agent = pay_call(
        client, "POST", f"{PAY_API}/agents/{admin_user.id}/terms", admin_claim_headers, terms, status=404
    )
    pay_call(client, "POST", f"{tab}/terms", admin_claim_headers, {**terms, "base": 1}, status=400)
    unknown_user = pay_call(client, "GET", f"{PAY_API}/agents/999999/terms", admin_claim_headers, status=404)

    assert empty == {
        "agent": {"user_id": agent, "name": "Sardor Agent", "phone": sales_agent_user.phone, "is_active": True},
        "employment": {"start": None, "end": None},
        "owed_to_date": {"amount": 0.0, "month": None, "nets_in": None},
        "terms": [],
        "term_in_force": None,
        "plans": [{"id": plan["id"], "name": "Standard"}],
        "editable_from_month": "2026-10",
    }
    assert (no_start["error_code"], no_start["details"]["field"]) == ("SALES_PAY_TERMS_INVALID", "employment_start_date")
    assert employed["employment"] == {"start": "2026-10-01", "end": None}
    row = added["term_in_force"]
    assert {key: row[key] for key in ("effective_month", "base_salary", "plan", "note", "created_by")} == {
        "effective_month": "2026-10",
        "base_salary": 3000000.0,
        "plan": {"id": plan["id"], "name": "Standard"},
        "note": "Hired on 3,000,000",
        "created_by": {"id": admin_user.id, "name": "Admin User"},
    }
    assert added["terms"] == [row]
    assert (fractional["error_code"], fractional["details"]) == (
        "SALES_PAY_AMOUNT_INVALID",
        {"field": "base_salary", "validation_errors": []},
    )
    assert (unknown_plan["error_code"], unknown_plan["details"]) == ("SALES_PAY_NOT_FOUND", {"resource": "plan", "id": 999999})
    assert (not_an_agent["error_code"], not_an_agent["details"]["resource"]) == ("SALES_PAY_NOT_FOUND", "agent")
    assert (unknown_user["error_code"], unknown_user["details"]) == ("SALES_PAY_NOT_FOUND", {"resource": "agent", "id": 999999})


def test_an_end_date_moves_a_closed_months_shortfall_between_carry_and_owed(make_world):
    """I-26 through A18, the adjustment-driven mechanics of T-OWED-1 (a)-(d). October is the
    carry case (gross -100,000) and closed on 2 November with carry_out -100,000 (revision 1).

    - end 2026-11-01: October's worked days and its employed-next-month answer are unchanged
      (only 1 November, a November day, moves), so October is not touched (still revision 1, carried);
    - end 2026-10-31: October flips (no November employment): revision 2, owed 100,000;
    - end cleared: flips back: revision 3, carried again;
    - once October is approved, end 2026-10-31 is refused naming October, and nothing changes;
    - end 2026-11-01 is still accepted: it affects only November, which is open."""
    world = make_world(base=3000000)
    below_zero_october(world)
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    employment = f"/agents/{world.agent.id}/employment"

    def end_at(end, *, status=200):
        return pay_route(world, "PUT", employment, {"start": "2026-10-01", "end": end}, status=status)

    end_at("2026-11-01")
    end_on_1_november = october(world)
    end_at("2026-10-31")
    leaver = october(world)
    end_at(None)
    carried_again = october(world)
    pay_route(world, "POST", "/periods/2026-10/approve")
    refused = end_at("2026-10-31", status=409)
    profile = SalesAgentProfile.query.filter_by(user_id=world.agent.id).one()
    end_before_the_refusal = profile.employment_end_date
    end_at("2026-11-01")

    def split(statement):
        return statement["revision"], statement["summary"]["carry_out"], statement["summary"]["owed"]

    assert split(end_on_1_november) == (1, -100000.0, 0.0)
    assert split(leaver) == (2, 0.0, 100000.0)
    assert split(carried_again) == (3, -100000.0, 0.0)
    assert (refused["error_code"], refused["details"]) == ("SALES_PAY_MONTH_LOCKED", {"months": ["2026-10"]})
    assert end_before_the_refusal is None
    assert SalesPayStatement.query.filter_by(agent_user_id=world.agent.id).one().revision == 3


def test_unpaid_days_and_holidays_in_a_closed_month_refreeze_it_and_the_closed_months_after_it(make_world):
    """§4.9: October and November are both closed. A10 on October (the 12th unpaid) re-freezes
    October (31 working days, 30 worked: 2,600,000 x 30 / 31 = 2,516,129.03 -> 2,516,129) and,
    because November reads October's gate for any October line, November too. A7 on October (1
    October a holiday) re-freezes both again: 30 working, 29 worked: 2,600,000 x 29 / 30 =
    2,513,333.33 -> 2,513,333. A10 answers the re-frozen October statement."""
    world = make_world(base=2600000)
    water_order(world, 6, date(2026, 10, 20))
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    water_order(world, 6, date(2026, 11, 10))
    move_clock(world, local_at(2026, 12, 2))
    pay_route(world, "POST", "/periods/2026-11/close")
    agent = world.agent.id

    unpaid = pay_route(world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": "2026-10-12"}]})
    november_after_unpaid = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")["revision"]
    holidays = pay_route(world, "PUT", "/periods/2026-10/holidays", TEACHERS_DAY)
    after_holiday = october(world)
    november_after_holiday = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")["revision"]

    assert (unpaid["revision"], unpaid["inputs"]["worked_days"], unpaid["summary"]["base_amount"]) == (2, 30, 2516129.0)
    assert unpaid["inputs"]["unpaid_days"] == [{"date": "2026-10-12", "note": None}]
    assert november_after_unpaid == 2
    assert (holidays["status"], holidays["holidays"]) == ("closed", [{"date": "2026-10-01", "note": "Teachers' Day"}])
    assert (
        after_holiday["revision"],
        after_holiday["inputs"]["working_days"],
        after_holiday["inputs"]["worked_days"],
        after_holiday["summary"]["base_amount"],
    ) == (3, 30, 29, 2513333.0)
    assert november_after_holiday == 3
