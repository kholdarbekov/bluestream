"""Order creation charges the courtesy through the one pricing method (spec §5.6, §5.7, plan Task 23).

`OrderService.price_order` prices every order `create_order` writes: the bot, the website, the admin screen, the
repeat endpoint and subscription billing. The courtesy is applied after the rail price and before the subtotal and
the tier quote, and order creation claims it in the same transaction that writes the order.

Every test drives the route a real client calls with the body it sends: the bot posts
`{"items", "delivery_address_id", "payment_method": "cash", "source": "telegram"}` to `POST /api/v1/orders/`; the admin
create modal posts `admin_ui/src/pages/Orders.js`'s body to `POST /api/v1/admin/orders`; the repeat endpoint reads no
body. The world comes from `price_courtesy_builders.py`; no figure here is computed by the code under test.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.price_change import PriceCourtesy
from business_app.models.product import ProductFiscalProfile
from business_app.serializers.order_serializers import serialize_order_item
from business_app.services import order_service as order_service_module
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from business_app.services.price_change import courtesy as courtesy_module
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from shared.enums import PaymentMethod
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    NEW_PRICE,
    NEW_TOTAL,
    OLD_PRICE,
    announced_change,
    courtesy_world,
    delivered_before_notice,
    entry_of,
    link_through_service,
    person,
    switch,
    water,
)
from tests.integration.reorder_reminder_builders import customer_headers, live_order, make_product
from tests.integration.test_subscription_min_order_quantity import _create_payload
from tests.integration.tier_discount_factory import seed_program, seed_tier

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/orders/"
ESTIMATE = "/api/v1/orders/cart/estimate"
# The Asl belgisi pre-utilisation a Click checkout calls (external I/O), patched as
# tests/integration/test_click_payment_deadline.py patches it.
PRE_UTILISE = (
    "business_app.services.payment_fiscalization_service.PaymentFiscalizationService"
    ".pre_utilise_marking_codes_for_payment"
)

# (unit_price, courtesy_quantity, courtesy_unit_price, discount_amount, total_price) of a stored basket line.
COURTESY_LINE = (NEW_PRICE, 2, OLD_PRICE, Decimal("4000.00"), COURTESY_TOTAL)
NEW_PRICE_LINE = (NEW_PRICE, None, None, Decimal("0.00"), NEW_TOTAL)
SPLIT_KEYS = (
    "unit_price",
    "courtesy_quantity",
    "courtesy_unit_price",
    "regular_quantity",
    "discount_amount",
    "total_price",
)


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def w(app, db, sample_product, admin_user):
    return courtesy_world(app, db, water(db, sample_product), admin_user)


def _bot_body(customer, product, quantity=BASKET, payment_method="cash"):
    """The body the customer bot posts to `POST /api/v1/orders/`."""
    return {
        "items": [{"product_id": product.id, "quantity": quantity}],
        "delivery_address_id": customer.address.id,
        "payment_method": payment_method,
        "source": "telegram",
    }


def _place(client, customer, product, quantity=BASKET, payment_method="cash"):
    response = client.post(
        ORDERS, json=_bot_body(customer, product, quantity, payment_method), headers=customer.headers
    )
    assert response.status_code == 201, response.get_json()
    return response


def _order(db, response) -> Order:
    db.session.expire_all()
    return db.session.get(Order, response.get_json()["data"]["order"]["id"])


def _only_line(order: Order) -> OrderItem:
    (line,) = order.order_items
    return line


def _money(line: OrderItem):
    return (line.unit_price, line.courtesy_quantity, line.courtesy_unit_price, line.discount_amount, line.total_price)


def _estimate(client, customer, product):
    """The bot's confirm screen asks for this before it posts the order."""
    return client.post(
        ESTIMATE,
        json={"items": [{"product_id": product.id, "quantity": BASKET}], "payment_method": "cash"},
        headers=customer.headers,
    )


