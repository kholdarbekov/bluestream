"""The reschedule answer the admin Orders page renders, driven over the two GETs it calls.

`GET /admin/orders` (the list row) and `GET /admin/orders/<id>` (the detail
payload that REPLACES the row in Orders.js's `selectedOrder` while the modal
opens) publish one helper's output, `order_schedule_fields`, so the Reschedule
button cannot mean one thing on the row and another in the modal. Spec:
docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md §3.2, §5.

Only the clock is frozen (`delivery_window.local_now`, the one definition of
"today"). The notification rows, the contract and both endpoints are real.
"""

import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.corporate import CorporateContract, CorporateContractStatus, CorporatePrepaymentAccount
from business_app.models.delivery import Delivery
from business_app.models.notification import Notification
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.utils.constants import NotificationChannel, NotificationStatus, NotificationType
from business_app.utils.password_security import hash_password
from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
from shared.enums import (
    CorporateContractTrackingMode,
    DeliveryStatus,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)

pytestmark = pytest.mark.integration

TZ = ZoneInfo("Asia/Tashkent")
ORDERS = "/api/v1/admin/orders"
# Detail-only: each costs a lookup (notifications, the contract, the driver's
# user row), so the paginated list must never carry them.
DETAIL_ONLY_KEYS = {
    "reschedule_notifies_customer",
    "reschedule_customer_channel",
    "reschedule_driver_losing_stop",
    "reschedule_min_date",
    "reschedule_max_date",
}


def _frozen_local_now():
    """10:00 Tashkent today. Read once per test, so the service's "today" and
    the assertions' "today" cannot land on two sides of local midnight."""
    return datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)


def _order(db, customer_id, *, order_status=OrderStatus.CONFIRMED, delivery_status=None, driver_id=None,
           delivery_date=None, window_start=None, window_end=None, with_payment=True):
    """A cash order as the admin screens see it: a PENDING COD payment and,
    when `delivery_status` is given, its delivery row in that status."""
    order = Order(
        user_id=customer_id,
        status=order_status,
        total_amount=Decimal("50000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_date=delivery_date,
        delivery_window_start=window_start,
        delivery_window_end=window_end,
        order_source="admin",
    )
    db.session.add(order)
    db.session.flush()
    if with_payment:
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=customer_id,
                payment_method=PaymentMethod.CASH,
                amount=order.total_amount,
                currency="UZS",
                status=PaymentStatus.PENDING,
                payment_id=f"resched-meta-{order.id}",
            )
        )
    if delivery_status is not None:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=delivery_status,
                delivery_person_id=driver_id,
                scheduled_date=datetime.now(timezone.utc),
                scheduled_time_slot="anytime",
            )
        )
    db.session.commit()
    return order.id


def _reschedule_notice(db, *, order_id, user_id, is_sent=True):
    """The row `NotificationService.send_notification` persists for a
    `delivery_rescheduled` send: the type as its string value, `order_id`
    copied from the payload, `is_sent` from the channel's result."""
    db.session.add(
        Notification(
            user_id=user_id,
            notification_type=NotificationType.DELIVERY_RESCHEDULED.value,
            channel=NotificationChannel.TELEGRAM,
            title="Delivery Rescheduled",
            message="📅 Delivery of your order has been rescheduled.",
            is_sent=is_sent,
            sent_at=datetime.now(timezone.utc) if is_sent else None,
            delivery_status=NotificationStatus.SENT if is_sent else NotificationStatus.FAILED,
            order_id=order_id,
        )
    )
    db.session.commit()


