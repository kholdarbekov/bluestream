"""GET /api/v1/orders/reorder-info is the backend's one answer to two questions: "what would a
reorder be" and "is an order already open" (spec 2026-10-21 §3, R8, R9). Every bot reorder path
reads this answer and works nothing out itself, so it is pinned here through the route the bot
calls, with the customer's own JWT.
"""
from types import SimpleNamespace

import pytest

from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from shared.enums import OrderStatus, SubscriptionStatus
from tests.integration.reorder_reminder_builders import (
    add_order_lines,
    at,
    customer_headers,
    delivered_order,
    home_address,
    legacy_delivered_order,
    live_order,
    make_customer,
    make_product,
    subscription,
    subscription_delivery,
)

pytestmark = pytest.mark.integration

ROUTE = "/api/v1/orders/reorder-info"


def _reorder_info(app, client, user):
    resp = client.get(ROUTE, headers=customer_headers(app, user))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]


@pytest.fixture
def shop(db, sample_category):
    return SimpleNamespace(water=make_product(db, sample_category, "Water 19L"),
                           cups=make_product(db, sample_category, "Cooler cups"))


def test_the_candidate_is_the_newest_one_off_delivery_without_its_free_line(app, client, db, shop):
    alice = make_customer(db)
    home = home_address(db, alice)
    add_order_lines(db, delivered_order(db, alice, at(-20)), [(shop.cups, 9)])
    usual = add_order_lines(db, delivered_order(db, alice, at(-10)), [(shop.water, 5), (shop.cups, 2)],
                            reward_lines=[(shop.water, 1)])
    plan = subscription(db, alice, status=SubscriptionStatus.ACTIVE)
    add_order_lines(db, subscription_delivery(db, alice, plan, at(-2)), [(shop.water, 8)])  # R8: skipped

    assert _reorder_info(app, client, alice) == {
        "candidate": {
            "order_id": usual.id,
            "order_number": usual.order_number,
            "items": [
                {"product_id": shop.water.id, "product_name": "Water 19L", "quantity": 5},
                {"product_id": shop.cups.id, "product_name": "Cooler cups", "quantity": 2},
            ],
            "address": {"id": home.id, "title": "home", "full_address": home.full_address},
        },
        "open_order": None,
    }


def test_a_delivered_order_with_no_delivered_history_row_is_never_the_candidate(app, client, db, shop):
    alice = make_customer(db)
    dated = add_order_lines(db, delivered_order(db, alice, at(-10)), [(shop.water, 2)])
    add_order_lines(db, legacy_delivered_order(db, alice, at(-1)), [(shop.water, 7)])

    assert _reorder_info(app, client, alice)["candidate"]["order_id"] == dated.id


def test_no_candidate_when_every_delivery_is_a_subscription_or_only_a_free_line(app, client, db, shop):
    alice = make_customer(db)
    plan = subscription(db, alice, status=SubscriptionStatus.CANCELLED)
    add_order_lines(db, subscription_delivery(db, alice, plan, at(-10)), [(shop.water, 4)])
    add_order_lines(db, delivered_order(db, alice, at(-5)), reward_lines=[(shop.water, 1)])

    assert _reorder_info(app, client, alice) == {"candidate": None, "open_order": None}


def test_the_address_is_null_when_it_belongs_to_another_account(app, client, db, shop):
    """The one reachable "not one of her saved addresses": a delivered order cannot lose its
    address (FK-refused delete, ck_orders_address_required_after_pending)."""
    alice = make_customer(db)
    someone_elses = home_address(db, make_customer(db))
    order = add_order_lines(db, delivered_order(db, alice, at(-1), address=someone_elses), [(shop.water, 3)])

    candidate = _reorder_info(app, client, alice)["candidate"]

    assert (candidate["order_id"], candidate["address"]) == (order.id, None)


@pytest.mark.parametrize("status", sorted(ACTIVE_ORDER_STATUSES, key=lambda s: s.value), ids=lambda s: s.value)
def test_the_newest_order_in_an_active_status_is_the_open_order(app, client, db, status):
    alice = make_customer(db)
    delivered_order(db, alice, at(-5))
    live_order(db, alice, at(-3), status=OrderStatus.PENDING)
    newest = live_order(db, alice, at(-1), status=status)

    assert _reorder_info(app, client, alice)["open_order"] == {
        "id": newest.id, "order_number": newest.order_number, "status": status.value,
    }


def test_an_open_order_awaiting_a_new_date_says_so(app, client, db):
    """The status the customer is shown everywhere else (`display_status`), not the raw row."""
    alice = make_customer(db)
    order = live_order(db, alice, at(-1), status=OrderStatus.CONFIRMED, failed=True)

    assert _reorder_info(app, client, alice)["open_order"] == {
        "id": order.id, "order_number": order.order_number, "status": "awaiting_new_date",
    }


def test_an_open_subscription_order_is_still_the_open_order(app, client, db):
    alice = make_customer(db)
    plan = subscription(db, alice, status=SubscriptionStatus.ACTIVE)
    order = live_order(db, alice, at(-1), status=OrderStatus.CONFIRMED)
    order.subscription_id, order.is_subscription_order = plan.id, True
    db.session.commit()

    assert _reorder_info(app, client, alice)["open_order"]["id"] == order.id


def test_no_open_order_once_every_order_is_delivered_cancelled_or_returned(app, client, db):
    alice = make_customer(db)
    delivered_order(db, alice, at(-3))
    live_order(db, alice, at(-2), status=OrderStatus.CANCELLED)
    live_order(db, alice, at(-1), status=OrderStatus.RETURNED)

    assert _reorder_info(app, client, alice)["open_order"] is None


# Isolation: the answer is read for the JWT's own account. Bob is not linked to Alice; his orders
# are newer than hers on both questions, so a lookup that forgot whose orders it reads names his.


def test_another_customers_newer_orders_never_answer_for_her(app, client, db, shop):
    alice, bob = make_customer(db), make_customer(db)
    her_usual = add_order_lines(db, delivered_order(db, alice, at(-10)), [(shop.water, 2)])
    her_open = live_order(db, alice, at(-5), status=OrderStatus.PENDING)
    add_order_lines(db, delivered_order(db, bob, at(-2)), [(shop.cups, 6)])
    live_order(db, bob, at(-1), status=OrderStatus.CONFIRMED)

    info = _reorder_info(app, client, alice)

    assert info["candidate"]["order_id"] == her_usual.id
    assert info["candidate"]["items"] == [{"product_id": shop.water.id, "product_name": "Water 19L", "quantity": 2}]
    assert info["open_order"] == {"id": her_open.id, "order_number": her_open.order_number, "status": "pending"}


def test_with_no_orders_of_her_own_she_reads_none_of_another_customers(app, client, db, shop):
    alice, bob = make_customer(db), make_customer(db)
    add_order_lines(db, delivered_order(db, bob, at(-2)), [(shop.water, 3)])
    live_order(db, bob, at(-1), status=OrderStatus.CONFIRMED)

    assert _reorder_info(app, client, alice) == {"candidate": None, "open_order": None}
