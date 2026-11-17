"""REAL POSTGRES: two linked phones placing their orders at the same moment get one courtesy (spec §5.6, §6).

Spec §6, "Two orders at the same moment by one person": one order gets the courtesy; the other is refused with its new
total and redrawn. A orders from the bot (`POST /api/v1/orders/`, source "telegram"). B, a phone linked to A, orders
at the same moment from the website (the same route, source "web") or through the operator's phone order
(`POST /api/v1/staff/operator/orders`). Both screens showed the courtesy total, 76,000, and both post it as
`expected_total`.

Order creation claims the courtesy before `Order(...)` takes its `order_sequences` row: `apply_courtesy(...,
claim=True)` locks every entry of the person, ordered by id, and the order marks the entry used (`mark_used`) and
commits in that same transaction. The three channels number their orders from three different `order_sequences` rows
(TG, WB, CC), so the only lock B can wait on is the courtesy's. B waits, then reads the entry A committed: used. B is
priced at 80,000, the honest confirm refuses the 76,000 its screen showed with the new quote, and nothing of B's order
is written. B re-posting the 80,000 it is now shown is placed.

- `one_entry`: only A has a delivered order before the notice, so the switch lists A alone; B is linked afterwards.
- `two_entries`: A and B were two people at the switch, each with a delivered order before the notice, so each has an
  entry; they are linked afterwards, and the claim must lock both.
- `operator`: B's order is the operator's phone order, whose claim, `mark_used` and commit are
  `StaffService.create_phone_order`'s own, and whose 409 is the staff envelope (`details.reason`, `details.quote`).

`with_for_update` compiles to nothing on SQLite, so this runs on a fully-migrated Postgres (`pg_app`). The
interleaving is forced the way test_customer_cancel_races_pg.py forces its own: A is held inside `mark_used` (the
claim's locks taken, the entry not yet written) until the test releases it, and B starts only then. Each order runs
on its own thread, app context, session and connection, as two gunicorn workers would; every wait is bounded. The
world comes from `price_courtesy_builders.py`, and the totals are its figures (76,000 with the courtesy, 80,000
without), never computed by the code under test.
"""

import os
import threading

import pytest
from flask import current_app

from business_app.models.order import Order, OrderItem
from business_app.models.price_change import PriceCourtesy
from business_app.services.price_change import courtesy as courtesy_module
from shared.enums import UserRole
from tests.integration.contract_pricing_factory import make_staff
from tests.integration.price_change_builders import bottles, make_admin
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    NEW_TOTAL,
    announced_change,
    delivered_before_notice,
    link_through_service,
    person,
    switch,
    water,
)
from tests.integration.reorder_reminder_builders import claim_headers
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/orders/"
OPERATOR_CREATE = "/api/v1/staff/operator/orders"


def _items(product):
    return [{"product_id": product.id, "quantity": BASKET}]


def _bot_body(product, customer, expected_total):
    """The body the customer bot posts when the customer confirms, with the total its screen showed."""
    return {
        "items": _items(product),
        "delivery_address_id": customer.address.id,
        "payment_method": "cash",
        "source": "telegram",
        "expected_total": expected_total,
    }


def _web_body(product, customer, expected_total):
    """What the website's checkout posts (`orderBody` in static/js/pages/checkout.js), with the total it showed."""
    return {
        "items": _items(product),
        "delivery_address_id": customer.address.id,
        "delivery_date": None,
        "delivery_window_start": None,
        "delivery_window_end": None,
        "payment_method": "cash",
        "delivery_notes": None,
        "source": "web",
        "expected_total": expected_total,
    }


def _operator_body(product, customer, expected_total):
    """The body the operator bot posts when it confirms (`create_order.py`), with the total the operator read out."""
    return {
        "client_id": customer.user.id,
        "items": _items(product),
        "delivery_address_id": customer.address.id,
        "payment_method": "cash",
        "delivery_notes": None,
        "expected_total": expected_total,
    }


# B's channel -> (create route, the body its client posts, the created order's id in a 201's body)
SECOND = {
    "web": (ORDERS, _web_body, lambda body: body["data"]["order"]["id"]),
    "operator": (OPERATOR_CREATE, _operator_body, lambda body: body["data"]["id"]),
}