def _grocery_store(db, *, contract_ends_on):
    """A grocery store on an active AMOUNT contract ending on `contract_ends_on`,
    written the way the admin Contracts form writes it: a bare date, stored as
    that day's 00:00 UTC."""
    store = User(
        email="bahor-market@example.com",
        phone="+998909990011",
        password_hash=hash_password("StorePassword123!"),
        first_name="Bahor",
        last_name="Market",
        user_type=UserType.ENTITY,
        entity_subtype=EntitySubtype.GROCERY_STORE,
        company_name="Bahor Market",
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(store)
    db.session.flush()
    contract = CorporateContract(
        user_id=store.id,
        contract_number="GS-RESCHED-001",
        name="Bahor Market supply",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=30),
        end_date=datetime.combine(contract_ends_on, time(0, 0), tzinfo=timezone.utc),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.AMOUNT,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(CorporatePrepaymentAccount(contract_id=contract.id, is_active=True))
    db.session.commit()
    return store


def _detail(client, headers, order_id, now_local):
    with patch("business_app.utils.delivery_window.local_now", return_value=now_local):
        resp = client.get(f"{ORDERS}/{order_id}", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["order"]


def _list_row(client, headers, order_id):
    resp = client.get(ORDERS, headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return next(r for r in resp.get_json()["data"]["items"] if r["id"] == order_id)


def test_the_detail_publishes_the_schedule_and_the_whole_reschedule_answer(
    client, db, admin_claim_headers, sample_user, delivery_driver
):
    """A driver is on the way. The modal must say the customer will hear (by
    Telegram, since their bot is connected) and that Delivery Driver loses
    the stop.

    The detail also gains the schedule block it used to omit. Opening the
    modal replaced the list row and blanked its window and release time."""
    sample_user.telegram_id = "700100200"
    sample_user.is_bot_active = True
    db.session.commit()
    now_local = _frozen_local_now()
    today = now_local.date()
    order_id = _order(
        db, sample_user.id,
        order_status=OrderStatus.OUT_FOR_DELIVERY,
        delivery_status=DeliveryStatus.IN_TRANSIT,
        driver_id=delivery_driver.id,
        delivery_date=today,
        window_start=time(12, 0),
        window_end=time(18, 0),
    )

    body = _detail(client, admin_claim_headers, order_id, now_local)

    assert body["delivery_date"] == today.isoformat()
    assert body["delivery_window"] == {"start": "12:00", "end": "18:00", "kind": "between", "label": "12:00-18:00"}
    assert body["awaiting_release"] is False and body["release_at"] is None
    assert body["can_reschedule"] is True and body["reschedule_block_code"] is None
    assert body["reschedule_notifies_customer"] is True
    assert body["reschedule_customer_channel"] == "telegram"
    assert body["reschedule_driver_losing_stop"] == {"id": delivery_driver.id, "name": "Delivery Driver"}
    assert body["reschedule_min_date"] == today.isoformat()
    assert body["reschedule_max_date"] == (today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)).isoformat()

    row = _list_row(client, admin_claim_headers, order_id)
    for key in ("delivery_date", "delivery_window", "awaiting_release", "release_at",
                "can_reschedule", "reschedule_block_code"):
        assert row[key] == body[key], key
    assert not DETAIL_ONLY_KEYS & set(row)


@pytest.mark.parametrize(
    "telegram_id,is_bot_active,email,expected_channel",
    [
        ("700100201", True, "cust@example.com", "telegram"),
        # Blocked the bot but has an email: the notice falls through to email.
        ("700100202", False, "cust@example.com", "email"),
        # Review Focus #5: nothing to reach them by. The backend says so with
        # None, and the modal says "tell them yourself" instead of promising a
        # notice.
        (None, False, None, None),
    ],
    ids=["telegram-connected", "bot-blocked-has-email", "unreachable"],
)
def test_the_detail_names_the_channel_the_notice_would_use(
    client, db, admin_claim_headers, sample_user, delivery_driver,
    telegram_id, is_bot_active, email, expected_channel,
):
    """ARRIVED: the customer is owed a notice whichever way it can travel, so
    `reschedule_notifies_customer` stays True even when no channel exists.
    That combination is the one the modal must word as a warning."""
    sample_user.telegram_id = telegram_id
    sample_user.is_bot_active = is_bot_active
    sample_user.email = email
    db.session.commit()
    now_local = _frozen_local_now()
    order_id = _order(
        db, sample_user.id,
        order_status=OrderStatus.OUT_FOR_DELIVERY,
        delivery_status=DeliveryStatus.ARRIVED,
        driver_id=delivery_driver.id,
        delivery_date=now_local.date(),
    )

    body = _detail(client, admin_claim_headers, order_id, now_local)

    assert body["reschedule_notifies_customer"] is True
    assert body["reschedule_customer_channel"] == expected_channel


def test_an_assigned_delivery_notifies_only_a_customer_who_was_already_told(
    app, client, db, admin_claim_headers, sample_user, delivery_driver
):
    """R13's second half. An assigned driver has not left yet, so a first
    reschedule tells nobody. Once a notice has gone out for THIS order, every
    later re-date keeps the customer current. Reading the notice does not undo
    that: `mark-all-read` flips `delivery_status`, never `is_sent`."""
    now_local = _frozen_local_now()
    order_id = _order(
        db, sample_user.id,
        delivery_status=DeliveryStatus.ASSIGNED,
        driver_id=delivery_driver.id,
        delivery_date=now_local.date(),
    )
    other_order_id = _order(db, sample_user.id, delivery_date=now_local.date())
    # Neither of these means "this order's customer was told":
    _reschedule_notice(db, order_id=order_id, user_id=sample_user.id, is_sent=False)
    _reschedule_notice(db, order_id=other_order_id, user_id=sample_user.id)

    first = _detail(client, admin_claim_headers, order_id, now_local)
    assert first["reschedule_notifies_customer"] is False
    assert first["reschedule_driver_losing_stop"] == {"id": delivery_driver.id, "name": "Delivery Driver"}

    _reschedule_notice(db, order_id=order_id, user_id=sample_user.id)
    assert _detail(client, admin_claim_headers, order_id, now_local)["reschedule_notifies_customer"] is True

    with app.app_context():
        customer_token = create_access_token(identity=str(sample_user.id))
    read = client.post(
        "/api/v1/notifications/mark-all-read",
        headers={"Authorization": f"Bearer {customer_token}"},
    )
    assert read.status_code == 200, read.get_data(as_text=True)
    sent = Notification.query.filter_by(order_id=order_id, is_sent=True).one()
    assert sent.delivery_status == NotificationStatus.READ

    assert _detail(client, admin_claim_headers, order_id, now_local)["reschedule_notifies_customer"] is True


@pytest.mark.parametrize(
    "order_status,delivery_status,block_code",
    [
        (OrderStatus.DELIVERED, DeliveryStatus.DELIVERED, "ORDER_NOT_RESCHEDULABLE"),
        (OrderStatus.CANCELLED, None, "ORDER_NOT_RESCHEDULABLE"),
        (OrderStatus.CANCELLED, DeliveryStatus.CANCELLED, "ORDER_NOT_RESCHEDULABLE"),
        (OrderStatus.RETURNED, DeliveryStatus.RETURNED, "ORDER_NOT_RESCHEDULABLE"),
        # A live order whose delivery a driver-bot cancel left behind (dev
        # order 153): refused, not repaired (R2).
        (OrderStatus.CONFIRMED, DeliveryStatus.CANCELLED, "DELIVERY_NOT_RESCHEDULABLE"),
        (OrderStatus.OUT_FOR_DELIVERY, DeliveryStatus.DELIVERED, "DELIVERY_NOT_RESCHEDULABLE"),
        (OrderStatus.CONFIRMED, DeliveryStatus.RETURNED, "DELIVERY_NOT_RESCHEDULABLE"),
    ],
    ids=[
        "delivered-order", "cancelled-order-no-row", "cancelled-order", "returned-order",
        "live-order-cancelled-delivery", "live-order-delivered-delivery", "live-order-returned-delivery",
    ],
)
def test_a_blocked_order_says_why_on_the_row_and_in_the_detail_and_promises_nothing(
    client, db, admin_claim_headers, sample_user, delivery_driver, order_status, delivery_status, block_code
):
    now_local = _frozen_local_now()
    driver_id = delivery_driver.id if delivery_status in (DeliveryStatus.DELIVERED, DeliveryStatus.RETURNED) else None
    order_id = _order(db, sample_user.id, order_status=order_status, delivery_status=delivery_status,
                      driver_id=driver_id)

    row = _list_row(client, admin_claim_headers, order_id)
    body = _detail(client, admin_claim_headers, order_id, now_local)

    for payload in (row, body):
        assert payload["can_reschedule"] is False
        assert payload["reschedule_block_code"] == block_code
    assert body["reschedule_notifies_customer"] is False
    assert body["reschedule_driver_losing_stop"] is None


def test_a_grocery_store_can_be_moved_no_later_than_its_contract_end(client, db, admin_claim_headers):
    """R11: the delivery charges the store's AMOUNT contract, and the charge
    refuses once the contract has ended. The contract end is the last day on
    offer, as the admin sees it on the contract, not the 15-day horizon."""
    now_local = _frozen_local_now()
    today = now_local.date()
    store = _grocery_store(db, contract_ends_on=today + timedelta(days=5))
    order_id = _order(db, store.id, delivery_date=today + timedelta(days=3))

    body = _detail(client, admin_claim_headers, order_id, now_local)

    assert body["reschedule_min_date"] == today.isoformat()
    assert body["reschedule_max_date"] == (today + timedelta(days=5)).isoformat()
    # Held for three more days: the detail now carries the release fields the
    # list row always had.
    assert body["awaiting_release"] is True
    assert body["release_at"] == OrderScheduleService.release_at(db.session.get(Order, order_id)).isoformat()


def test_a_grocery_store_with_two_active_contracts_still_opens_with_the_plain_horizon(
    client, db, admin_claim_headers
):
    """Two active AMOUNT contracts is a contract-side ambiguity: the delivery
    charge refuses on it too, so a date cap could not protect the charge. The
    order modal must still open, offering the booking horizon, rather than the
    whole detail endpoint failing on data the admin fixes elsewhere."""
    now_local = _frozen_local_now()
    today = now_local.date()
    store = _grocery_store(db, contract_ends_on=today + timedelta(days=5))
    db.session.add(
        CorporateContract(
            user_id=store.id,
            contract_number="GS-RESCHED-002",
            name="Bahor Market second supply",
            status=CorporateContractStatus.ACTIVE,
            start_date=datetime.now(timezone.utc) - timedelta(days=10),
            end_date=datetime.combine(today + timedelta(days=3), time(0, 0), tzinfo=timezone.utc),
            currency="UZS",
            is_active=True,
            tracking_mode=CorporateContractTrackingMode.AMOUNT,
        )
    )
    db.session.commit()
    order_id = _order(db, store.id, delivery_date=today + timedelta(days=1))

    body = _detail(client, admin_claim_headers, order_id, now_local)

    assert body["can_reschedule"] is True
    assert body["reschedule_min_date"] == today.isoformat()
    assert body["reschedule_max_date"] == (today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)).isoformat()


def test_list_rows_answer_without_the_detail_lookups(
    client, db, admin_claim_headers, count_queries, delivery_driver
):
    """The seeded data would make a detail-mode answer query `notifications`
    (an ASSIGNED row asks whether the customer was told before) and
    `corporate_contracts` (a grocery store's max date). The list serializes up
    to 100 rows, so it must touch neither table."""
    today = _frozen_local_now().date()
    store = _grocery_store(db, contract_ends_on=today + timedelta(days=5))
    order_id = _order(db, store.id, delivery_status=DeliveryStatus.ASSIGNED, driver_id=delivery_driver.id,
                      with_payment=False)
    _reschedule_notice(db, order_id=order_id, user_id=store.id)

    with count_queries() as counter:
        row = _list_row(client, admin_claim_headers, order_id)

    assert row["can_reschedule"] is True and row["reschedule_block_code"] is None
    assert not DETAIL_ONLY_KEYS & set(row)
    touched = [s for s in counter.statements if re.search(r"\b(notifications|corporate_contracts)\b", s)]
    assert touched == [], touched