@pytest.mark.parametrize(
    ("cap", "quantity", "courtesy", "total"),
    [(2, 2, 2, "36000.00"), (2, 4, 2, "76000.00"), (3, 2, 2, "36000.00")],
)
def test_an_eligible_customer_pays_the_old_price_up_to_the_cap(
    client, app, db, sample_product, admin_user, cap, quantity, courtesy, total
):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, cap=cap)

    response = _place(client, w.customer, w.product, quantity)

    order = _order(db, response)
    line = _only_line(order)
    assert (line.unit_price, line.courtesy_quantity, line.courtesy_unit_price) == (NEW_PRICE, courtesy, OLD_PRICE)
    assert line.discount_amount == Decimal("2000.00") * courtesy
    assert line.total_price == order.subtotal == order.total_amount == Decimal(total)
    assert entry_of(db, w.customer.user).order_id == order.id
    assert response.get_json()["data"]["order"]["order_items"][0]["courtesy_quantity"] == courtesy


def test_the_courtesy_is_used_once_and_the_next_order_pays_the_new_price(client, db, w):
    first = _order(db, _place(client, w.customer, w.product))
    second_response = _place(client, w.customer, w.product)
    second = _order(db, second_response)

    assert _money(_only_line(first)) == COURTESY_LINE
    assert _money(_only_line(second)) == NEW_PRICE_LINE
    assert second.total_amount == NEW_TOTAL
    assert entry_of(db, w.customer.user).order_id == first.id
    published = second_response.get_json()["data"]["order"]["order_items"][0]
    assert {key: published.get(key) for key in SPLIT_KEYS} == {
        "unit_price": 20000.0,
        "courtesy_quantity": None,
        "courtesy_unit_price": None,
        "regular_quantity": None,
        "discount_amount": 0.0,
        "total_price": 80000.0,
    }
    estimate = _estimate(client, w.customer, w.product)
    assert estimate.status_code == 200
    assert estimate.get_json()["data"]["price_courtesy"] is None


def test_linked_phones_share_one_courtesy(client, app, db, w):
    second_phone = person(app, db, "+998901515003")
    link_through_service(db, w.customer.user, second_phone.user, w.admin)

    by_second_phone = _order(db, _place(client, second_phone, w.product))
    by_first_phone = _order(db, _place(client, w.customer, w.product))

    assert (by_second_phone.total_amount, by_first_phone.total_amount) == (COURTESY_TOTAL, NEW_TOTAL)
    assert entry_of(db, w.customer.user).order_id == by_second_phone.id


def test_two_lines_of_one_product_share_the_cap(client, app, db, sample_product, admin_user, admin_claim_headers):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, cap=3)

    # The admin create modal's body (admin_ui/src/pages/Orders.js), with the product picked twice.
    response = client.post(
        "/api/v1/admin/orders",
        json={
            "user_id": w.customer.user.id,
            "delivery_address_id": w.customer.address.id,
            "payment_method": "cash",
            "delivery_notes": "",
            "consume_marking_codes": False,
            "items": [{"product_id": w.product.id, "quantity": 2}, {"product_id": w.product.id, "quantity": 2}],
            "delivery_date": None,
            "delivery_window_start": None,
            "delivery_window_end": None,
        },
        headers=admin_claim_headers,
    )

    assert response.status_code == 201, response.get_json()
    order = _order(db, response)
    lines = sorted(order.order_items, key=lambda line: line.id)
    assert [(line.courtesy_quantity, line.discount_amount, line.total_price) for line in lines] == [
        (2, Decimal("4000.00"), Decimal("36000.00")),
        (1, Decimal("2000.00"), Decimal("38000.00")),
    ]
    assert order.subtotal == Decimal("74000.00")


