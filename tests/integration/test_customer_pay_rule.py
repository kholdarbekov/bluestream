"""The one Pay rule, published as `customer_actions.pay` (plan 2026-10-27 Task 1; spec 2026-10-27 §4.1, §4.2, §4.4).

`payment_projection.pay_refusal` decides whether a customer may pay an order online, and why not. Every customer
order payload (the list, the detail and POST /orders) publishes its answer as `customer_actions.pay` with the
finished words, and the subscription page's `allowed_actions.pay`, `upcoming_delivery.can_pay` and history rows read
the same function. So the bot and the website only render it, and the write guard cannot disagree.

Driven through the endpoints the clients call, over the one shared state matrix
(`tests/integration/customer_action_matrix.py`). Only external I/O is mocked: the Asl belgisi pre-utilisation.
"""

from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.translation import Translation
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils import payment_projection
from business_app.utils.payment_projection import pay_refusal
from business_app.utils.timezone_utils import get_utc_now
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import PaymentStatus, SubscriptionStatus
from tests.integration.customer_action_matrix import ROW, ROWS, seed
from tests.integration.tier_discount_factory import post_order, verify_phone

pytestmark = pytest.mark.integration

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
NBSP = "\u00a0"
SEEDED_PREFIXES = ("api.orders.actions.", "api.payments.error.", "api.subscriptions.page.", "web.display_labels.")
ORDERS = "/api/v1/orders"
SUBSCRIPTIONS = "/api/v1/subscriptions"
MONDAY = date(2027, 1, 4)
TUESDAY = date(2027, 1, 5)
PAY_CLOSED = {
    "allowed": False,
    "refusal": "deadline_passed",
    "label": None,
    "expected_amount": None,
    "amount_label": None,
    "deadline_label": None,
}


