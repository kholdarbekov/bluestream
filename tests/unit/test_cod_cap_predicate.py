"""The COD debt cap predicate — both arms, and only both arms.

Reads the thresholds from the config SSOT rather than restating 2 / 10 000, so
an env override in any environment cannot make this suite lie about the rule.
"""

from decimal import Decimal

import pytest

from business_app.utils.cod_cap import cod_cap_reached, strip_place_scope_money_for_customer
from shared.business_config import COD_ACTIVE_DEBT_LIMIT, COD_DEBT_AMOUNT_THRESHOLD


@pytest.mark.unit
def test_count_arm_alone_does_not_cap():
    """The motivating case: two shortfalls of 280 sum each.

    At the count limit, nowhere near the amount floor. Under the old count-only
    rule this customer lost the cash rail over 560 sum.
    """
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT, Decimal("560.00")) is False


@pytest.mark.unit
def test_amount_arm_alone_does_not_cap():
    """One big debt: over the amount floor, under the count limit."""
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT - 1, Decimal("50000.00")) is False


@pytest.mark.unit
def test_both_arms_cap():
    over = Decimal(COD_DEBT_AMOUNT_THRESHOLD) + Decimal("0.01")
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT, over) is True


@pytest.mark.unit
def test_more_than_the_count_limit_still_caps():
    over = Decimal(COD_DEBT_AMOUNT_THRESHOLD) + Decimal("1.00")
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT + 3, over) is True


@pytest.mark.unit
def test_amount_arm_is_strictly_greater_than_the_threshold():
    """Exactly at the threshold is NOT capped — the arm is `>`, never `>=`."""
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT, Decimal(COD_DEBT_AMOUNT_THRESHOLD)) is False


@pytest.mark.unit
def test_zero_debts_never_cap():
    assert cod_cap_reached(0, Decimal("0.00")) is False


@pytest.mark.unit
def test_a_float_total_is_coerced_not_compared_as_a_float():
    """Read surfaces publish money as float; the predicate must still be exact."""
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT, float(COD_DEBT_AMOUNT_THRESHOLD)) is False
    assert cod_cap_reached(COD_ACTIVE_DEBT_LIMIT, float(COD_DEBT_AMOUNT_THRESHOLD) + 1.0) is True


@pytest.mark.unit
def test_service_constant_is_the_config_constant():
    """The service class attribute is a re-export, never a second literal."""
    from business_app.services.cash_collection_service import CashCollectionService

    assert CashCollectionService.COD_ACTIVE_DEBT_LIMIT == COD_ACTIVE_DEBT_LIMIT


# ---------------------------------------------------------------------------
# Task 30A: the customer-facing redaction of `get_cod_restriction_context`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_strip_place_scope_money_drops_only_that_one_key():
    context = {
        "active_cod_debt_count": 0,
        "place_active_cod_debt_count": 2,
        "cluster_net_open_cod_debt_total": 0.0,
        "place_net_open_cod_debt_total": 12000.0,
        "cod_restricted": True,
        "restriction_scope": "place",
    }

    safe = strip_place_scope_money_for_customer(context)

    assert "place_net_open_cod_debt_total" not in safe
    # Nothing else moves -- this is a targeted strip, not a blanket filter.
    assert safe == {
        "active_cod_debt_count": 0,
        "place_active_cod_debt_count": 2,
        "cluster_net_open_cod_debt_total": 0.0,
        "cod_restricted": True,
        "restriction_scope": "place",
    }


@pytest.mark.unit
def test_strip_place_scope_money_is_a_no_op_when_the_key_is_absent():
    """The person-only path (no delivery_address_id) never had the key —
    stripping it must not raise or otherwise change the dict's shape."""
    context = {"active_cod_debt_count": 0, "cod_restricted": False}

    assert strip_place_scope_money_for_customer(context) == context


@pytest.mark.unit
def test_strip_place_scope_money_does_not_mutate_the_input():
    """Internal callers (order_cash_edit_service, staff/admin endpoints) share
    the same dict this function is handed; it must return a copy."""
    context = {"place_net_open_cod_debt_total": 12000.0}

    strip_place_scope_money_for_customer(context)

    assert context == {"place_net_open_cod_debt_total": 12000.0}
