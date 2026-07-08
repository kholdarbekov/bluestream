"""Nothing dispatched after the money commit may abort the chain; dispatches leave a marker."""

from datetime import UTC, datetime, timedelta

import pytest

from business_app import db
from business_app.models.user import UserAddress
from shared.enums import PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


@pytest.fixture
def order_with_address(db, sample_order, sample_user):
    """Attach an in-range delivery address so ``create_delivery`` clears its
    address/range guards. Local to this module because the shared fixture lives
    under ``tests/integration`` and is not visible here."""
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Yunusobod, Tashkent",
        street_address="Yunusobod street 1",
        latitude=41.3111,
        longitude=69.2797,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    sample_order.delivery_address_id = address.id
    sample_order.delivery_date = datetime.now(UTC) + timedelta(days=1)
    db.session.commit()
    return sample_order


def test_dispatch_payment_confirmation_sets_marker_and_survives_broker_failure(
    app, db, sample_order, monkeypatch
):
    payment = _seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()

    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks

    enqueued = []
    monkeypatch.setattr(
        notification_tasks.send_payment_confirmation_task, "delay", lambda pid: enqueued.append(pid)
    )
    svc = PaymentService()
    assert svc.dispatch_payment_confirmation(payment) is True
    assert enqueued == [payment.id]
    marker = (payment.provider_data or {}).get("post_payment", {}).get("confirmation_enqueued_at")
    assert marker


def test_dispatch_survives_broker_failure(app, db, sample_order, monkeypatch):
    payment = _seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()

    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks

    def boom(pid):
        raise RuntimeError("broker down")

    monkeypatch.setattr(notification_tasks.send_payment_confirmation_task, "delay", boom)
    svc = PaymentService()
    assert svc.dispatch_payment_confirmation(payment) is False
    assert (payment.provider_data or {}).get("post_payment", {}).get("confirmation_enqueued_at") is None


def test_create_delivery_survives_auto_assign_broker_failure(app, db, order_with_address, monkeypatch):
    from business_app.services.delivery_service import DeliveryService

    svc = DeliveryService()
    monkeypatch.setattr(
        DeliveryService,
        "_schedule_delivery_assignment",
        lambda self, delivery_id: (_ for _ in ()).throw(RuntimeError("broker down")),
    )
    delivery = svc.create_delivery(order_with_address.id)
    assert delivery.id is not None  # creation committed despite the enqueue failure