def test_a_subscription_order_never_takes_the_courtesy(client, app, db, w):
    # Celery publish and the route's customer notification are external I/O (the seams
    # tests/integration/test_subscription_min_order_quantity.py patches), held to their real signatures.
    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay", autospec=True), patch(
        "business_app.api.subscriptions.get_notification_service", autospec=True
    ):
        created = client.post(
            "/api/v1/subscriptions/",
            json=_create_payload(w.product.id, BASKET, w.customer.address.id),
            headers=w.customer.headers,
        )
        assert created.status_code == 201, created.get_json()
        subscription_id = created.get_json()["data"]["subscription"]["id"]
        with app.app_context():
            result = SubscriptionService().create_next_order(subscription_id, due_only=False)

    assert result["success"] is True, result
    db.session.expire_all()
    order = Order.query.filter_by(subscription_id=subscription_id).one()
    assert _money(_only_line(order)) == NEW_PRICE_LINE
    assert order.total_amount == NEW_TOTAL
    assert entry_of(db, w.customer.user).order_id is None


def test_an_account_with_an_active_contract_gets_no_courtesy(
    client, app, db, sample_product, sample_category, admin_user
):
    product = water(db, sample_product)
    account, office = make_workplace(db)
    delivered_before_notice(db, account, office, product)
    announced_change(db, product, admin_user)
    switch(db)
    assert entry_of(db, account) is not None  # no contract existed when the list was built
    # The contract is signed after the switch and covers another product, so the 18.9 L line is not contract-priced:
    # only the account's contract refuses the courtesy, at use time.
    make_contract(db, account, make_product(db, sample_category, "Cooler 5 L"))
    buyer = SimpleNamespace(user=account, address=office, headers=customer_headers(app, account))

    order = _order(db, _place(client, buyer, product))

    assert _money(_only_line(order)) == NEW_PRICE_LINE
    assert order.total_amount == NEW_TOTAL
    assert entry_of(db, account).order_id is None


def test_the_tier_discount_is_quoted_on_the_net_subtotal(client, db, w):
    seed_tier(db, seed_program(db), name="Base", rate=Decimal("5"))

    order = _order(db, _place(client, w.customer, w.product))

    assert (order.subtotal, order.tier_discount, order.total_amount) == (
        COURTESY_TOTAL,
        Decimal("3800.00"),
        Decimal("72200.00"),
    )


def test_a_customer_who_ordered_during_the_notice_period_pays_the_new_price(
    client, app, db, sample_product, admin_user
):
    w = courtesy_world(app, db, water(db, sample_product), admin_user, eligible=False)

    order = _order(db, _place(client, w.customer, w.product))

    assert _money(_only_line(order)) == NEW_PRICE_LINE
    assert order.total_amount == NEW_TOTAL
    assert entry_of(db, w.customer.user) is None


def test_an_order_after_the_window_pays_the_new_price(client, db, w, monkeypatch):
    after_the_window = ensure_utc(w.change.courtesy_ends_at) + timedelta(seconds=1)
    monkeypatch.setattr(order_service_module, "get_utc_now", lambda: after_the_window)

    order = _order(db, _place(client, w.customer, w.product))

    assert _money(_only_line(order)) == NEW_PRICE_LINE
    assert order.total_amount == NEW_TOTAL
    assert entry_of(db, w.customer.user).order_id is None


