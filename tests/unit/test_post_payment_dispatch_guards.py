"""Nothing dispatched after the money commit may abort the chain; dispatches leave a marker."""

from datetime import UTC, datetime, timedelta

import pytest

from business_app import db
from business_app.models.user import UserAddress
from shared.enums import PaymentStatus

from tests.integration.payment_builders import seed_click_payment


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


def test_dispatch_payment_confirmation_sets_marker_and_publishes_only_after_the_commit(
    app, db, sample_order, monkeypatch
):
    """The confirmation is decided and marked in the caller's transaction, and published by its commit (Task Hfix:
    nothing customer-facing leaves before the money commits)."""
    payment = seed_click_payment(db, sample_order)
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
    marker = (payment.provider_data or {}).get("post_payment", {}).get("confirmation_enqueued_at")
    assert marker
    assert enqueued == []
    db.session.commit()
    assert enqueued == [payment.id]


def test_a_broker_failure_after_the_commit_leaves_the_payment_unmarked_for_the_sweep(
    app, db, sample_order, monkeypatch
):
    """The publish fails after the money committed: the committing caller never sees it, and the payment is left
    unmarked so the side-effect sweep sends the confirmation later."""
    payment = seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()

    from business_app.models.payment import Payment
    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks

    def boom(pid):
        raise RuntimeError("broker down")

    monkeypatch.setattr(notification_tasks.send_payment_confirmation_task, "delay", boom)
    svc = PaymentService()
    assert svc.dispatch_payment_confirmation(payment) is True
    db.session.commit()

    db.session.expire_all()
    assert PaymentService.payment_confirmation_dispatched(db.session.get(Payment, payment.id)) is False


def test_a_dispatch_that_raises_parks_nothing_and_leaves_the_payment_unmarked(app, db, sample_order, monkeypatch):
    payment = seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()

    from business_app.services.notification_service import NotificationService
    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks

    def unreadable(user, **kwargs):
        raise RuntimeError("the customer row could not be read")

    enqueued = []
    monkeypatch.setattr(NotificationService, "_resolve_telegram_else_email", staticmethod(unreadable))
    monkeypatch.setattr(
        notification_tasks.send_payment_confirmation_task, "delay", lambda pid: enqueued.append(pid)
    )
    svc = PaymentService()
    assert svc.dispatch_payment_confirmation(payment) is False
    assert PaymentService.payment_confirmation_dispatched(payment) is False
    db.session.commit()
    assert enqueued == []


def _marked_at(payment):
    return ((payment.provider_data or {}).get("post_payment") or {}).get("confirmation_enqueued_at")


def test_a_failed_publish_never_clears_a_mark_another_dispatch_wrote_since(app, db, sample_order, monkeypatch):
    """The unmark clears only the mark its own transaction wrote: a concurrent dispatcher that marked (and sent) the
    payment between the commit and the failed publish keeps its mark, so the sweep does not send a second one."""
    from sqlalchemy.orm import Session

    from business_app.models.payment import Payment
    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks

    payment = seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()
    theirs = "2026-11-02T09:00:00+00:00"

    def another_dispatcher_marks_it_then_the_broker_fails(pid):
        with Session(bind=db.engine) as other:
            row = other.get(Payment, pid)
            row.provider_data = {**(row.provider_data or {}), "post_payment": {"confirmation_enqueued_at": theirs}}
            other.commit()
        raise RuntimeError("broker down")

    monkeypatch.setattr(
        notification_tasks.send_payment_confirmation_task, "delay", another_dispatcher_marks_it_then_the_broker_fails
    )
    assert PaymentService().dispatch_payment_confirmation(payment) is True
    ours = _marked_at(payment)
    db.session.commit()

    db.session.expire_all()
    assert ours != theirs
    assert _marked_at(db.session.get(Payment, payment.id)) == theirs


@pytest.mark.parametrize("rail, swept", [("click", True), ("payme", False), ("business_account", False)])
def test_a_failed_publish_on_a_rail_no_sweep_covers_is_logged_at_error(
    app, db, sample_order, monkeypatch, caplog, rail, swept
):
    """The side-effect sweep re-sends Click and card confirmations only. A failed Payme or business-account publish is
    unmarked too, and logged at ERROR with its payment and rail, so a person can follow it up."""
    import logging

    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks
    from shared.enums import PaymentMethod

    payment = seed_click_payment(db, sample_order)
    payment.payment_method = PaymentMethod(rail)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()

    def boom(pid):
        raise RuntimeError("broker down")

    monkeypatch.setattr(notification_tasks.send_payment_confirmation_task, "delay", boom)
    assert PaymentService().dispatch_payment_confirmation(payment) is True
    # The app's logging config stops `business_app.*` records short of the root logger, where caplog listens.
    dispatch_logger = logging.getLogger("business_app.utils.payment_confirmation_dispatch")
    dispatch_logger.addHandler(caplog.handler)
    try:
        db.session.commit()
    finally:
        dispatch_logger.removeHandler(caplog.handler)

    unswept = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.ERROR and "no sweep re-sends that rail" in record.getMessage()
    ]
    expected = (
        []
        if swept
        else [f"Payment confirmation for payment {payment.id} ({rail}) was not sent, and no sweep re-sends that rail"]
    )
    assert unswept == expected


def test_a_savepoint_release_publishes_nothing_and_the_outer_commit_publishes_once(
    app, db, sample_order, monkeypatch
):
    """SQLAlchemy dispatches after_commit for a SAVEPOINT release too (2.0.43); the confirmation waits for the outer
    commit. A SAVEPOINT rollback does not drop it either: only the outer transaction decides."""
    from business_app.services.payment_service import PaymentService
    from business_app.tasks import notification_tasks

    payment = seed_click_payment(db, sample_order)
    payment.status = PaymentStatus.COMPLETED
    db.session.commit()
    enqueued = []
    monkeypatch.setattr(
        notification_tasks.send_payment_confirmation_task, "delay", lambda pid: enqueued.append(pid)
    )

    assert PaymentService().dispatch_payment_confirmation(payment) is True
    with db.session.begin_nested():
        payment.failure_reason = None
    assert enqueued == []
    try:
        with db.session.begin_nested():
            payment.description = "a step that fails inside its savepoint"
            raise RuntimeError("the step failed")
    except RuntimeError:
        pass
    assert enqueued == []
    db.session.commit()

    assert enqueued == [payment.id]


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