@pytest.fixture(autouse=True)
def seeded(db):
    """The Pay words, the payment refusals, the subscription page and the display labels, as the seeder ships them."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if not key.startswith(SEEDED_PREFIXES):
            continue
        for language, value in values.items():
            if value:
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


def local(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=TASHKENT).astimezone(timezone.utc)


def _at(monkeypatch, instant: datetime) -> None:
    """The clock Pay reads: the published answer's and the write guard's."""
    monkeypatch.setattr("business_app.services.customer_order_actions.get_utc_now", lambda: instant)
    monkeypatch.setattr(payment_projection, "get_utc_now", lambda: instant)


class _Customer:
    """The customer clients' bearer token (`create_access_token(identity=str(user.id))`), a fresh client per
    call, in English."""

    def __init__(self, app, user):
        with app.app_context():
            token = create_access_token(identity=str(user.id))
        self._app = app
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def get(self, path):
        resp = self._app.test_client().get(f"{path}{'&' if '?' in path else '?'}lang=en", headers=self.headers)
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()

    def order_pay(self, order_id):
        return self.get(f"{ORDERS}/{order_id}")["data"]["order"]["customer_actions"]["pay"]

    def listed(self, order_id):
        return next(o for o in self.get(f"{ORDERS}/?per_page=100")["data"]["orders"] if o["id"] == order_id)

    def subscription(self, sub_id):
        published = self.get(f"{SUBSCRIPTIONS}/{sub_id}")["data"]["subscription"]
        assert "error" not in published, published  # the serializer's silent partial fallback
        return published

    def history_row(self, sub_id, order_id):
        rows = self.get(f"{SUBSCRIPTIONS}/{sub_id}/history")["data"]["items"]
        return next(row for row in rows if row["order_id"] == order_id)


@pytest.fixture
def customer(app, sample_user):
    return _Customer(app, sample_user)


def _fresh(db, order_id) -> Order:
    db.session.expire_all()
    return db.session.get(Order, order_id)


# --------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_every_order_payload_publishes_the_one_pay_answer(app, db, sample_user, customer, row):
    order = seed(db, sample_user, row, now=get_utc_now())

    detail = customer.order_pay(order.id)
    listed = customer.listed(order.id)

    allowed = row.pay_refusal is None
    assert listed["customer_actions"]["pay"] == detail
    assert (detail["allowed"], detail["refusal"]) == (allowed, row.pay_refusal)
    assert detail["label"] == (f"Pay 30{NBSP}000{NBSP}so'm" if allowed else None)
    assert detail["expected_amount"] == ("30000.00" if allowed else None)
    assert detail["amount_label"] == (f"30{NBSP}000{NBSP}so'm" if allowed else None)
    assert (detail["deadline_label"] is not None) is (allowed and row.has_deadline)
    # The write guard's own function agrees.
    assert pay_refusal(_fresh(db, order.id)) == row.pay_refusal


# --------------------------------------------------------------------------- the deadline


def test_pay_disappears_exactly_at_the_deadline(monkeypatch, db, sample_user, customer):
    # Payable from local Monday 09:00, so the deadline is Tuesday 09:00.
    order = seed(db, sample_user, ROW["click_pending"], now=local(MONDAY, 10))

    _at(monkeypatch, local(MONDAY, 10))
    assert customer.order_pay(order.id)["deadline_label"] == "Pay by 09:00 tomorrow."

    _at(monkeypatch, local(TUESDAY, 8, 59, 59))
    assert customer.order_pay(order.id)["allowed"] is True
    assert pay_refusal(_fresh(db, order.id)) is None

    _at(monkeypatch, local(TUESDAY, 9))
    assert customer.order_pay(order.id) == PAY_CLOSED
    assert pay_refusal(_fresh(db, order.id)) == "deadline_passed"


def test_the_pay_by_label_counts_local_days_after_local_midnight(monkeypatch, db, sample_user, customer):
    """Review Focus 2: payable from local Monday 02:00 (Sunday 21:00 UTC), read at local Monday 02:30 (Sunday
    21:30 UTC). A UTC "today" would print "02:00 on Tuesday"."""
    order = seed(db, sample_user, ROW["click_pending"], now=local(MONDAY, 3))

    _at(monkeypatch, local(MONDAY, 2, 30))
    assert customer.order_pay(order.id)["deadline_label"] == "Pay by 02:00 tomorrow."

    _at(monkeypatch, local(TUESDAY, 2))
    assert customer.order_pay(order.id)["refusal"] == "deadline_passed"


# --------------------------------------------------------------------------- checkout


def test_a_click_checkout_answers_pay_and_a_cash_checkout_does_not(
    app, db, sample_user, sample_product, user_address, customer
):
    verify_phone(db, sample_user)

    def checkout(method):
        return post_order(
            app, customer.headers, product_id=sample_product.id, address_id=user_address.id, payment_method=method
        )

    with patch(
        "business_app.services.payment_fiscalization_service.PaymentFiscalizationService"
        ".pre_utilise_marking_codes_for_payment",
        return_value={"utilised_at": None, "fast_path": True},
    ):
        click, cash = checkout("click"), checkout("cash")

    assert (click.status_code, cash.status_code) == (201, 201), (click.get_json(), cash.get_json())
    assert click.get_json()["data"]["order"]["customer_actions"]["pay"]["allowed"] is True
    assert cash.get_json()["data"]["order"]["customer_actions"]["pay"]["refusal"] == "not_online"


# --------------------------------------------------------------------------- the subscription page


def _assert_subscription_reads_the_rule(db, customer, order_id, expected):
    order = _fresh(db, order_id)
    detail = customer.subscription(order.subscription_id)
    assert detail["allowed_actions"]["pay"]["allowed"] is detail["upcoming_delivery"]["can_pay"] is expected
    if expected:
        assert detail["allowed_actions"]["pay"]["expected_amount"] == "30000.00"
    history_row = customer.history_row(order.subscription_id, order_id)
    assert history_row["customer_actions"]["pay"] == customer.order_pay(order_id)
    assert history_row["can_pay"] is history_row["customer_actions"]["pay"]["allowed"] is expected


def test_the_subscription_pay_fields_read_the_same_rule(app, db, sample_user, customer):
    order = seed(db, sample_user, ROW["sub_active_click"], now=get_utc_now())
    _assert_subscription_reads_the_rule(db, customer, order.id, True)

    # The same order, partly paid: the driver collects the rest at the door.
    partly = ROW["click_partly_paid"]
    payment = _fresh(db, order.id).payment
    payment.status, payment.amount_collected = partly.payment, partly.collected
    payment.outstanding_amount = payment.amount - partly.collected
    db.session.commit()
    _assert_subscription_reads_the_rule(db, customer, order.id, False)

    late = seed(db, sample_user, ROW["sub_click_past_deadline"], now=get_utc_now())
    _assert_subscription_reads_the_rule(db, customer, late.id, False)


def test_an_order_with_the_driver_has_no_deadline_note(db, sample_user, customer):
    row = replace(ROW["out_for_delivery_past_deadline"], subscription=SubscriptionStatus.ACTIVE)
    order = seed(db, sample_user, row, now=get_utc_now())

    upcoming = customer.subscription(order.subscription_id)["upcoming_delivery"]

    assert upcoming["order_id"] == order.id
    assert upcoming["can_pay"] is True and upcoming["pay_deadline_label"] is None
    assert upcoming["payment_note"] == "Pay online whenever you're ready."


def test_a_partly_paid_order_shows_pay_at_the_door(db, sample_user, customer):
    row = replace(ROW["click_partly_paid"], subscription=SubscriptionStatus.ACTIVE)
    order = seed(db, sample_user, row, now=get_utc_now())

    upcoming = customer.subscription(order.subscription_id)["upcoming_delivery"]

    assert upcoming["order_id"] == order.id
    assert upcoming["can_pay"] is False
    assert upcoming["payment_status"]["code"] == "pay_on_delivery"
    assert upcoming["payment_note"] == "Pay the driver at the door."


def test_a_failed_attempt_without_a_deadline_says_it_can_be_tried_again(db, sample_user, customer):
    row = replace(
        ROW["out_for_delivery_past_deadline"], payment=PaymentStatus.FAILED, subscription=SubscriptionStatus.ACTIVE
    )
    order = seed(db, sample_user, row, now=get_utc_now())

    upcoming = customer.subscription(order.subscription_id)["upcoming_delivery"]

    assert upcoming["payment_status"]["code"] == "payment_failed"
    assert upcoming["payment_note"] == "That payment didn't go through. You can try again."


# --------------------------------------------------------------------------- fail closed


def test_a_broken_order_serializes_with_pay_closed(monkeypatch, db, sample_user, customer):
    from business_app.serializers.order_serializers import serialize_order
    from business_app.services.customer_order_actions import CLOSED_CUSTOMER_ACTIONS

    order = seed(db, sample_user, ROW["click_pending"], now=get_utc_now())

    def boom(cls, order):
        raise RuntimeError("broken order")

    monkeypatch.setattr(OrderScheduleService, "customer_display_status", classmethod(boom))

    listed = customer.listed(order.id)
    assert "error" in listed  # the serializer's partial fallback
    assert listed["customer_actions"] == CLOSED_CUSTOMER_ACTIONS
    published = serialize_order(_fresh(db, order.id))
    assert published["customer_actions"] == CLOSED_CUSTOMER_ACTIONS
    assert published["customer_actions"] is not CLOSED_CUSTOMER_ACTIONS


def test_money_value_is_the_amount_a_write_echoes():
    from business_app.services.customer_order_actions import money_value

    assert money_value(Decimal("30000")) == "30000.00"
    assert money_value(Decimal("35000.5")) == "35000.50"
