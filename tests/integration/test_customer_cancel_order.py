"""F5: a customer may cancel their own order only while nothing is paid and no driver has it.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.3 and
§8 item 3.

Order 1277 (TG_000508_26) is why. Its Cancel button was drawn while the order was pending;
the customer tapped it after the delivery had failed, and it ended an order that was only
waiting for a new date. The rule is now one backend predicate,
`OrderService.customer_cancel_block_code`. `GET /orders` and `GET /orders/<id>` publish it
as `can_customer_cancel`, and `POST /orders/<id>/cancel` enforces it on locked rows. Every
test drives those routes the way the bot and the web call them: neither client sends a
body with the cancel POST.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple, Optional

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text
from sqlalchemy.orm.attributes import set_committed_value

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.translation import Translation
from business_app.utils.constants import NotificationType
from business_app.utils.state_validators import DELIVERY_DRIVERLESS_STATES
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus

ORDERS_URL = "/api/v1/orders/"
ORDER_URL = "/api/v1/orders/{order_id}"
CANCEL_URL = "/api/v1/orders/{order_id}/cancel"
REFUSED = "ORDER_NOT_CUSTOMER_CANCELLABLE"

# A link no config default produces, so finding it in the message proves the route read
# COMPANY_TELEGRAM_BOT_URL when it answered.
BOT_URL = "https://t.me/cancel_refusal_probe_bot"

# The web copy each published reason_code is rendered from (the plan's interface contract).
REFUSAL_COPY_KEY = {
    "NOT_CANCELLABLE": "api.orders.cancel_refused.not_cancellable",
    "AWAITING_NEW_DATE": "api.orders.cancel_refused.awaiting_new_date",
    "PAID": "api.orders.cancel_refused.paid",
    "WITH_DRIVER": "api.orders.cancel_refused.with_driver",
}


class Case(NamedTuple):
    name: str
    order_status: OrderStatus
    is_paid: bool
    payment_status: PaymentStatus
    delivery_status: Optional[DeliveryStatus]
    refused: Optional[str]  # the published reason_code; None when the customer may cancel


# Short names keep each row of the matrix on one line.
OS, PS, DS = OrderStatus, PaymentStatus, DeliveryStatus

CASES = [
    Case("pending-unpaid", OS.PENDING, False, PS.PENDING, None, None),
    Case("pending-paid", OS.PENDING, True, PS.COMPLETED, None, "PAID"),
    Case("confirmed-no-delivery-row", OS.CONFIRMED, False, PS.PENDING, None, None),
    Case("confirmed-scheduled", OS.CONFIRMED, False, PS.PENDING, DS.SCHEDULED, None),
    Case("confirmed-in-the-pool", OS.CONFIRMED, False, PS.PENDING, DS.PENDING, None),
    Case("confirmed-held-for-a-later-day", OS.CONFIRMED, False, PS.PENDING, DS.RESCHEDULED, None),
    Case("confirmed-driver-accepted", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, "WITH_DRIVER"),
    Case("preparing", OS.PREPARING, False, PS.PENDING, DS.ASSIGNED, "WITH_DRIVER"),
    Case("out-for-delivery", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.IN_TRANSIT, "WITH_DRIVER"),
    Case("awaiting-new-date-confirmed", OS.CONFIRMED, False, PS.PENDING, DS.FAILED, "AWAITING_NEW_DATE"),
    Case("awaiting-new-date-out-for-delivery", OS.OUT_FOR_DELIVERY, False, PS.PENDING, DS.FAILED, "AWAITING_NEW_DATE"),
    # Awaiting is checked before paid: the customer hears the more specific true thing.
    Case("awaiting-new-date-and-paid", OS.CONFIRMED, True, PS.COMPLETED, DS.FAILED, "AWAITING_NEW_DATE"),
    Case("partially-paid", OS.CONFIRMED, False, PS.PARTIALLY_PAID, None, "PAID"),
    # The payment went through before `is_paid` was stamped: the money is still in.
    Case("completed-payment-not-yet-stamped", OS.CONFIRMED, False, PS.COMPLETED, None, "PAID"),
    # Checked first of all, so a delivered order is "not cancellable", never "paid".
    Case("delivered", OS.DELIVERED, True, PS.COMPLETED, DS.DELIVERED, "NOT_CANCELLABLE"),
]


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _seed(db, customer, driver, case):
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-F5-{case.name}",
        status=case.order_status,
        payment_method=PaymentMethod.CLICK,
        is_paid=case.is_paid,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
    )
    db.session.add(order)
    db.session.flush()
    collected = {PS.COMPLETED: Decimal("15000.00"), PS.PARTIALLY_PAID: Decimal("5000.00")}.get(
        case.payment_status, Decimal("0.00")
    )
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal("15000.00"),
            amount_collected=collected,
            outstanding_amount=Decimal("15000.00") - collected,
            currency="UZS",
            status=case.payment_status,
            payment_id=f"PAY-F5-{case.name}",
        )
    )
    delivery = None
    if case.delivery_status is not None:
        delivery = Delivery(
            order_id=order.id,
            status=case.delivery_status,
            # The CHECK constraint's own set: a driverless row never carries a driver.
            delivery_person_id=None if case.delivery_status in DELIVERY_DRIVERLESS_STATES else driver.id,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
    db.session.commit()
    return order, delivery


@pytest.fixture
def refusal_copy(app, db, monkeypatch):
    """Seed the web refusal copy exactly as the canonical seeder ships it, and pin the bot link.

    The test DB carries no translation rows, so without this `get_translation` returns the
    raw key and the message could not be checked. Only the `api.orders.cancel_refused.*`
    rows the seeder owns are loaded, so a route asking for any key the seeder does not ship
    still gets the raw key back and fails below."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    copy = {key: texts for key, texts in BACKEND_TRANSLATIONS.items() if key.startswith("api.orders.cancel_refused.")}
    for key, texts in copy.items():
        for language, value in texts.items():
            db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()
    return copy


