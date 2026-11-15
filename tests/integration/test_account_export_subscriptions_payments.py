"""The account data download carries the customer's subscriptions and payments (owner 2026-11-14: "Account data
export: yes include subscriptions and payments").

Driven the way the cabinet drives it: the subscriptions are made, paused and cancelled through the storefront's own
API calls, and the file is fetched as my-account.js fetches it (POST /api/v1/auth/export-data with the login cookie and
`X-CSRF-TOKEN`, no body). Only external I/O is mocked (the guard tests' Celery publish, route notifications and Click
link). Payments are rows, as the gateways and the drivers leave them.

The customer's own rows only: another customer's subscription and payment never appear, and neither does a payment
the customer is only the collector of (`payments.collected_by`, the second foreign key to users).

The file is the document as the route writes it (review EXP-1): every instant in UTC, indented, UTF-8 text. The API's
timezone middleware presents JSON answers in the viewer's timezone but leaves a downloaded attachment alone.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription
from business_app.models.user import UserAddress
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionFrequency
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures by name)
    BASE,
    THURSDAY,
    _create,
    _external_io,
    _published,
    _state,
    clock,
    copy,
    every_day_slot,
    monday,
    web,
)
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

EXPORT_URL = "/api/v1/auth/export-data"

SUBSCRIPTION_KEYS = {
    "id",
    "subscription_number",
    "name",
    "status",
    "frequency",
    "delivery_day_of_week",
    "delivery_day_of_month",
    "delivery_time_slot",
    "delivery_address_id",
    "delivery_address_title",
    "payment_method",
    "items",
    "next_delivery_date",
    "last_delivery_date",
    "start_date",
    "paused_at",
    "end_date",
    "created_at",
}

# What the gateways and the database keep about a payment that is never the customer's download.
NEVER_EXPORTED = (
    "CLICK-TX-77001",
    "SIGN-STRING-secret",
    "8600 49** **** 1234",
    "my.click.uz/services/pay",
    "Gateway refused: SECRET",
    "provider_data",
    "idempotency_key",
    "collected_by",
)


def _download(app, user):
    """my-account.js: apiRequest('/auth/export-data', {method: 'POST'}) with no body, then response.blob()."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    browser, csrf = _logged_in_browser(app, token)
    response = browser.post(EXPORT_URL, headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf})
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    return response.get_data(as_text=True)


def _instant(text):
    """An exported instant: ISO 8601 in UTC (offset +00:00), compared as the instant it names."""
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    assert moment.utcoffset() == timedelta(0), text
    return moment