def _orders_with_a_courtesy(pg_db, user_ids):
    """The ids of the person's orders that carry a courtesy line."""
    rows = (
        pg_db.session.query(Order.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(Order.user_id.in_(user_ids), OrderItem.courtesy_quantity.isnot(None))
        .distinct()
    )
    return sorted(order_id for (order_id,) in rows)


@pytest.mark.parametrize("entries, second", [("one_entry", "web"), ("two_entries", "web"), ("one_entry", "operator")])
def test_two_linked_phones_ordering_at_once_get_one_courtesy(pg_app, pg_db, monkeypatch, entries, second):
    # `pg_app` is built without the suite's REDIS_URL, and an order reserves its stock in Redis: the precedent of
    # test_sales_agent_order_hold_pg.py.
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    product = water(pg_db, bottles(pg_db).p19)
    admin = make_admin(pg_db)
    a, b = person(pg_app, pg_db, "+998901531001"), person(pg_app, pg_db, "+998901531002")
    delivered_before_notice(pg_db, a.user, a.address, product)
    if entries == "two_entries":
        delivered_before_notice(pg_db, b.user, b.address, product)
    announced_change(pg_db, product, admin)
    switch(pg_db)
    link_through_service(pg_db, a.user, b.user, admin)
    listed = [entry.user_id for entry in PriceCourtesy.query.order_by(PriceCourtesy.id)]
    assert listed == ([a.user.id] if entries == "one_entry" else [a.user.id, b.user.id])

    # Everything the threads send is built here, on the test's own session.
    a_id, b_id = a.user.id, b.user.id
    route, body_of, order_id_of = SECOND[second]
    a_body, a_headers = _bot_body(product, a, 76000.0), a.headers
    b_body = body_of(product, b, 76000.0)
    if second == "web":
        b_headers = b.headers
    else:  # the operator taking B's call
        b_headers = claim_headers(pg_app, make_staff(pg_db, UserRole.OPERATOR, "+998901531003"), "operator")
    b_orders_before = Order.query.filter(Order.user_id == b_id).count()

    first_holds_the_courtesy, release_first = threading.Event(), threading.Event()
    real_mark_used = courtesy_module.mark_used

    def mark_used(entry, order):
        """A's order stops here, the claim's locks held and the entry not yet written, until the test releases it."""
        if threading.current_thread().name == "first":
            first_holds_the_courtesy.set()
            assert release_first.wait(timeout=30), "the test never released the first order"
        real_mark_used(entry, order)

    monkeypatch.setattr(courtesy_module, "mark_used", mark_used)

    answers, errors = {}, []
    first = _thread(
        "first",
        pg_app,
        lambda client: client.post(ORDERS, json=a_body, headers=a_headers),
        answers,
        errors,
        finally_set=first_holds_the_courtesy,
    )
    second_thread = _thread(
        "second", pg_app, lambda client: client.post(route, json=b_body, headers=b_headers), answers, errors
    )

    first.start()
    try:
        assert first_holds_the_courtesy.wait(timeout=30), "the first order never reached mark_used"
        second_thread.start()
        assert _await_a_lock_waiter(pg_db), "the second order never waited on the courtesy's row lock"
    finally:
        release_first.set()
        _finish(first, *([second_thread] if second_thread.ident else []))

    assert errors == [] and answers["first"][0] == 201, (errors, answers.get("first"))
    status, body = answers["second"]
    assert status == 409, body
    if second == "web":  # the customer envelope
        assert body["data"]["reason"] == "price_changed"
        assert body["data"]["quote"]["pricing"]["final_total"] == 80000.0
        assert body["data"]["quote"]["price_courtesy"] is None
    else:  # "operator": the staff envelope
        assert body["details"]["reason"] == "price_changed"
        assert body["details"]["quote"]["total_amount"] == 80000.0

    first_order_id = answers["first"][1]["data"]["order"]["id"]
    pg_db.session.expire_all()
    assert pg_db.session.get(Order, first_order_id).total_amount == COURTESY_TOTAL
    assert Order.query.filter(Order.user_id == b_id).count() == b_orders_before
    assert _orders_with_a_courtesy(pg_db, [a_id, b_id]) == [first_order_id]
    holders = PriceCourtesy.query.filter(PriceCourtesy.order_id.isnot(None)).all()
    assert [entry.order_id for entry in holders] == [first_order_id]

    # B's screen is redrawn with the 409's quote, and B confirms the 80,000 it now shows.
    again = pg_app.test_client().post(route, json={**b_body, "expected_total": 80000.0}, headers=b_headers)

    assert again.status_code == 201, again.get_json()
    pg_db.session.expire_all()
    assert pg_db.session.get(Order, order_id_of(again.get_json())).total_amount == NEW_TOTAL
    assert _orders_with_a_courtesy(pg_db, [a_id, b_id]) == [first_order_id]
