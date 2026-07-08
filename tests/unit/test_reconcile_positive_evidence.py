"""reconcile_pending_payments: cancel only on affirmative gateway evidence.

Positive-evidence contract (Task 7):
- gateway-affirmative cancelled/failed => cancel (any age).
- ``not_found`` past timeout => cancel (Click affirmatively does not recognize it).
- unknown/ambiguous status past timeout => LEAVE PENDING + one-shot audit alert.
- every reconcile-cancel notifies the customer via ``payment_autocancel_retry``.

Fixture style mirrors Task 4's ``tests/unit/test_reverse_click_payment_task.py``
(``db`` + ``sample_order`` + ``_seed_click_payment``); the module-level ``db``
import is the same singleton the ``db`` fixture yields.
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app import db
from shared.enums import PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


def _age_payment(payment, minutes):
    payment.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.session.commit()


@pytest.fixture
def notified(monkeypatch):
    calls = []
    from business_app.services.notification_service import NotificationService

    monkeypatch.setattr(
        NotificationService,
        "send_notification",
        lambda self, user_id, key, template_data=None, **kw: calls.append((user_id, key, template_data)),
    )
    return calls


def _run_reconcile_with_status(monkeypatch, status_payload):
    from business_app.services.payment_service import PaymentService

    monkeypatch.setattr(PaymentService, "check_payment_status", lambda self, payment_id: status_payload)
    from business_app.tasks.payment_tasks import reconcile_pending_payments

    return reconcile_pending_payments.run()


@pytest.mark.unit
def test_unknown_status_past_timeout_leaves_pending_and_alerts_once(
    db, sample_order, monkeypatch, notified
):
    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=120)

    events = []
    from business_app.utils import audit_logger as audit_module

    monkeypatch.setattr(audit_module.audit_logger, "log_event", lambda **kw: events.append(kw))

    _run_reconcile_with_status(monkeypatch, {"status": "pending"})
    db.session.expire_all()
    assert db.session.get(type(payment), payment.id).status == PaymentStatus.PENDING
    review_events = [e for e in events if e.get("action") == "payment_reconcile_needs_review"]
    assert len(review_events) == 1
    assert notified == []

    # Second run: flag set -> no second alert.
    events.clear()
    _run_reconcile_with_status(monkeypatch, {"status": "pending"})
    assert [e for e in events if e.get("action") == "payment_reconcile_needs_review"] == []


@pytest.mark.unit
def test_not_found_past_timeout_cancels_and_notifies_customer(
    db, sample_order, monkeypatch, notified
):
    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=120)

    _run_reconcile_with_status(monkeypatch, {"status": "not_found", "not_found": True})
    db.session.expire_all()
    refreshed = db.session.get(type(payment), payment.id)
    assert refreshed.status == PaymentStatus.CANCELLED
    assert notified == [
        (payment.user_id, "payment_autocancel_retry", {"order_number": sample_order.order_number})
    ]


@pytest.mark.unit
def test_gateway_cancelled_cancels_and_notifies(db, sample_order, monkeypatch, notified):
    payment = _seed_click_payment(db, sample_order)
    _age_payment(payment, minutes=30)  # before timeout: gateway evidence alone suffices

    _run_reconcile_with_status(monkeypatch, {"status": "cancelled"})
    db.session.expire_all()
    assert db.session.get(type(payment), payment.id).status == PaymentStatus.CANCELLED
    assert len(notified) == 1
    assert notified[0][1] == "payment_autocancel_retry"


# --------------------------------------------------------------------------- #
# H1 fix: the not_found signal must survive the REAL
# update_payment_status -> PaymentService.check_payment_status bridge.
# These two tests do NOT monkeypatch PaymentService.check_payment_status;
# they patch the PROVIDER level so the whole service path executes.
# --------------------------------------------------------------------------- #

def _run_reconcile_with_provider_status(monkeypatch, provider_result):
    """Patch ClickPaymentProviderService.check_payment_status (provider level)."""
    from business_app.services.click_payment_provider_service import (
        ClickPaymentProviderService,
    )

    if isinstance(provider_result, Exception):

        def _provider_check(self, payment):
            raise provider_result

    else:

        def _provider_check(self, payment):
            return provider_result

    monkeypatch.setattr(ClickPaymentProviderService, "check_payment_status", _provider_check)
    from business_app.tasks.payment_tasks import reconcile_pending_payments

    return reconcile_pending_payments.run()


@pytest.mark.unit
def test_provider_not_found_bridges_real_service_check_and_cancels(
    db, sample_order, monkeypatch, notified
):
    """Provider-level not_found on a BLIND payment past timeout -> CANCELLED.

    Exercises the real update_payment_status -> get_payment_status bridge:
    before the H1 fix update_payment_status discarded the gateway status, the
    service returned "pending" and reconcile's not_found branch never fired.
    """
    from tests.unit.test_click_status_by_mti import _blind_payment

    payment = _blind_payment(db, sample_order)
    _age_payment(payment, minutes=120)

    _run_reconcile_with_provider_status(
        monkeypatch,
        {"status": "not_found", "not_found": True, "provider_transaction_id": None, "raw": None},
    )
    db.session.expire_all()
    refreshed = db.session.get(type(payment), payment.id)
    assert refreshed.status == PaymentStatus.CANCELLED
    assert notified == [
        (payment.user_id, "payment_autocancel_retry", {"order_number": sample_order.order_number})
    ]


@pytest.mark.unit
def test_provider_transport_error_leaves_pending_not_cancelled(
    db, sample_order, monkeypatch, notified
):
    """Transport errors must NOT masquerade as not_found.

    Provider check raises PaymentError -> payment stays PENDING (never
    cancelled), and past timeout it gets the one-shot needs-review audit
    flag instead of a cancel.
    """
    from business_app.utils.exceptions import PaymentError
    from tests.unit.test_click_status_by_mti import _blind_payment

    payment = _blind_payment(db, sample_order)
    _age_payment(payment, minutes=120)

    events = []
    from business_app.utils import audit_logger as audit_module

    monkeypatch.setattr(audit_module.audit_logger, "log_event", lambda **kw: events.append(kw))

    _run_reconcile_with_provider_status(monkeypatch, PaymentError("boom"))
    db.session.expire_all()
    refreshed = db.session.get(type(payment), payment.id)
    assert refreshed.status == PaymentStatus.PENDING
    review_events = [e for e in events if e.get("action") == "payment_reconcile_needs_review"]
    assert len(review_events) == 1
    assert notified == []
