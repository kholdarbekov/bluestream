"""The courtesy status staff screens and the customer page read (spec §5.6): one answer per person, whichever of
their linked phones is asked, formatted by the one serializer."""

from datetime import timedelta

import pytest

from business_app.serializers.price_courtesy_serializers import serialize_courtesy_status
from business_app.services.price_change.courtesy import courtesy_status
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_change_builders import (
    APPLIED_AT,
    IN_WINDOW,
    WINDOW_CLOSED,
    applied_change,
    bottles,
    courtesy_entry,
    make_admin,
)
from tests.integration.reorder_reminder_builders import link, live_order, make_customer

pytestmark = pytest.mark.integration


def _world(db):
    b = bottles(db)
    pc = applied_change(db, b, created_by=make_admin(db))
    ali, ali2 = make_customer(db), make_customer(db)
    link(db, ali, ali2)
    return b, pc, ali, ali2


def _status(user, at):
    return serialize_courtesy_status(courtesy_status(user.id, at=at))


def test_an_eligible_person_sees_it_on_every_linked_phone(db):
    b, pc, ali, ali2 = _world(db)
    courtesy_entry(db, pc, ali)
    expected = {
        "state": "available",
        "expires_on": pc.courtesy_last_day.isoformat(),
        "caps": [
            {"product_id": b.p19.id, "product_name": b.p19.name, "cap": 2},
            {"product_id": b.p10.id, "product_name": b.p10.name, "cap": 3},
        ],
        "used_order_id": None,
        "used_order_number": None,
    }
    assert _status(ali, IN_WINDOW) == expected
    assert _status(ali2, IN_WINDOW) == expected


def test_once_used_every_phone_sees_the_order(db):
    b, pc, ali, ali2 = _world(db)
    order = live_order(db, ali, APPLIED_AT + timedelta(days=1))
    courtesy_entry(db, pc, ali, order=order)
    expected = {
        "state": "used",
        "expires_on": pc.courtesy_last_day.isoformat(),
        "caps": [],
        "used_order_id": order.id,
        "used_order_number": order.order_number,
    }
    assert _status(ali, IN_WINDOW) == expected
    assert _status(ali2, IN_WINDOW) == expected


def test_two_entries_in_one_person_and_one_used_reads_used(db):
    b, pc, ali, ali2 = _world(db)
    order = live_order(db, ali2, APPLIED_AT + timedelta(days=1))
    courtesy_entry(db, pc, ali)
    courtesy_entry(db, pc, ali2, order=order)
    for user in (ali, ali2):
        status = _status(user, IN_WINDOW)
        assert status["state"] == "used"
        assert status["used_order_id"] == order.id
        assert status["caps"] == []


@pytest.mark.parametrize(
    "at_name,state",
    [("before", "none"), ("in", "available"), ("last", "available"), ("closed", "none")],
)
def test_it_exists_only_inside_the_window(db, at_name, state):
    b, pc, ali, _ali2 = _world(db)
    courtesy_entry(db, pc, ali)
    at = {
        "before": APPLIED_AT - timedelta(seconds=1),
        "in": IN_WINDOW,
        "last": WINDOW_CLOSED - timedelta(seconds=1),
        "closed": WINDOW_CLOSED,
    }[at_name]
    assert _status(ali, at)["state"] == state


def test_a_person_without_an_entry_has_none(db):
    _b, _pc, ali, _ali2 = _world(db)
    assert _status(ali, IN_WINDOW) == {
        "state": "none",
        "expires_on": None,
        "caps": [],
        "used_order_id": None,
        "used_order_number": None,
    }


def test_an_account_under_contract_has_none_even_with_an_entry(db):
    b = bottles(db)
    pc = applied_change(db, b, created_by=make_admin(db))
    account, _office = make_workplace(db)
    contract, _row = make_contract(db, account, b.p19)
    contract.start_date = APPLIED_AT - timedelta(days=30)
    db.session.commit()
    courtesy_entry(db, pc, account)
    assert _status(account, IN_WINDOW)["state"] == "none"
