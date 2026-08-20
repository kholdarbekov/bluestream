"""`auto_confirm_pending_orders`' delivery backstop must not cry wolf.

The task confirms an aged PENDING order and then checks, defensively, that a
`Delivery` row exists — logging ERROR when it does not, because that used to
mean delivery creation had silently failed inside
`_handle_status_change_actions`.

Scheduled orders changed what "no delivery" means. A future-dated order has NO
delivery row BY DESIGN until the release sweep runs on its morning, so every
scheduled order reaching this backstop emitted an ERROR. This feature's stated
risk is that "nothing was due" and "the sweep is broken" look identical from
outside; an ERROR per normal scheduled order poisons the one channel that has
to stay meaningful.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import UserAddress
from business_app.tasks.order_tasks import auto_confirm_pending_orders
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

_MISSING_DELIVERY_ERROR = "auto-confirmed but delivery was NOT created"


def _error_messages(mock_error):
    """`logger` here is a celery `get_task_logger`, which does not propagate to
    the root logger the way `caplog` needs — `caplog.text` comes back EMPTY
    even while the ERROR is plainly emitted (it shows up in captured stdout via
    the app's JSON handler). Asserting on the patched logger instead is both
    reliable and a payload assertion rather than a call-occurrence one."""
    return [str(call.args[0]) if call.args else "" for call in mock_error.call_args_list]


def _aged_cash_order(db, user, *, delivery_date):
    """An aged PENDING cash order with a real, in-polygon address.

    Cash (not CLICK) so the task's `payment_method.value == "cash"` branch
    fires without needing a COMPLETED payment, and with an address so the
    CONFIRMED transition's `assert_order_address_for_status` passes and
    `create_delivery` would genuinely succeed if the gate let it through.
    """
    address = UserAddress(
        user_id=user.id,
        title="Home",
        full_address="Amir Temur 1",
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()

    order = Order(
        user_id=user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("25000.00"),
        total_amount=Decimal("25000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        delivery_date=delivery_date,
        order_source="web",
    )
    db.session.add(order)
    db.session.flush()
    order.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=user.id,
            amount=order.total_amount,
            payment_method=PaymentMethod.CASH,
            status=PaymentStatus.PENDING,
        )
    )
    db.session.commit()
    return order


def test_scheduled_order_auto_confirms_without_logging_an_error(app, db, sample_user):
    """A future-dated order is confirmed, correctly gets NO delivery row, and
    must produce NO ERROR line — its missing delivery is the expected state."""
    with app.app_context():
        order = _aged_cash_order(db, sample_user, delivery_date=date.today() + timedelta(days=3))
        order_id = order.id

        with patch("business_app.tasks.order_tasks.logger.error") as log_error:
            result = auto_confirm_pending_orders()

        assert result["confirmed_count"] == 1
        assert result["failed_count"] == 0
        db.session.refresh(order)
        assert order.status is OrderStatus.CONFIRMED
        assert Delivery.query.filter_by(order_id=order_id).first() is None
        assert not [m for m in _error_messages(log_error) if _MISSING_DELIVERY_ERROR in m]


def test_a_genuinely_missing_delivery_still_logs_an_error(app, db, sample_user):
    """The paired negative: the backstop must still fire for an order whose
    delivery really did fail to appear. Without this, 'fixing' the false alarm
    by deleting the check entirely would ship green.

    `ensure_delivery_if_due` is stubbed to a silent no-op — exactly the failure
    mode the backstop exists for — on an UNDATED order, which is due
    immediately and therefore never awaiting release.
    """
    with app.app_context():
        order = _aged_cash_order(db, sample_user, delivery_date=None)
        order_id = order.id

        with patch(
            "business_app.services.order_schedule_service.OrderScheduleService.ensure_delivery_if_due",
            return_value=None,
        ), patch("business_app.tasks.order_tasks.logger.error") as log_error:
            result = auto_confirm_pending_orders()

        assert result["confirmed_count"] == 1
        assert Delivery.query.filter_by(order_id=order_id).first() is None
        assert [m for m in _error_messages(log_error) if _MISSING_DELIVERY_ERROR in m]