@pytest.fixture
def customer_told(monkeypatch):
    """Everything the cancel tells the customer, with its exact arguments.

    A successful cancel queues the status message after its commit, and the route sends its
    own notification. A refusal must do neither."""
    from business_app.services.notification_service import NotificationService
    from business_app.tasks.notification_tasks import send_order_notification_task

    told = []

    # Mirrors NotificationService.send_notification exactly, like the autouse stub in
    # tests/conftest.py, so a drifted call fails here instead of being swallowed.
    def _send_notification(
        _self,
        user_id,
        notification_type,
        channels=None,
        template_data=None,
        priority="normal",
        template_override=None,
        campaign_id=None,
    ):
        told.append(("notification", str(user_id), notification_type, (template_data or {}).get("order_number")))
        return {}

    def _queue_status_message(order_id: int, notification_type: str):
        told.append(("status_message", order_id, notification_type))

    monkeypatch.setattr(NotificationService, "send_notification", _send_notification, raising=True)
    monkeypatch.setitem(vars(send_order_notification_task), "delay", _queue_status_message)
    return told


def _assert_web_copy(app, message, seeded):
    """The web shows `message` as it is: the seeded copy with the bot link filled in, and
    never a phone number (F16)."""
    assert message in {text.format(bot_url=BOT_URL) for text in seeded.values()}, message
    assert BOT_URL in message
    assert app.config["COMPANY_PHONE"] not in message
    assert not re.search(r"\d{5,}|tel:", message), message


@pytest.mark.integration
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_cancel_a_customer_is_offered_is_the_cancel_the_backend_allows(
    app, client, db, sample_user, delivery_driver, refusal_copy, customer_told, case
):
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    headers = _headers(app, sample_user)

    detail = client.get(ORDER_URL.format(order_id=order.id), headers=headers)
    assert detail.status_code == 200, detail.get_json()
    published = detail.get_json()["data"]["order"]
    assert "error" not in published, f"the degraded fallback answered, so its False proves nothing: {published}"
    listed = client.get(ORDERS_URL, headers=headers).get_json()["data"]["orders"]
    assert [row["can_customer_cancel"] for row in listed if row["id"] == order.id] == [
        published["can_customer_cancel"]
    ], "the list the web draws Cancel from must agree with the detail"

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=headers)
    body = response.get_json()
    db.session.expire_all()

    # The button a customer is shown and the write it sends agree, in every row.
    assert published["can_customer_cancel"] is (response.status_code == 200), body

    if case.refused is None:
        assert response.status_code == 200, body
        assert body["data"]["order"]["status"] == "cancelled"
        assert body["data"]["order"]["can_customer_cancel"] is False
        assert db.session.get(Order, order.id).status is OrderStatus.CANCELLED
        if delivery is not None:
            assert db.session.get(Delivery, delivery.id).status is DeliveryStatus.CANCELLED
        assert ("status_message", order.id, "status_changed_cancelled") in customer_told
        assert ("notification", str(sample_user.id), NotificationType.ORDER_UPDATE, order.order_number) in customer_told
    else:
        assert response.status_code == 400, body
        assert body["success"] is False
        assert body["data"] == {"error_code": REFUSED, "reason_code": case.refused, "can_customer_cancel": False}
        _assert_web_copy(app, body["message"], refusal_copy[REFUSAL_COPY_KEY[case.refused]])
        # Refused before anything moved: the order, its delivery and its money are as they were,
        # and the customer is told nothing beyond the refusal itself.
        assert db.session.get(Order, order.id).status is case.order_status
        if delivery is not None:
            assert db.session.get(Delivery, delivery.id).status is case.delivery_status
        assert Payment.query.filter_by(order_id=order.id).one().status is case.payment_status
        assert customer_told == []


