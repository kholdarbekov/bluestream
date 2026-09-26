"""A payment confirmation describes the one collection that triggered it.

A payment can be funded by several cash-collection events (prepaid credit
reserved from an older event, an earlier partial collection). The customer
message must quote the triggering event, not the sum of every funding event.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.translation import Translation
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.notification_service import NotificationService
from business_app.tasks import notification_tasks
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

_REAL_SEND_NOTIFICATION = NotificationService.send_notification

_PAYMENT_LABELS = {
    "telegram.payment_cash": {"uz": "💰 Naqd pul", "en": "💰 Cash on Delivery", "ru": "💰 Наличными"},
    "telegram.payment_card": {"uz": "💳 Karta", "en": "💳 Card", "ru": "💳 Карта"},
}


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notification_tasks.send_payment_confirmation_task,
        "delay",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


@pytest.fixture
def telegram_customer(app, db, sample_user, monkeypatch):
    def _make(language):
        sample_user.telegram_id = "820000001"
        sample_user.preferred_language = language
        for key, by_language in _PAYMENT_LABELS.items():
            for lang, value in by_language.items():
                db.session.add(Translation(key=key, language=lang, value=value, category="general", is_active=True))
        db.session.commit()
        monkeypatch.setitem(app.config, "TELEGRAM_BOT_TOKEN", "test-token")
        return sample_user

    return _make


def _cash_order(db, user, *, number, total, status=OrderStatus.CONFIRMED, days_ago=0):
    created_at = datetime.now(UTC) - timedelta(days=days_ago)
    order = Order(
        user_id=user.id,
        order_number=number,
        status=status,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
        payment_method=PaymentMethod.CASH,
        created_at=created_at,
    )
    db.session.add(order)
    db.session.flush()
    if status == OrderStatus.DELIVERED:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.DELIVERED,
                scheduled_date=created_at,
                scheduled_time_slot="09:00-12:00",
                delivered_at=created_at,
                actual_delivery_time=created_at,
            )
        )
        db.session.flush()
    return order


def _collect(service, customer, admin, order, amount, *, source="standalone_meeting"):
    return service.post_collection(
        customer_id=customer.id,
        amount=Decimal(amount),
        source=source,
        recorded_by_user_id=admin.id,
        order_id=order.id,
        notes="Collected",
    )


def _run_task_and_capture_text(monkeypatch, call):
    """Run a captured confirmation task for real; only Redis and Telegram HTTP are faked."""
    args, kwargs = call
    monkeypatch.setattr(NotificationService, "send_notification", _REAL_SEND_NOTIFICATION)
    fake_redis = MagicMock()
    fake_redis.get.return_value = None
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": 1}}
    with (
        patch("business_app.redis_client", fake_redis),
        patch("business_app.services.notification_service.requests.post", return_value=response) as post,
    ):
        notification_tasks.send_payment_confirmation_task.run(*args, **kwargs)
    return post.call_args.kwargs["json"]["text"]


def _two_collections_on_one_order(db, service, customer, admin):
    order = _cash_order(db, customer, number="TG_PART_26", total="100000.00", status=OrderStatus.DELIVERED, days_ago=3)
    payment = service.ensure_cod_payment_for_order(order)
    db.session.commit()
    _collect(service, customer, admin, order, "60000.00")
    later = _collect(service, customer, admin, order, "40000.00")
    return payment, later


def test_card_transfer_on_a_credit_covered_order_quotes_only_the_transfer(
    db, admin_user, telegram_customer, dispatched, monkeypatch
):
    """Credit reserved from an older transfer + a new card transfer: quote the new
    transfer alone, by card, with nothing owed and no stale debt line."""
    customer = telegram_customer("uz")
    service = CashCollectionService()

    older = _cash_order(db, customer, number="TG_OLD_26", total="88650.00", status=OrderStatus.DELIVERED, days_ago=20)
    service.ensure_cod_payment_for_order(older)
    db.session.commit()
    _collect(service, customer, admin_user, older, "90000.00", source="personal_card_transfer")
    assert service.get_customer_prepaid_balance(customer.id) == Decimal("1350.00")

    order = _cash_order(db, customer, number="TG_NEW_26", total="88650.00")
    payment = service.ensure_cod_payment_for_order(order)
    db.session.flush()
    assert service.reserve_customer_prepaid_credit_for_payment(payment, actor_user_id=customer.id) == Decimal("1350.00")
    db.session.commit()
    dispatched.clear()

    _collect(service, customer, admin_user, order, "90000.00", source="personal_card_transfer")

    assert len(dispatched) == 1
    text = _run_task_and_capture_text(monkeypatch, dispatched[0])
    assert "Summa: 90,000 so'm" in text
    assert "180,000" not in text
    assert "Usul: 💳 Karta" in text
    assert "To'lov tasdiqlandi!" in text
    assert "TG_OLD_26" not in text
    assert "yetishmadi" not in text
    assert "Buyurtmaning 1,350 so'mi avvalgi to'lovlaringiz hisobidan qoplandi." in text
    assert "ortiqcha 2,700 so'm" in text


def test_a_later_collection_on_the_same_order_quotes_only_itself(
    db, admin_user, telegram_customer, dispatched, monkeypatch
):
    customer = telegram_customer("en")
    service = CashCollectionService()
    _payment, _later = _two_collections_on_one_order(db, service, customer, admin_user)

    assert len(dispatched) == 2
    text = _run_task_and_capture_text(monkeypatch, dispatched[1])
    assert "Amount: 40,000 UZS" in text
    assert "Payment Confirmed!" in text
    assert "60,000 UZS of the order was covered by your earlier payments." in text


def test_breakdown_without_an_event_describes_the_latest_collection(db, sample_user, admin_user, dispatched):
    """Callers that predate event scoping (in-flight tasks) still get one collection, never the sum."""
    service = CashCollectionService()
    payment, _later = _two_collections_on_one_order(db, service, sample_user, admin_user)

    breakdown = NotificationService()._build_payment_collection_breakdown(payment)

    assert breakdown["received_total"] == Decimal("40000.00")
    assert breakdown["covered_elsewhere"] == Decimal("60000.00")
    assert breakdown["case"] == "exact"


def test_confirmation_is_dispatched_once_per_collection_for_its_own_order(db, sample_user, admin_user, dispatched):
    """One collection settling an older debt and its own order is one message, anchored on its own order."""
    service = CashCollectionService()
    older = _cash_order(db, sample_user, number="TG_A_26", total="20000.00", status=OrderStatus.DELIVERED, days_ago=10)
    newer = _cash_order(db, sample_user, number="TG_B_26", total="30000.00", status=OrderStatus.DELIVERED, days_ago=1)
    service.ensure_cod_payment_for_order(older)
    newer_payment = service.ensure_cod_payment_for_order(newer)
    db.session.commit()
    newer_payment_id = newer_payment.id

    event = _collect(service, sample_user, admin_user, newer, "50000.00")

    assert len(dispatched) == 1
    args, kwargs = dispatched[0]
    assert args[0] == newer_payment_id
    assert kwargs["cash_collection_event_id"] == event.id


def test_a_voided_collection_is_not_announced(db, sample_user, admin_user, dispatched, monkeypatch):
    service = CashCollectionService()
    order = _cash_order(db, sample_user, number="TG_VOID_26", total="10000.00", status=OrderStatus.DELIVERED)
    payment = service.ensure_cod_payment_for_order(order)
    db.session.commit()
    event = _collect(service, sample_user, admin_user, order, "10000.00")
    service.reverse_collection_event(event.id, reversed_by_user_id=admin_user.id, reason="Recorded twice")

    send = Mock()
    monkeypatch.setattr(NotificationService, "send_notification", send)
    result = NotificationService().send_payment_notification(payment.id, cash_collection_event_id=event.id)

    assert result["skipped"] is True
    send.assert_not_called()


def test_task_forwards_the_collection_and_dedupes_on_it(app):
    store = {}
    fake_redis = MagicMock()
    fake_redis.get.side_effect = store.get
    fake_redis.setex.side_effect = lambda key, ttl, value: store.__setitem__(key, value)
    with (
        patch("business_app.tasks.notification_tasks.NotificationService") as service_cls,
        patch("business_app.redis_client", fake_redis),
    ):
        service_cls.return_value.send_payment_notification.return_value = {"success": True}
        notification_tasks.send_payment_confirmation_task.run(
            601, collection_state_token="5000.00", cash_collection_event_id=77
        )
        retry = notification_tasks.send_payment_confirmation_task.run(
            601, collection_state_token="9000.00", cash_collection_event_id=77
        )

    service_cls.return_value.send_payment_notification.assert_called_once_with(601, cash_collection_event_id=77)
    assert "notif:payment_confirm:601:event:77" in store
    assert retry["skipped"] is True
