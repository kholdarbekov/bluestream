"""Sweep: COMPLETED Click payments with lost fiscalization or confirmation get re-driven.

Fixture style mirrors Task 7's ``tests/unit/test_reconcile_positive_evidence.py``
(``db`` + ``sample_order`` + ``_seed_click_payment``); no ``db_session`` fixture
exists in this project's conftest.
"""

from datetime import datetime, timedelta, timezone

from business_app import db
from shared.enums import PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


def _completed_payment(db, order, *, paid_minutes_ago=60):
    payment = _seed_click_payment(db, order)
    payment.status = PaymentStatus.COMPLETED
    payment.paid_at = datetime.now(timezone.utc) - timedelta(minutes=paid_minutes_ago)
    db.session.commit()
    return payment


def test_sweep_requeues_missing_fiscalization_and_confirmation(db, sample_order, monkeypatch):
    payment = _completed_payment(db, sample_order)
    assert getattr(payment, "fiscalization", None) is None

    queued_fisc = []
    dispatched = []
    from business_app.services.payment_service import PaymentService

    monkeypatch.setattr(
        PaymentService, "queue_click_fiscalization", lambda self, pid: queued_fisc.append(pid)
    )
    monkeypatch.setattr(
        PaymentService,
        "dispatch_payment_confirmation",
        lambda self, p: dispatched.append(p.id) or True,
    )

    from business_app.tasks.payment_tasks import reconcile_completed_payment_side_effects

    counts = reconcile_completed_payment_side_effects.run()
    assert queued_fisc == [payment.id]
    assert dispatched == [payment.id]
    assert counts["fiscalization_requeued"] == 1
    assert counts["confirmation_redispatched"] == 1


def test_sweep_skips_fully_processed_payment(db, sample_order, monkeypatch):
    payment = _completed_payment(db, sample_order)
    provider_data = dict(payment.provider_data or {})
    provider_data["post_payment"] = {"confirmation_enqueued_at": "2026-07-08T00:00:00+00:00"}
    payment.provider_data = provider_data
    db.session.commit()

    from business_app.services.payment_service import PaymentService

    queued = []
    monkeypatch.setattr(PaymentService, "queue_click_fiscalization", lambda self, pid: queued.append(pid))
    dispatched = []
    monkeypatch.setattr(
        PaymentService, "dispatch_payment_confirmation", lambda self, p: dispatched.append(p.id) or True
    )

    # Give the payment a completed fiscalization row using the project's model.
    # `PaymentFiscalizationService._get_payment` joinedload's `fiscalization` as
    # None before the row exists, so `payment.fiscalization` stays stale on this
    # Python object; query the row directly instead.
    from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
    from business_app.models.payment import PaymentFiscalization

    PaymentFiscalizationService().queue_click_fiscalization(payment.id)
    fisc = PaymentFiscalization.query.filter_by(payment_id=payment.id).first()
    from shared.enums import FiscalizationStatus

    fisc.status = FiscalizationStatus.COMPLETED
    db.session.commit()

    from business_app.tasks.payment_tasks import reconcile_completed_payment_side_effects

    counts = reconcile_completed_payment_side_effects.run()
    assert queued == []
    assert dispatched == []
    assert counts["fiscalization_requeued"] == 0
    assert counts["confirmation_redispatched"] == 0


def test_sweep_ignores_old_payments(db, sample_order, monkeypatch):
    payment = _completed_payment(db, sample_order, paid_minutes_ago=60 * 24 * 8)  # 8 days
    from business_app.services.payment_service import PaymentService

    queued = []
    monkeypatch.setattr(PaymentService, "queue_click_fiscalization", lambda self, pid: queued.append(pid))
    from business_app.tasks.payment_tasks import reconcile_completed_payment_side_effects

    reconcile_completed_payment_side_effects.run()
    assert queued == []