@pytest.mark.integration
def test_a_driver_accept_this_session_has_not_seen_is_still_refused(
    app, client, db, sample_user, delivery_driver, customer_told
):
    """The customer's page was drawn while the delivery sat in the pool, and a driver took it a
    moment ago on another connection. `DeliveryAssignmentService.assign_driver` locks only the
    Delivery, so the decision must be made on that row, locked and re-read, never on the copy
    this session already holds. Otherwise the cancel cascade takes the stop off the driver who
    just accepted it.

    The session's stale picture is built deterministically. The row is committed `assigned`,
    then the instance in the identity map is put back to what this session last read, as
    committed state, so only a `populate_existing` re-read can see past it. It is the technique
    of test_admin_order_reschedule.py::test_the_reschedule_decides_on_the_row_it_locked_not_a_stale_copy,
    committed here because a refusal rolls back and would otherwise undo the driver's accept too."""
    case = Case("stale-read", OS.CONFIRMED, False, PS.PENDING, DS.PENDING, "WITH_DRIVER")
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    stale = db.session.get(Order, order.id).delivery
    assert stale is delivery and stale.status is DeliveryStatus.PENDING

    db.session.execute(
        text("UPDATE deliveries SET status = 'assigned', delivery_person_id = :driver WHERE id = :delivery"),
        {"driver": delivery_driver.id, "delivery": delivery.id},
    )
    db.session.commit()
    assert stale.status is DeliveryStatus.ASSIGNED  # the commit refreshed it; put the stale read back
    set_committed_value(stale, "status", DeliveryStatus.PENDING)
    set_committed_value(stale, "delivery_person_id", None)
    assert db.session.get(Delivery, delivery.id) is stale and stale.status is DeliveryStatus.PENDING

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"]["reason_code"] == "WITH_DRIVER"
    db.session.expire_all()
    landed = db.session.get(Delivery, delivery.id)
    assert (landed.status, landed.delivery_person_id) == (DeliveryStatus.ASSIGNED, delivery_driver.id)
    assert db.session.get(Order, order.id).status is OrderStatus.CONFIRMED
    assert customer_told == []


@pytest.mark.integration
def test_another_customers_order_is_not_found_and_left_alone(
    app, client, db, sample_user, second_sample_user, delivery_driver, customer_told
):
    """Ownership is checked on the locked row: a foreign id answers exactly like a missing one."""
    order, _delivery = _seed(
        db, sample_user, delivery_driver, Case("foreign", OS.PENDING, False, PS.PENDING, None, None)
    )

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, second_sample_user))

    assert response.status_code == 404, response.get_json()
    assert response.get_json()["success"] is False
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OrderStatus.PENDING
    assert customer_told == []


class TestTheWebRefusalCopyIsSeeded:
    def test_there_is_one_web_copy_per_published_reason_code(self):
        from business_app.services.order_service import (
            CUSTOMER_CANCEL_AWAITING_NEW_DATE,
            CUSTOMER_CANCEL_NOT_CANCELLABLE,
            CUSTOMER_CANCEL_PAID,
            CUSTOMER_CANCEL_WITH_DRIVER,
        )

        assert set(REFUSAL_COPY_KEY) == {
            CUSTOMER_CANCEL_NOT_CANCELLABLE,
            CUSTOMER_CANCEL_AWAITING_NEW_DATE,
            CUSTOMER_CANCEL_PAID,
            CUSTOMER_CANCEL_WITH_DRIVER,
        }

    def test_every_web_refusal_is_trilingual_points_to_the_bot_and_names_no_phone(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in REFUSAL_COPY_KEY.values():
            assert key in BACKEND_TRANSLATIONS, f"{key} would reach the customer as a raw key"
            assert set(BACKEND_TRANSLATIONS[key]) == {"en", "uz", "ru"}, key
            for language, value in BACKEND_TRANSLATIONS[key].items():
                assert "{bot_url}" in value, f"{key}/{language} does not point to the bot"
                assert not re.search(r"\d{5,}|tel:|\+998", value), f"{key}/{language} names a phone"
            assert "’" not in BACKEND_TRANSLATIONS[key]["uz"], f"{key}/uz has a curly apostrophe"

    def test_the_awaiting_copy_never_says_a_driver_has_the_order(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        copy = BACKEND_TRANSLATIONS["api.orders.cancel_refused.awaiting_new_date"]
        assert "driver" not in copy["en"].lower()
        assert "haydovchi" not in copy["uz"].lower()
        assert "водител" not in copy["ru"].lower()
