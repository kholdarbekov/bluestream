"""Unit tests for NotificationService._dispatch_bottle_delivery_summary (design §3.2).

Isolated from the DB: get_order_bottle_summary, redis_client, and trigger_bot_webhook
are patched at the notification-service module path (module-under-test convention,
mirroring test_notification_tasks.py). The `db` fixture supplies the app context that
NotificationService.__init__ needs (it reads current_app.config).
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from business_app.services.notification_service import (
    BottleSummaryNotReady,
    NotificationService,
)

MODULE = "business_app.services.notification_service"


def _order(order_id=77, order_number="ORD-77"):
    return SimpleNamespace(
        id=order_id,
        order_number=order_number,
        user_id=1,
        delivery_address_id=5,
    )


def _user(telegram_id="998123456789"):
    return SimpleNamespace(id=1, telegram_id=telegram_id, is_bot_active=True)


def _summary(*, expected="0", delivered="0", collected="0", balance="0", recorded=True):
    return {
        "expected_bottles": Decimal(expected),
        "delivery_recorded": recorded,
        "bottles_delivered": Decimal(delivered),
        "bottles_collected": Decimal(collected),
        "balance": Decimal(balance),
    }


@pytest.mark.unit
def test_dispatch_sends_exact_payload_and_claims_key(db):
    service = NotificationService()
    order = _order()
    user = _user()
    fake_redis = MagicMock()
    fake_redis.set.return_value = True

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary",
        return_value=_summary(expected="4", delivered="4", collected="3", balance="1", recorded=True),
    ), patch(f"{MODULE}.trigger_bot_webhook", return_value={"success": True}) as mock_webhook:
        result = service._dispatch_bottle_delivery_summary(None, None, order, user)

    fake_redis.set.assert_called_once_with("notif:bottle_summary:77", 1, nx=True, ex=259200)
    mock_webhook.assert_called_once()
    endpoint, payload = mock_webhook.call_args[0]
    assert endpoint == "/internal/delivery-completed"
    assert payload == {
        "order_id": 77,
        "order_number": "ORD-77",
        "telegram_id": 998123456789,
        "bottles_delivered": "4",
        "bottles_collected": "3",
        "balance": "1",
    }
    assert result["dispatched"] is True
    fake_redis.delete.assert_not_called()


@pytest.mark.unit
def test_dispatch_raises_when_ledger_not_committed_and_does_not_claim_key(db):
    service = NotificationService()
    order = _order(order_id=88)
    user = _user()
    fake_redis = MagicMock()

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary",
        return_value=_summary(expected="4", delivered="0", collected="0", balance="0", recorded=False),
    ), patch(f"{MODULE}.trigger_bot_webhook") as mock_webhook:
        with pytest.raises(BottleSummaryNotReady):
            service._dispatch_bottle_delivery_summary(None, None, order, user)

    fake_redis.set.assert_not_called()
    mock_webhook.assert_not_called()


@pytest.mark.unit
def test_dispatch_skips_when_key_already_claimed(db):
    service = NotificationService()
    order = _order(order_id=99)
    user = _user()
    fake_redis = MagicMock()
    fake_redis.set.return_value = None  # NX lost — key already present

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary",
        return_value=_summary(expected="0", recorded=True),
    ), patch(f"{MODULE}.trigger_bot_webhook") as mock_webhook:
        result = service._dispatch_bottle_delivery_summary(None, None, order, user)

    assert result["skipped"] is True
    assert result["reason"] == "already_dispatched"
    mock_webhook.assert_not_called()


@pytest.mark.unit
def test_dispatch_skips_when_no_telegram_id(db):
    service = NotificationService()
    order = _order()
    user = _user(telegram_id=None)
    fake_redis = MagicMock()

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary"
    ) as mock_summary, patch(f"{MODULE}.trigger_bot_webhook") as mock_webhook:
        result = service._dispatch_bottle_delivery_summary(None, None, order, user)

    assert result["skipped"] is True
    assert result["reason"] == "no_telegram"
    fake_redis.set.assert_not_called()
    mock_summary.assert_not_called()
    mock_webhook.assert_not_called()


@pytest.mark.unit
def test_dispatch_skips_when_telegram_id_non_numeric(db):
    # A legacy/non-numeric telegram_id can't address a Telegram chat. Skip with
    # the same skipped shape instead of raising ValueError deep in payload-build
    # (which would burn the task's 3 retries). The idempotency key must NOT be
    # claimed, and no webhook must fire.
    service = NotificationService()
    order = _order(order_id=103)
    user = _user(telegram_id="not-a-number")
    fake_redis = MagicMock()

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary",
        return_value=_summary(expected="0", recorded=True),
    ), patch(f"{MODULE}.trigger_bot_webhook") as mock_webhook:
        result = service._dispatch_bottle_delivery_summary(None, None, order, user)

    assert result["skipped"] is True
    assert result["reason"] == "invalid_telegram_id"
    fake_redis.set.assert_not_called()
    fake_redis.delete.assert_not_called()
    mock_webhook.assert_not_called()


@pytest.mark.unit
def test_dispatch_deletes_key_and_raises_on_transient_failure(db):
    service = NotificationService()
    order = _order(order_id=101)
    user = _user()
    fake_redis = MagicMock()
    fake_redis.set.return_value = True

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary",
        return_value=_summary(expected="0", recorded=True),
    ), patch(
        f"{MODULE}.trigger_bot_webhook",
        return_value={"success": False, "message": "Bot webhook timeout"},
    ):
        with pytest.raises(RuntimeError):
            service._dispatch_bottle_delivery_summary(None, None, order, user)

    fake_redis.delete.assert_called_once_with("notif:bottle_summary:101")


@pytest.mark.unit
def test_dispatch_does_not_raise_when_bot_webhook_unconfigured(db):
    service = NotificationService()
    order = _order(order_id=102)
    user = _user()
    fake_redis = MagicMock()
    fake_redis.set.return_value = True

    with patch(f"{MODULE}.redis_client", fake_redis), patch(
        f"{MODULE}.BottleTrackingService.get_order_bottle_summary",
        return_value=_summary(expected="0", recorded=True),
    ), patch(
        f"{MODULE}.trigger_bot_webhook",
        return_value={"success": False, "message": "Bot webhook URL not configured"},
    ):
        result = service._dispatch_bottle_delivery_summary(None, None, order, user)

    assert result["success"] is False
    assert result["reason"] == "bot_webhook_unconfigured"
    fake_redis.delete.assert_called_once_with("notif:bottle_summary:102")


@pytest.mark.integration
def test_the_fabricated_summary_matches_the_real_order_bottle_summary(db, sample_user, user_address):
    """Anti-blind-spot pin for `_summary()`.

    Every test above patches `get_order_bottle_summary` with a fabricated dict,
    so none of them can notice the real one changing shape — the same blind spot
    that let the customer bot and the driver statement ship broken. The keys did
    NOT move in the place re-key (only the MEANING of `balance` did: it is the
    whole place's pool now), which is precisely why the pin has to exist before
    the next change rather than after it.
    """
    from business_app.models.order import Order
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from shared.enums import OrderStatus

    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.DELIVERED,
        total_amount=Decimal("50000.00"),
        delivery_address_id=user_address.id,
    )
    db.session.add(order)
    db.session.flush()

    real = BottleTrackingService.get_order_bottle_summary(order)

    assert set(_summary()) <= set(real)
    # The webhook renders these; a str/Decimal flip would reach the customer.
    for key in ("expected_bottles", "bottles_delivered", "bottles_collected", "balance"):
        assert isinstance(real[key], Decimal), key
    assert isinstance(real["delivery_recorded"], bool)