def test_a_claim_that_finds_the_courtesy_used_charges_the_new_price(client, db, w, monkeypatch):
    """The confirm screen's estimate found the courtesy, then another order of the person claimed it while this one
    was being priced (controller ruling on Task 15). The claim must run on lines no preview lowered: had it been handed
    lines a preview already lowered, they would keep the old-price split and this order would charge it."""
    assert _estimate(client, w.customer, w.product).get_json()["data"]["price_courtesy"]["applied"] is True
    rival_id = live_order(db, w.customer.user, get_utc_now(), address=w.customer.address).id
    entry_id = entry_of(db, w.customer.user).id
    real_apply_courtesy = courtesy_module.apply_courtesy
    lines_the_claim_saw = []

    def a_rival_order_claims_first(lines, *, user_id, at, is_subscription_order, claim):
        if claim:
            lines_the_claim_saw.extend((line["unit_price"], line.get("courtesy_quantity")) for line in lines)
            # The rival's transaction commits its claim just before this one takes the lock.
            courtesy_module.mark_used(db.session.get(PriceCourtesy, entry_id), db.session.get(Order, rival_id))
        return real_apply_courtesy(
            lines, user_id=user_id, at=at, is_subscription_order=is_subscription_order, claim=claim
        )

    monkeypatch.setattr(courtesy_module, "apply_courtesy", a_rival_order_claims_first)

    order = _order(db, _place(client, w.customer, w.product))

    assert lines_the_claim_saw == [(NEW_PRICE, None)]
    assert _money(_only_line(order)) == NEW_PRICE_LINE
    assert order.total_amount == NEW_TOTAL
    assert entry_of(db, w.customer.user).order_id == rival_id


def test_order_again_after_the_switch_is_priced_by_the_courtesy_rule(client, app, db, sample_product, admin_user):
    """Review Focus 1: the repeat endpoint clones a 4 x 18,000 order. It must price the clone like any new order,
    never copy the old unit price onto every unit."""
    product = water(db, sample_product)
    customer = person(app, db, "+998901515004")
    old_price_order = delivered_before_notice(db, customer.user, customer.address, product=product)
    old_price_order.payment_method = PaymentMethod.CASH  # a real delivered order names the method the repeat reuses
    db.session.commit()
    announced_change(db, product, admin_user)
    switch(db)

    first = client.post(f"/api/v1/orders/repeat/{old_price_order.id}", headers=customer.headers)

    assert first.status_code == 201, first.get_json()
    repeat = _order(db, first)
    line = _only_line(repeat)
    assert (line.unit_price, line.courtesy_quantity, line.courtesy_unit_price, line.total_price) == (
        NEW_PRICE,
        2,
        OLD_PRICE,
        COURTESY_TOTAL,
    )
    assert entry_of(db, customer.user).order_id == repeat.id

    second = client.post(f"/api/v1/orders/repeat/{old_price_order.id}", headers=customer.headers)

    assert second.status_code == 201, second.get_json()
    second_repeat = _order(db, second)
    assert (_only_line(second_repeat).courtesy_quantity, second_repeat.total_amount) == (None, NEW_TOTAL)


def test_a_placed_courtesy_order_reads_back_its_split(client, w):
    """Review Focus 2: on a courtesy line unit_price x quantity is not the total; the order API publishes the split."""
    order_id = _place(client, w.customer, w.product).get_json()["data"]["order"]["id"]

    response = client.get(f"/api/v1/orders/{order_id}", headers=w.customer.headers)

    assert response.status_code == 200
    order = response.get_json()["data"]["order"]
    (line,) = order["order_items"]
    assert {key: line.get(key) for key in SPLIT_KEYS} == {
        "unit_price": 20000.0,
        "courtesy_quantity": 2,
        "courtesy_unit_price": 18000.0,
        "regular_quantity": 2,
        "discount_amount": 4000.0,
        "total_price": 76000.0,
    }
    assert order["total_amount"] == 76000.0


def test_a_line_whose_product_is_gone_still_publishes_its_split(db, w):
    """`serialize_order_item`'s fallback branch (the product row cannot be read) publishes the same split."""
    line = OrderItem(
        product_id=10**9,
        quantity=BASKET,
        unit_price=NEW_PRICE,
        discount_amount=Decimal("4000.00"),
        total_price=COURTESY_TOTAL,
        courtesy_quantity=2,
        courtesy_unit_price=OLD_PRICE,
    )

    published = serialize_order_item(line)

    assert "product_name" not in published  # the fallback branch
    assert {key: published.get(key) for key in SPLIT_KEYS} == {
        "unit_price": 20000.0,
        "courtesy_quantity": 2,
        "courtesy_unit_price": 18000.0,
        "regular_quantity": 2,
        "discount_amount": 4000.0,
        "total_price": 76000.0,
    }