_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _every_instant(node, path="$"):
    """(path, value) for every date-time string anywhere in the document; calendar dates (YYYY-MM-DD) are not."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _every_instant(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _every_instant(value, f"{path}[{index}]")
    elif isinstance(node, str) and _INSTANT.match(node):
        yield path, node


def _assert_file_as_the_route_writes_it(text, document):
    """Review EXP-1: the attachment is the route's own `json.dumps(..., indent=2, ensure_ascii=False)` (no timezone
    rewrite, no one-line re-dump with \\u escapes) and every instant in it is UTC."""
    assert text == json.dumps(document, indent=2, ensure_ascii=False)
    instants = list(_every_instant(document))
    assert instants, "the document carries instants"
    for path, value in instants:
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0), (path, value)


def _order(db, user, number, *, status=OrderStatus.DELIVERED, total="38000.00"):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=status,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _payment(db, user, *, order=None, subscription_id=None, created_at, **fields):
    payment = Payment(
        user_id=user.id,
        order_id=order.id if order else None,
        subscription_id=subscription_id,
        created_at=created_at,
        **fields,
    )
    db.session.add(payment)
    db.session.commit()
    return payment


@pytest.fixture
def titled_address(db, user_address):
    user_address.title = "Uy"
    db.session.commit()
    return user_address


def test_the_download_carries_the_customers_subscriptions_and_payments_and_nobody_elses(
    app,
    db,
    web,
    monday,
    sample_user,
    second_sample_user,
    delivery_driver,
    titled_address,
    sample_product,
    every_day_slot,
):
    # --- Subscriptions, made the way the cabinet makes them -------------------------------------------------------
    weekly = _create(
        web,
        titled_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        name="Uyga suv",
        delivery_time_slot_id=every_day_slot.id,
    )
    paused = _create(web, titled_address.id, [{"product_id": sample_product.id, "quantity": 1}], name="Офис")
    assert web.post(f"{BASE}/{paused}/pause", {"expected_state": _state(web, paused)}).status_code == 200
    monthly = _create(
        web,
        titled_address.id,
        [{"product_id": sample_product.id, "quantity": 3}],
        name="Oylik",
        frequency="monthly",
        delivery_day_of_week=None,
        delivery_day_of_month=15,
    )
    assert web.post(f"{BASE}/{monthly}/cancel", {"expected_state": _state(web, monthly)}).status_code == 200
    # The engine's first orders go through the (mocked) worker: no payment row exists yet but the ones below.
    assert Payment.query.count() == 0

    # Another customer's subscription.
    elsewhere = UserAddress(user_id=second_sample_user.id, full_address="Toshkent, Yunusobod 4", city="Tashkent")
    db.session.add(elsewhere)
    db.session.flush()
    theirs = Subscription(
        user_id=second_sample_user.id,
        name="Begona obuna",
        frequency=SubscriptionFrequency.WEEKLY,
        delivery_day_of_week=THURSDAY,
        delivery_address_id=elsewhere.id,
        billing_amount=Decimal("15000.00"),
        payment_method=PaymentMethod.CLICK,
        start_date=datetime.now(UTC),
    )
    db.session.add(theirs)
    db.session.commit()

    # --- Payments, as the gateways and the drivers leave them -----------------------------------------------------
    t0 = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    card_order = _order(db, sample_user, "EXP_PAY_000001_26")
    card = _payment(
        db,
        sample_user,
        order=card_order,
        created_at=t0,
        amount=Decimal("38000.00"),
        amount_collected=Decimal("38000.00"),
        outstanding_amount=Decimal("0.00"),
        payment_method=PaymentMethod.CLICK,
        status=PaymentStatus.COMPLETED,
        paid_at=t0 + timedelta(minutes=2),
        provider_transaction_id="CLICK-TX-77001",
        provider_data={"sign_string": "SIGN-STRING-secret", "card_number": "8600 49** **** 1234"},
        payment_link="https://my.click.uz/services/pay?id=77001",
    )
    cod_order = _order(db, sample_user, "EXP_PAY_000002_26")
    cod = _payment(
        db,
        sample_user,
        order=cod_order,
        created_at=t0 - timedelta(days=1),
        amount=Decimal("38000.00"),
        amount_collected=Decimal("20000.00"),
        outstanding_amount=Decimal("18000.00"),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.PARTIALLY_PAID,
        collected_by=delivery_driver.id,
        last_collected_at=t0 - timedelta(hours=20),
    )
    refunded_order = _order(db, sample_user, "EXP_PAY_000003_26", status=OrderStatus.CANCELLED)
    refunded = _payment(
        db,
        sample_user,
        order=refunded_order,
        created_at=t0 - timedelta(days=2),
        amount=Decimal("38000.00"),
        amount_collected=Decimal("38000.00"),
        outstanding_amount=Decimal("0.00"),
        payment_method=PaymentMethod.PAYME,
        status=PaymentStatus.PARTIALLY_REFUNDED,
        paid_at=t0 - timedelta(days=2),
    )
    for_subscription = _payment(
        db,
        sample_user,
        subscription_id=weekly,
        created_at=t0 + timedelta(days=1),
        amount=Decimal("30000.00"),
        payment_method=PaymentMethod.CLICK,
        status=PaymentStatus.FAILED,
        failure_reason="Gateway refused: SECRET",
    )
    # Another customer's payment, and one the customer only collected for someone else.
    _payment(
        db,
        second_sample_user,
        order=_order(db, second_sample_user, "EXP_PAY_000004_26"),
        created_at=t0,
        amount=Decimal("99000.00"),
        payment_method=PaymentMethod.CLICK,
        status=PaymentStatus.COMPLETED,
        paid_at=t0,
    )
    _payment(
        db,
        second_sample_user,
        order=_order(db, second_sample_user, "EXP_PAY_000005_26"),
        created_at=t0,
        amount=Decimal("77000.00"),
        amount_collected=Decimal("77000.00"),
        outstanding_amount=Decimal("0.00"),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.COMPLETED,
        paid_at=t0,
        collected_by=sample_user.id,
    )

    text = _download(app, sample_user)
    document = json.loads(text)

    assert set(document) == {"user_profile", "orders", "addresses", "subscriptions", "payments", "export_date"}
    _assert_file_as_the_route_writes_it(text, document)
    assert '"name": "Офис"' in text  # the stored name, as UTF-8 text

    # --- subscriptions: the customer's three, newest first, nobody else's -----------------------------------------
    subscriptions = document["subscriptions"]
    assert [s["id"] for s in subscriptions] == [monthly, paused, weekly]
    assert all(set(s) == SUBSCRIPTION_KEYS for s in subscriptions)
    assert "Begona obuna" not in text and theirs.id not in {s["id"] for s in subscriptions}

    published = {sid: _published(web, sid) for sid in (weekly, paused, monthly)}
    by_id = {s["id"]: s for s in subscriptions}
    for sid, row in by_id.items():
        shown = published[sid]
        # The same answers the customer's own subscription screens publish.
        assert (row["name"], row["status"], row["frequency"], row["payment_method"]) == (
            shown["name"],
            shown["status"],
            shown["frequency"],
            shown["payment_method"],
        )
        assert (row["next_delivery_date"], row["last_delivery_date"]) == (
            shown["next_delivery_date"],
            shown["last_delivery_date"],
        )
        assert [(i["product_id"], i["product_name"], i["quantity"]) for i in row["items"]] == [
            (i["product_id"], i["product_name"], i["quantity"]) for i in shown["subscription_items"]
        ]
        assert row["subscription_number"] == db.session.get(Subscription, sid).subscription_number
        _instant(row["created_at"])
        _instant(row["start_date"])

    weekly_row = by_id[weekly]
    assert {k: weekly_row[k] for k in SUBSCRIPTION_KEYS - {"subscription_number", "start_date", "created_at"}} == {
        "id": weekly,
        "name": "Uyga suv",
        "status": "active",
        "frequency": "weekly",
        "delivery_day_of_week": THURSDAY,
        "delivery_day_of_month": None,
        "delivery_time_slot": {"id": every_day_slot.id, "start_time": "10:00", "end_time": "12:00"},
        "delivery_address_id": titled_address.id,
        "delivery_address_title": "Uy",
        "payment_method": "click",
        "items": [{"product_id": sample_product.id, "product_name": "Pure Water 19L", "quantity": 2}],
        "next_delivery_date": published[weekly]["next_delivery_date"],
        "last_delivery_date": None,
        "paused_at": None,
        "end_date": None,
    }
    assert weekly_row["next_delivery_date"] is not None

    now = datetime.now(UTC)
    paused_row = by_id[paused]
    assert paused_row["status"] == "paused" and paused_row["end_date"] is None
    assert abs(_instant(paused_row["paused_at"]) - now) < timedelta(minutes=5)
    assert paused_row["delivery_time_slot"] is None  # "Any time"

    monthly_row = by_id[monthly]
    assert (monthly_row["status"], monthly_row["frequency"]) == ("cancelled", "monthly")
    assert (monthly_row["delivery_day_of_week"], monthly_row["delivery_day_of_month"]) == (None, 15)
    assert abs(_instant(monthly_row["end_date"]) - now) < timedelta(minutes=5)

    # --- payments: the customer's four, newest first, on Payment.user_id only -------------------------------------
    exported = [
        {**p, "created_at": _instant(p["created_at"]), "paid_at": _instant(p["paid_at"])} for p in document["payments"]
    ]
    assert exported == [
        {
            "id": for_subscription.id,
            "order_number": None,
            "subscription_id": weekly,
            "amount": 30000.0,
            "amount_collected": 0.0,
            "outstanding_amount": 30000.0,
            "currency": "UZS",
            "payment_method": "click",
            "status": "failed",
            "created_at": t0 + timedelta(days=1),
            "paid_at": None,
        },
        {
            "id": card.id,
            "order_number": "EXP_PAY_000001_26",
            "subscription_id": None,
            "amount": 38000.0,
            "amount_collected": 38000.0,
            "outstanding_amount": 0.0,
            "currency": "UZS",
            "payment_method": "click",
            "status": "completed",
            "created_at": t0,
            "paid_at": t0 + timedelta(minutes=2),
        },
        {
            "id": cod.id,
            "order_number": "EXP_PAY_000002_26",
            "subscription_id": None,
            "amount": 38000.0,
            "amount_collected": 20000.0,
            "outstanding_amount": 18000.0,
            "currency": "UZS",
            "payment_method": "cash",
            "status": "partially_paid",
            "created_at": t0 - timedelta(days=1),
            "paid_at": None,
        },
        {
            "id": refunded.id,
            "order_number": "EXP_PAY_000003_26",
            "subscription_id": None,
            "amount": 38000.0,
            "amount_collected": 38000.0,
            "outstanding_amount": 0.0,
            "currency": "UZS",
            "payment_method": "payme",
            "status": "partially_refunded",
            "created_at": t0 - timedelta(days=2),
            "paid_at": t0 - timedelta(days=2),
        },
    ]
    for secret in NEVER_EXPORTED + (card.payment_id, card.idempotency_key):
        assert secret not in text, secret

    # The parts that were already there are untouched by the new ones.
    assert {o["order_number"] for o in document["orders"]} == {
        "EXP_PAY_000001_26",
        "EXP_PAY_000002_26",
        "EXP_PAY_000003_26",
    }
    assert [a["id"] for a in document["addresses"]] == [titled_address.id]


def test_a_customer_with_no_subscriptions_or_payments_gets_empty_parts(app, db, sample_user):
    text = _download(app, sample_user)
    document = json.loads(text)

    assert (document["subscriptions"], document["payments"]) == ([], [])
    _assert_file_as_the_route_writes_it(text, document)
    assert _instant(document["user_profile"]["created_at"])
