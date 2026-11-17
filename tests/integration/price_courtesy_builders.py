"""The courtesy world every channel test shares (plan 2026-11-16, Part C, Task 22 onwards).

One world: old price 18,000, new 20,000, cap 2, N = 14, a basket of 4. With the courtesy
`2 x 18,000 + 2 x 20,000 = 76,000`; without it 80,000; before the switch 72,000. Delivery is free in tests.

Built on `price_change_builders.py` (the price-change rows) and `reorder_reminder_builders.py` (people and orders).
No builder computes a date or decides eligibility: the eligible person is the one the real `build_courtesy_list`
lists once `switch` has run the real `apply_due_price_changes`, and tests compare a date with
`w.change.courtesy_last_day.isoformat()`.
"""

from datetime import timedelta
from functools import partial
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable

from business_app.models.order import OrderItem
from business_app.models.price_change import PriceCourtesy
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.price_change.switch import apply_due_price_changes
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import get_utc_now
from tests.integration.price_change_builders import make_admin, make_price_change, priced_line  # noqa: F401
from tests.integration.reorder_reminder_builders import (
    customer_headers,
    delivered_order,
    home_address,
    live_order,
    make_customer,
)

OLD_PRICE = Decimal("18000.00")
NEW_PRICE = Decimal("20000.00")
CAP = 2
COURTESY_DAYS = 14
BASKET = 4
COURTESY_TOTAL = Decimal("76000.00")
NEW_TOTAL = Decimal("80000.00")
OLD_TOTAL = Decimal("72000.00")


def water(db, product):
    """`product` as the world's 18.9 L bottle: at the old price, no discount price, minimum order 2, stock 100."""
    product.base_price = OLD_PRICE
    product.discount_price = None
    product.min_order_quantity = 2
    product.stock_quantity = 100
    db.session.commit()
    return product


def person(app, db, phone: str) -> SimpleNamespace:
    """A verified customer with a home address and the JWT the clients send. `POST /api/v1/orders/` is
    `@require_verification("phone")`, so the phone is verified."""
    user = make_customer(db, phone=phone)
    user.phone_verified_at = get_utc_now()
    db.session.commit()
    return SimpleNamespace(user=user, address=home_address(db, user), headers=customer_headers(app, user))


def delivered_before_notice(db, user, address, product=None):
    """A delivered order from 40 days ago, before the announcement. With `product`, it carries the basket at the old
    price (one line, `OLD_TOTAL`)."""
    order = delivered_order(db, user, get_utc_now() - timedelta(days=40), address=address)
    if product is not None:
        db.session.add(OrderItem.from_priced_line(order.id, priced_line(product, BASKET, OLD_PRICE)))
        order.subtotal = order.total_amount = OLD_TOTAL
        db.session.commit()
    return order


def ordered_in_notice(db, user, address):
    """A live order placed 10 days ago, inside the notice period: it makes the person ineligible."""
    return live_order(db, user, get_utc_now() - timedelta(days=10), address=address)


def announced_change(db, product, actor, *, new_price=NEW_PRICE, cap=CAP, courtesy_days=COURTESY_DAYS, extra_items=()):
    """A change announced 30 days ago and due today: not applied yet, so `switch` is what applies it."""
    change = make_price_change(
        db,
        [(product, new_price, cap, None), *extra_items],
        created_by=actor,
        announce_on=local_date() - timedelta(days=30),
        change_on=local_date(),
        courtesy_days=courtesy_days,
        announcement_started_at=get_utc_now() - timedelta(days=30),
    )
    db.session.commit()
    return change


def switch(db):
    """The switch runs, as the beat task runs it."""
    apply_due_price_changes()
    db.session.expire_all()


def entry_of(db, user):
    """The person's courtesy entry (the one anchored on `user`), or None."""
    return PriceCourtesy.query.filter_by(user_id=user.id).order_by(PriceCourtesy.id).first()


def link_through_service(db, primary, secondary, admin):
    """Link two accounts as one person the way the admin does."""
    return CustomerLinkService().link_accounts(primary.id, secondary.id, admin.id, "same person")


def place_by_bot(app, w):
    """The default first order of a `used` world: the bot's `POST /api/v1/orders/` with the basket of 4, cash."""
    response = app.test_client().post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": w.product.id, "quantity": BASKET}],
            "delivery_address_id": w.customer.address.id,
            "payment_method": "cash",
            "source": "telegram",
        },
        headers=w.customer.headers,
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    return response


def courtesy_world(
    app,
    db,
    product,
    admin,
    *,
    phone="+998901515001",
    switched=True,
    eligible=True,
    cap=CAP,
    extra_items=(),
    used=False,
    place: Callable[[SimpleNamespace], Any] | None = None,
    new=False,
    customer: SimpleNamespace | None = None,
) -> SimpleNamespace:
    """The change announced, a customer with a delivered order before it (and, when not `eligible`, one placed inside
    the notice period too), and the switch run unless `switched` is False.

    `new` leaves out the delivered order (a customer with no history). `customer` (`user`, `address`, `headers`) is
    the person the history and the orders land on; the default is `person(app, db, phone)`. `used` places the
    customer's first order after the switch through `place(w)` (default: the bot route), which takes the courtesy."""
    customer = customer or person(app, db, phone)
    if not new:
        delivered_before_notice(db, customer.user, customer.address, product)
    if not eligible:
        ordered_in_notice(db, customer.user, customer.address)
    change = announced_change(db, product, admin, cap=cap, extra_items=extra_items)
    if switched:
        switch(db)
    w = SimpleNamespace(product=product, change=change, customer=customer, admin=admin)
    if used:
        (place or partial(place_by_bot, app))(w)
        db.session.expire_all()
    return w
