"""Refreshing stored subscription item prices when a product's price changes (plan 2026-11-16, Task 18).

The switch moves `Product.base_price`; a plan keeps the unit price it was saved at, so its stored figures go
stale. `reprice_items_for_products` brings live plans (ACTIVE, PAUSED) holding the changed products to the product's
customer price, the same three steps an item edit takes.
"""

from decimal import Decimal

from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.services.subscription_service import SubscriptionService
from shared.enums import SubscriptionStatus
from tests.integration.price_change_builders import NEW_19, OLD_19, bottles
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    make_customer,
    store_fixture,
    subscription,
)

OTHER_PRICE = Decimal("9000.00")


def _plan(db, user, b, status, *, discount=0):
    plan = subscription(db, user, status=status)
    plan.discount_percentage = discount
    plan.subscription_items.append(
        SubscriptionItem(product_id=b.p19.id, quantity=2, unit_price=OLD_19, total_price=OLD_19 * 2)
    )
    plan.subscription_items.append(
        SubscriptionItem(product_id=b.other.id, quantity=1, unit_price=OTHER_PRICE, total_price=OTHER_PRICE)
    )
    plan.billing_amount = Decimal("45000.00")
    db.session.commit()
    return plan


def _figures(db, plan_id):
    db.session.expire_all()
    plan = db.session.get(Subscription, plan_id)
    items = {item.product_id: item for item in plan.subscription_items}
    return plan, items


def test_live_plans_take_the_new_item_price_and_billing_amount(store):
    b = bottles(store)
    b.p19.base_price = NEW_19
    active = _plan(store, make_customer(store), b, SubscriptionStatus.ACTIVE)
    paused = _plan(store, make_customer(store), b, SubscriptionStatus.PAUSED)

    refreshed = SubscriptionService().reprice_items_for_products([b.p19.id])
    store.session.commit()

    assert refreshed == 2
    for plan_id in (active.id, paused.id):
        plan, items = _figures(store, plan_id)
        assert (items[b.p19.id].unit_price, items[b.p19.id].total_price) == (
            Decimal("20000.00"),
            Decimal("40000.00"),
        )
        assert items[b.other.id].unit_price == OTHER_PRICE
        assert plan.billing_amount == Decimal("49000.00")


def test_a_plan_discount_stays_in_the_billing_amount(store):
    b = bottles(store)
    b.p19.base_price = NEW_19
    plan = _plan(store, make_customer(store), b, SubscriptionStatus.ACTIVE, discount=10)

    assert SubscriptionService().reprice_items_for_products([b.p19.id]) == 1
    store.session.commit()

    assert _figures(store, plan.id)[0].billing_amount == Decimal("44100.00")


def test_cancelled_and_expired_plans_keep_their_figures(store):
    b = bottles(store)
    b.p19.base_price = NEW_19
    plans = [
        _plan(store, make_customer(store), b, SubscriptionStatus.CANCELLED),
        _plan(store, make_customer(store), b, SubscriptionStatus.EXPIRED),
    ]

    assert SubscriptionService().reprice_items_for_products([b.p19.id]) == 0
    store.session.commit()

    for plan in plans:
        stored, items = _figures(store, plan.id)
        assert items[b.p19.id].unit_price == OLD_19
        assert items[b.p19.id].total_price == Decimal("36000.00")
        assert stored.billing_amount == Decimal("45000.00")


def test_plans_without_the_product_are_untouched(store):
    b = bottles(store)
    b.p19.base_price = NEW_19
    plan = _plan(store, make_customer(store), b, SubscriptionStatus.ACTIVE)

    assert SubscriptionService().reprice_items_for_products([b.p10.id]) == 0
    assert SubscriptionService().reprice_items_for_products([]) == 0
    store.session.commit()

    stored, items = _figures(store, plan.id)
    assert items[b.p19.id].unit_price == OLD_19
    assert stored.billing_amount == Decimal("45000.00")
