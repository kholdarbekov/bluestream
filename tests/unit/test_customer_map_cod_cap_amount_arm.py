"""The admin customer map must colour pins with the SSOT cap, amount arm included.

If the map keeps its own rule, it shows a customer as blocked while
``create_order`` accepts their cash order — or the reverse.
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_map_service import CustomerMapService
from shared.business_config import COD_ACTIVE_DEBT_LIMIT
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_place_group, make_user


@pytest.mark.unit
def test_pin_is_not_restricted_for_tiny_debts(db):
    u = make_user(db)
    a = make_address(db, u)
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, u, address=a, total=Decimal("280.00"))

    pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id)
    assert pin["active_cod_debt_count"] == COD_ACTIVE_DEBT_LIMIT
    assert pin["cod_restricted"] is False


@pytest.mark.unit
def test_pin_is_restricted_for_real_debts(db):
    u = make_user(db)
    a = make_address(db, u)
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, u, address=a, total=Decimal("6000.00"))

    pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id)
    assert pin["cod_restricted"] is True


@pytest.mark.unit
def test_pin_is_not_restricted_for_one_large_debt(db):
    u = make_user(db)
    a = make_address(db, u)
    delivered_cod_order(db, u, address=a, total=Decimal("50000.00"))

    pin = next(p for p in CustomerMapService.get_customer_map_pins() if p["user_id"] == u.id)
    assert pin["cod_restricted"] is False


@pytest.mark.unit
def test_every_pin_matches_the_service_answer(db):
    """No pin — and no member row of a place pin — may carry a locally-derived flag."""
    tiny, real, exempt = make_user(db), make_user(db), make_user(db, exempt=True)
    # The same shapes at a shared place, so the member-row branch below is
    # exercised rather than vacuous.
    office_tiny, office_real = make_user(db), make_user(db)
    office = {user: make_address(db, user) for user in (office_tiny, office_real)}
    make_place_group(db, *office.values())
    for user in (tiny, real, exempt, office_tiny, office_real):
        addr = office.get(user) or make_address(db, user)
        for _ in range(COD_ACTIVE_DEBT_LIMIT):
            delivered_cod_order(
                db, user, address=addr,
                total=Decimal("280.00") if user in (tiny, office_tiny) else Decimal("6000.00"),
            )

    svc = CashCollectionService()
    pins = CustomerMapService.get_customer_map_pins()
    assert [p["kind"] for p in pins].count("place") == 1
    for pin in pins:
        if pin["kind"] == "place":
            assert {m["user_id"] for m in pin["members"]} == {office_tiny.id, office_real.id}
            for member in pin["members"]:
                assert member["cod_restricted"] is svc.is_customer_cod_restricted(member["user_id"]), member
        else:
            assert pin["cod_restricted"] is svc.is_customer_cod_restricted(pin["user_id"]), pin["user_id"]


@pytest.mark.unit
def test_module_holds_no_copy_of_the_cap_rule(db):
    """Regression pin: the deleted inline fallback must not come back."""
    import business_app.services.customer_map_service as module

    assert not hasattr(module, "_COD_LIMIT")
