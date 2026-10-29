"""One customer can never read or change another customer's subscription through the /my-subscriptions
endpoints (spec 2026-10-26 §10 "order-history scoping tests"; final review Minor 6).

Every new customer endpoint resolves the subscription through `_get_user_subscription_or_raise(id, user_id)`.
These tests prove it on the wire: customer B, signed in exactly as the website signs in (cookie JWT plus
`X-CSRF-TOKEN`, base.js `apiRequest`), sends the very requests the page sends for customer A's subscription.
Each answers 404, the same answer as for a subscription that does not exist, and nothing is written.
"""

from datetime import timedelta

import pytest

from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.tasks.subscription_tasks import process_daily_subscription_billing
from shared.enums import OrderStatus
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures)
    BASE,
    _create,
    _external_io,
    _Web,
    clock,
    copy,
    cups,
    every_day_slot,
    monday,
)
from tests.unit._scope_money_helpers import make_address, make_user

pytestmark = pytest.mark.integration


@pytest.fixture
def owner_subscription(db, client, app, sample_user, user_address, sample_product, cups, every_day_slot, clock, monday):
    """Customer A's weekly-Thursday Click subscription, whose Thursday order the Wednesday run has placed."""
    web = _Web(app, client, sample_user)
    sub_id = _create(
        web,
        user_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        delivery_time_slot_id=every_day_slot.id,
    )
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()
    published = web.get(f"{BASE}/{sub_id}").get_json()["data"]["subscription"]
    return sub_id, published


@pytest.fixture
def stranger(app, db):
    """Customer B, on a browser of their own."""
    user = make_user(db)
    make_address(db, user)
    return _Web(app, app.test_client(), user)


def _snapshot(db, sub_id):
    """Everything these requests could change on A's subscription."""
    db.session.expire_all()
    sub = db.session.get(Subscription, sub_id)
    items = sorted(
        (item.product_id, item.quantity)
        for item in SubscriptionItem.query.filter_by(subscription_id=sub_id).all()
    )
    orders = sorted((order.id, order.status) for order in Order.query.filter_by(subscription_id=sub_id).all())
    logs = SubscriptionLog.query.filter_by(subscription_id=sub_id).count()
    return sub.next_delivery_date, sub.frequency, items, orders, logs


def test_another_customers_history_is_a_404(db, stranger, owner_subscription):
    sub_id, _ = owner_subscription

    resp = stranger.get(f"{BASE}/{sub_id}/history?page=1&per_page=5")

    assert resp.status_code == 404, resp.get_json()
    assert "items" not in (resp.get_json().get("data") or {})


def test_another_customers_preview_is_a_404(db, stranger, owner_subscription, cups):
    sub_id, _ = owner_subscription
    before = _snapshot(db, sub_id)

    schedule = stranger.post(f"{BASE}/{sub_id}/preview", {"frequency": "monthly", "delivery_day_of_month": 15})
    items = stranger.post(f"{BASE}/{sub_id}/preview", {"items": [{"product_id": cups.id, "quantity": 1}]})

    assert (schedule.status_code, items.status_code) == (404, 404), (schedule.get_json(), items.get_json())
    assert _snapshot(db, sub_id) == before


def test_another_customers_items_cannot_be_replaced(db, stranger, owner_subscription, cups):
    """With the very state A's page was drawn from (spec 2026-10-27 §16 M2): ownership answers first."""
    sub_id, published = owner_subscription
    before = _snapshot(db, sub_id)

    shown = published["allowed_actions"]["expected_state"]
    body = {"items": [{"product_id": cups.id, "quantity": 5}], "expected_state": shown}
    resp = stranger.put(f"{BASE}/{sub_id}/items", body)

    assert resp.status_code == 404, resp.get_json()
    assert _snapshot(db, sub_id) == before


def test_another_customers_delivery_cannot_be_skipped(db, stranger, owner_subscription):
    """With the very date A's page names: B learns nothing and A's order and clock stay as they were."""
    sub_id, published = owner_subscription
    named = published["allowed_actions"]["skip"]["date"]
    before = _snapshot(db, sub_id)
    assert [status for _, status in before[3]] == [OrderStatus.PENDING]

    resp = stranger.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": named, "reason_code": "away"})

    assert resp.status_code == 404, resp.get_json()
    assert _snapshot(db, sub_id) == before
