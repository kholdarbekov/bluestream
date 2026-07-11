"""Pin tests for cancel_abandoned_orders.

This task never ran: it wasn't scheduled, and its filter raised
NotImplementedError because `Order.payment` is a relationship, not a column.
The blanket `except Exception` turned the crash into {"error": ...}.
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.tasks.order_tasks import cancel_abandoned_orders
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import ``business_app.tasks.celery_app`` exactly once, safely.

    Its module-level ``celery = make_celery()`` calls the bare ``create_app()``
    (no config override). Under FLASK_ENV=testing (the whole suite's env) that
    trips a pre-existing, unrelated ordering bug in ``create_app()``:
    ``TestingConfig.init_app()`` runs ``db.create_all()`` before
    ``db.init_app(app)`` has bound ``db`` to the freshly constructed Flask app,
    raising RuntimeError. Fixing that is out of scope for this task, so instead
    we monkeypatch ``business_app.create_app`` for the single call
    ``make_celery()`` makes at import time, handing back the already-initialized
    pytest ``app`` fixture instead of letting it build a broken one from scratch.
    Mirrors the identical fixture in test_celery_task_wiring_cleanup.py.
    """
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def _aged_order(db, user, *, hours, status=OrderStatus.PENDING, payment_status=None):
    order = Order(
        user_id=user.id,
        status=status,
        subtotal=Decimal("25000.00"),
        total_amount=Decimal("25000.00"),
        payment_method=PaymentMethod.CLICK,
    )
    db.session.add(order)
    db.session.flush()
    order.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    if payment_status is not None:
        db.session.add(
            Payment(
                order_id=order.id,
                user_id=user.id,
                amount=order.total_amount,
                payment_method=PaymentMethod.CLICK,
                status=payment_status,
            )
        )
    db.session.commit()
    return order


def test_query_builds_at_all(app, db, sample_user):
    """Regression: Order.payment.is_(None) raised NotImplementedError."""
    with app.app_context():
        result = cancel_abandoned_orders()
        assert "error" not in result, f"task still crashes: {result.get('error')}"
        assert "cancelled_count" in result


def test_cancels_old_pending_order_with_no_payment(app, db, sample_user):
    with app.app_context():
        order = _aged_order(db, sample_user, hours=30)
        cancel_abandoned_orders()
        db.session.refresh(order)
        assert order.status is OrderStatus.CANCELLED


def test_cancels_old_pending_order_with_pending_payment(app, db, sample_user):
    with app.app_context():
        order = _aged_order(db, sample_user, hours=30, payment_status=PaymentStatus.PENDING)
        cancel_abandoned_orders()
        db.session.refresh(order)
        assert order.status is OrderStatus.CANCELLED


def test_spares_paid_order(app, db, sample_user):
    with app.app_context():
        order = _aged_order(db, sample_user, hours=30, payment_status=PaymentStatus.COMPLETED)
        cancel_abandoned_orders()
        db.session.refresh(order)
        assert order.status is OrderStatus.PENDING


def test_spares_recent_order(app, db, sample_user):
    with app.app_context():
        order = _aged_order(db, sample_user, hours=2)
        cancel_abandoned_orders()
        db.session.refresh(order)
        assert order.status is OrderStatus.PENDING


def test_is_scheduled_in_beat(celery_app_module):
    celery = celery_app_module.celery

    tasks = {entry["task"] for entry in celery.conf.beat_schedule.values()}
    assert "business_app.tasks.order_tasks.cancel_abandoned_orders" in tasks
