"""The customer's "your delivery moved to a new date" notice (spec §4.1; R13–R15).

Drives the REAL path a reschedule enqueues: `send_delivery_rescheduled_notification_task`
-> `NotificationService.send_delivery_rescheduled_notification` -> `send_notification` ->
`_send_telegram_notification` / `_send_email_notification` -> `_create_notification_record`.
Only the outbound HTTP POST is faked. Telegram's sendMessage and Brevo's smtp/email both
leave through `requests.post` in notification_service. The templates, the channel choice,
the date format and the persisted `Notification` row are all production code.

conftest's autouse `block_external_side_effects` stubs `send_notification` class-wide so no
test fires a real send. `live_send` puts the production implementation back, the technique
tests/unit/test_notification_service_refactor.py uses.
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from business_app.models.delivery import Delivery
from business_app.models.notification import Notification, NotificationPreference
from business_app.models.order import Order
from business_app.services.email_template_service import get_email_template_service
from business_app.services.notification_service import NotificationService
from business_app.tasks.notification_tasks import send_delivery_rescheduled_notification_task
from business_app.utils.constants import NotificationChannel, NotificationType
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

# Captured at import, before the autouse fixture swaps in its always-succeeds stub.
_REAL_SEND_NOTIFICATION = NotificationService.send_notification

TELEGRAM_URL = "https://api.telegram.org/bottest-token/sendMessage"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
COMPANY_PHONE = "+998712000000"
ORDER_NUMBER = "TG_000512_26"
NEW_DATE = date(2026, 9, 25)
CUSTOMER_CHAT_ID = "810000001"
# A clock time anywhere in the text would be an hour nobody has promised (spec §1: date only).
_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")


@pytest.fixture
def live_send(app, monkeypatch):
    """The production send path with only `requests.post` faked. Yields the fake."""
    monkeypatch.setattr(NotificationService, "send_notification", _REAL_SEND_NOTIFICATION)
    monkeypatch.setitem(app.config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setitem(app.config, "BREVO_API_KEY", "brevo-test-key")
    monkeypatch.setitem(app.config, "BREVO_SENDER_EMAIL", "noreply@aqua-element.uz")
    monkeypatch.setitem(app.config, "COMPANY_NAME", "Aqua Element")
    monkeypatch.setitem(app.config, "COMPANY_PHONE", COMPANY_PHONE)
    response = Mock(status_code=200)
    # One body answers both providers: Telegram reads `ok`/`result`, Brevo reads `messageId`.
    response.json.return_value = {"ok": True, "result": {"message_id": 501}, "messageId": "<brevo-501>"}
    with patch("business_app.services.notification_service.requests.post", return_value=response) as post_mock:
        yield post_mock


def _connect_telegram(db, user, *, language):
    user.telegram_id = CUSTOMER_CHAT_ID
    user.is_bot_active = True
    user.preferred_language = language
    db.session.commit()


def _dated_order(db, user, *, delivery_date=NEW_DATE):
    """An order moved to `delivery_date`, with a delivery row.

    The notice reads nothing off the delivery but its id, so the row's status is immaterial here.
    """
    order = Order(
        user_id=user.id,
        order_number=ORDER_NUMBER,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_date=delivery_date,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime(2026, 9, 25, 4, 0, tzinfo=UTC),
        scheduled_time_slot="anytime",
    )
    db.session.add(delivery)
    db.session.commit()
    return order, delivery


def _sent_rows():
    return Notification.query.filter_by(notification_type=NotificationType.DELIVERY_RESCHEDULED.value).all()


@pytest.mark.parametrize(
    "language, expected",
    [
        (
            "uz",
            f"📅 #{ORDER_NUMBER} buyurtmangizni yetkazib berish 25.09.2026 sanasiga ko'chirildi. "
            f"Savollar bo'lsa: {COMPANY_PHONE}.",
        ),
        (
            "ru",
            f"📅 Доставка вашего заказа #{ORDER_NUMBER} перенесена на 25.09.2026. "
            f"Вопросы? Звоните: {COMPANY_PHONE}.",
        ),
        (
            "en",
            f"📅 Delivery of your order #{ORDER_NUMBER} has been rescheduled to 09/25/2026. "
            f"Questions? Call {COMPANY_PHONE}.",
        ),
    ],
)
def test_a_connected_customer_gets_the_new_date_on_telegram(db, sample_user, live_send, language, expected):
    _connect_telegram(db, sample_user, language=language)
    order, _ = _dated_order(db, sample_user)

    result = send_delivery_rescheduled_notification_task.run(order.id)

    assert result == {"telegram": {"success": True, "message_id": 501}}
    live_send.assert_called_once_with(
        TELEGRAM_URL,
        json={"chat_id": CUSTOMER_CHAT_ID, "text": expected, "parse_mode": "HTML"},
        timeout=15,
    )
    sent = live_send.call_args.kwargs["json"]["text"]
    assert "{" not in sent
    assert not _CLOCK_TIME.search(sent)


def test_the_sent_notice_is_recorded_against_the_order(db, sample_user, live_send):
    """R13 finds an earlier notice by (type, order_id, is_sent). `_create_notification_record`
    copies `Notification.order_id` ONLY from the payload, so a notice sent without it would
    never count as sent and the customer would miss every later re-date."""
    _connect_telegram(db, sample_user, language="en")
    order, delivery = _dated_order(db, sample_user)

    send_delivery_rescheduled_notification_task.run(order.id)

    [row] = _sent_rows()
    assert (row.user_id, row.order_id, row.delivery_id) == (sample_user.id, order.id, delivery.id)
    assert row.channel == NotificationChannel.TELEGRAM
    assert row.is_sent is True
    assert row.recipient_telegram_id == CUSTOMER_CHAT_ID


def test_a_customer_who_blocked_the_bot_gets_the_notice_by_email(db, sample_user, live_send):
    """Review Focus 3. A customer whose bot link is no longer active keeps their `telegram_id`;
    only `is_bot_active` goes False. A rule keyed on `telegram_id` alone (the payment
    confirmation's is) would send into a chat that no longer takes messages and never try the
    email the customer does have."""
    sample_user.telegram_id = "810000002"
    sample_user.is_bot_active = False
    sample_user.preferred_language = "en"
    db.session.commit()
    order, _ = _dated_order(db, sample_user)

    result = send_delivery_rescheduled_notification_task.run(order.id)

    assert result == {"email": {"success": True, "message_id": "<brevo-501>", "status_code": 200}}
    assert live_send.call_count == 1
    assert live_send.call_args.args == (BREVO_URL,)
    email = live_send.call_args.kwargs["json"]
    assert email["to"] == [{"email": "test@example.com", "name": "Test User"}]
    assert email["subject"] == f"Delivery rescheduled: order #{ORDER_NUMBER} - Aqua Element"
    assert f"#{ORDER_NUMBER}" in email["htmlContent"]
    assert "09/25/2026" in email["htmlContent"]
    assert NotificationService._unrendered_placeholders(email["htmlContent"]) == []
    [row] = _sent_rows()
    assert (row.channel, row.recipient_email, row.order_id, row.is_sent) == (
        NotificationChannel.EMAIL,
        "test@example.com",
        order.id,
        True,
    )


def test_a_customer_with_no_bot_and_no_email_is_skipped_and_logged(db, sample_user, live_send):
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.email = None
    db.session.commit()
    order, _ = _dated_order(db, sample_user)

    with patch("business_app.services.notification_service.logger") as log:
        result = send_delivery_rescheduled_notification_task.run(order.id)

    assert result == {"success": False, "skipped": "no_channel"}
    log.info.assert_any_call(
        "Delivery-rescheduled notice skipped: no deliverable channel: order_id=%s user_id=%s",
        order.id,
        sample_user.id,
    )
    live_send.assert_not_called()
    assert _sent_rows() == []


def test_the_status_update_opt_out_does_not_silence_the_notice(db, sample_user, live_send):
    """R14: `delivery_telegram_status_updates` mutes the in_transit/arrived pings. A moved
    delivery date is not one of them, so the same customer still hears about it on Telegram."""
    _connect_telegram(db, sample_user, language="en")
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationService.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
            channel=NotificationChannel.TELEGRAM,
            is_enabled=False,
        )
    )
    db.session.commit()
    order, _ = _dated_order(db, sample_user)

    # The milestone path still honours the opt-out for this very customer: it falls through to email...
    assert NotificationService()._resolve_delivery_status_channels(
        sample_user, DeliveryStatus.IN_TRANSIT.value
    ) == [NotificationChannel.EMAIL]

    result = send_delivery_rescheduled_notification_task.run(order.id)

    # ...while the reschedule notice goes to the connected bot.
    assert result == {"telegram": {"success": True, "message_id": 501}}
    assert live_send.call_args.args == (TELEGRAM_URL,)


def test_the_notice_carries_the_date_the_order_has_when_it_sends(db, sample_user, live_send):
    """A correction (25th -> 27th) that lands before the first notice's task runs must not send
    the 25th. The task reads the order when it runs, not what the reschedule saw."""
    _connect_telegram(db, sample_user, language="ru")
    order, _ = _dated_order(db, sample_user, delivery_date=NEW_DATE)
    order.delivery_date = date(2026, 9, 27)
    db.session.commit()

    send_delivery_rescheduled_notification_task.run(order.id)

    assert live_send.call_args.kwargs["json"]["text"] == (
        f"📅 Доставка вашего заказа #{ORDER_NUMBER} перенесена на 27.09.2026. Вопросы? Звоните: {COMPANY_PHONE}."
    )


@pytest.mark.parametrize(
    "status, delivery_date, reason",
    [
        (OrderStatus.CANCELLED, NEW_DATE, "order_inactive"),
        (OrderStatus.DELIVERED, NEW_DATE, "order_inactive"),
        (OrderStatus.CONFIRMED, None, "no_delivery_date"),
    ],
    ids=["cancelled-before-the-task-ran", "delivered-before-the-task-ran", "undated"],
)
def test_a_notice_for_an_order_that_moved_on_is_dropped(
    db, sample_user, live_send, status, delivery_date, reason
):
    _connect_telegram(db, sample_user, language="en")
    order, _ = _dated_order(db, sample_user)
    order.status = status
    order.delivery_date = delivery_date
    db.session.commit()

    result = send_delivery_rescheduled_notification_task.run(order.id)

    assert result == {"success": False, "skipped": reason}
    live_send.assert_not_called()
    assert _sent_rows() == []


@pytest.mark.parametrize(
    "telegram_id, is_bot_active, email, telegram_allowed, expected",
    [
        ("810000001", True, "c@example.com", True, [NotificationChannel.TELEGRAM]),
        ("810000001", True, "c@example.com", False, [NotificationChannel.EMAIL]),
        ("810000001", False, "c@example.com", True, [NotificationChannel.EMAIL]),
        (None, False, "c@example.com", True, [NotificationChannel.EMAIL]),
        ("810000001", False, None, True, []),
        ("810000001", True, None, False, []),
    ],
    ids=[
        "connected",
        "connected-but-telegram-not-allowed",
        "bot-inactive",
        "never-linked",
        "bot-inactive-no-email",
        "not-allowed-no-email",
    ],
)
def test_one_channel_telegram_else_email(telegram_id, is_bot_active, email, telegram_allowed, expected):
    """The rule both the milestone pings and the reschedule notice use. Task 8 publishes its
    answer as `reschedule_customer_channel`, so the modal promises what the send will do."""
    user = SimpleNamespace(telegram_id=telegram_id, is_bot_active=is_bot_active, email=email)

    assert NotificationService._resolve_telegram_else_email(user, telegram_allowed=telegram_allowed) == expected


@pytest.mark.parametrize(
    "language, subject, marker, rendered_date",
    [
        (
            "uz",
            f"Yetkazib berish sanasi o'zgardi: buyurtma #{ORDER_NUMBER} - Aqua Element",
            "sanasiga ko'chirildi",
            "25.09.2026",
        ),
        ("ru", f"Доставка перенесена: заказ #{ORDER_NUMBER} - Aqua Element", "перенесена на", "25.09.2026"),
        ("en", f"Delivery rescheduled: order #{ORDER_NUMBER} - Aqua Element", "has been rescheduled to", "09/25/2026"),
    ],
)
def test_the_email_names_the_new_date_in_each_language(app, language, subject, marker, rendered_date):
    """Each language renders its OWN file (the marker), not a fallback, and no field leaks."""
    rendered = get_email_template_service().render_notification_email(
        NotificationType.DELIVERY_RESCHEDULED.value,
        language,
        {
            "order_id": 1,
            "delivery_id": 1,
            "order_number": ORDER_NUMBER,
            "new_date": rendered_date,
            "user_name": "Test User",
            "company_name": "Aqua Element",
            "company_phone": COMPANY_PHONE,
            "company_email": "info@aqua-element.uz",
        },
    )

    assert rendered is not None
    assert rendered["subject"] == subject
    content = rendered["content"]
    assert marker in content
    assert f"#{ORDER_NUMBER}" in content
    assert rendered_date in content
    assert COMPANY_PHONE in content
    assert NotificationService._unrendered_placeholders(content) == []
    assert "None" not in content


def test_delivery_rescheduled_is_a_registered_delivery_notification(client, db, sample_user, admin_claim_headers):
    service = NotificationService()

    assert NotificationType.DELIVERY_RESCHEDULED.value in NotificationService.NOTIFICATION_TYPE_GROUPS["delivery"]
    assert service._default_channels_for_type(NotificationType.DELIVERY_RESCHEDULED.value) == [
        NotificationChannel.TELEGRAM
    ]
    assert service._get_user_preferred_channels(sample_user.id, NotificationType.DELIVERY_RESCHEDULED) == [
        NotificationChannel.TELEGRAM
    ]

    # The admin Notifications page lists types from this endpoint; the new one files under "delivery".
    response = client.get("/api/v1/admin/notification-templates/types", headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    types = {row["value"]: row for row in response.get_json()["data"]["types"]}
    assert types["delivery_rescheduled"] == {
        "value": "delivery_rescheduled",
        "label": "Delivery Rescheduled",
        "category": "delivery",
        "in_use": False,
    }


def test_the_task_rides_the_notifications_queue():
    """`celery_app.task_routes` sends `business_app.tasks.notification_tasks.*` to `notifications`.
    The task gets there only by living in that module under its default name."""
    assert send_delivery_rescheduled_notification_task.name == (
        "business_app.tasks.notification_tasks.send_delivery_rescheduled_notification_task"
    )