# A stored courtesy line `line_money` refuses: its old price (18,000) is above its unit price (15,000). It is the row
# `reprice_order_for_rail` used to leave on a courtesy line it moved onto a contract price (final review CROSS-2).
STALE_SPLIT = {
    "unit_price": 15000.0,
    "courtesy_quantity": 2,
    "courtesy_unit_price": 18000.0,
    "regular_quantity": 2,
    "discount_amount": 4000.0,
    "total_price": 60000.0,
}


def test_every_read_publishes_a_stored_line_as_it_is_stored(client, db, w, admin_auth_headers):
    """(Ledger T23, final review ORDERS-5 / CROSS-2) One publisher of a stored line's split reads its stored columns and
    never recomputes them, so the customer's order read and the admin edit preview's `items_before` publish the same
    figures, even for a row `line_money` would refuse. A stored row is a fact; a read that raised answered 500."""
    order = _order(db, _place(client, w.customer, w.product))
    line = _only_line(order)
    # A declared test write: the stale row (see STALE_SPLIT).
    line.unit_price, line.total_price = Decimal("15000.00"), Decimal("60000.00")
    db.session.commit()
    other = make_product(db, w.product.category, "Water 5 L")
    # The edit modal's swap: the stale line removed (quantity 0), another bottle added. Only `items_before` reads it.
    swap = {
        "items": [
            {"orderItemId": line.id, "productId": w.product.id, "quantity": 0},
            {"productId": other.id, "quantity": 2},
        ],
        "reason": "swap the bottle",
    }

    read = client.get(f"/api/v1/orders/{order.id}", headers=w.customer.headers)
    preview = client.post(f"/api/v1/admin/orders/{order.id}/edit-preview", json=swap, headers=admin_auth_headers)

    assert read.status_code == 200, read.get_data(as_text=True)
    (published,) = read.get_json()["data"]["order"]["order_items"]
    assert {key: published.get(key) for key in SPLIT_KEYS} == STALE_SPLIT
    assert preview.status_code == 200, preview.get_data(as_text=True)
    (before,) = preview.get_json()["data"]["items_before"]
    assert {key: before.get(key) for key in SPLIT_KEYS} == STALE_SPLIT


def test_the_click_receipt_charges_the_courtesy_total(client, app, db, w, monkeypatch):
    """Review Focus 2: the Click receipt line is the line's charged total, never unit_price x quantity."""
    db.session.add(
        ProductFiscalProfile(
            product_id=w.product.id,
            spic="SPIC-19L",
            package_code="PACK-19L",
            units="1213733",
            vat_percent=Decimal("12.00"),
            fiscalization_enabled=True,
            requires_marking_codes=False,
        )
    )
    db.session.commit()
    monkeypatch.setitem(app.config, "COMPANY_TIN", "306522134")
    monkeypatch.setitem(app.config, "CLICK_SHOP_SERVICE_ID", "98060")
    with patch(PRE_UTILISE, autospec=True, return_value={"utilised_at": None, "fast_path": True}):
        order = _order(db, _place(client, w.customer, w.product, payment_method="click"))
    payment = Payment.query.filter_by(order_id=order.id).one()
    # The identifiers Click's PREPARE call stamps on the payment before the receipt is built.
    payment.provider_data = {"click": {"click_trans_id": "950101", "click_paydoc_id": "5231199101"}}
    db.session.commit()

    payload = PaymentFiscalizationService().build_click_fiscalization_payload(payment)

    (receipt_line,) = payload["items"]
    assert receipt_line["Price"] == 7_600_000
    assert sum(item["Price"] - item.get("Discount", 0) for item in payload["items"]) == 7_600_000
    assert order.total_amount == COURTESY_TOTAL
    assert payload["received_card"] == 7_600_000
