"""status_by_mti fallback: discovers the Click payment id by order number + date."""

from datetime import datetime, timedelta, timezone

import pytest

from business_app import db
from business_app.utils.exceptions import PaymentError
from shared.enums import PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


def _service(app):
    from business_app.services.click_payment_provider_service import ClickPaymentProviderService

    svc = ClickPaymentProviderService()
    svc.test_mode = False
    return svc


def _blind_payment(db, order):
    """A payment with NO Click ids anywhere (the crash-window state)."""
    payment = _seed_click_payment(db, order, click_paydoc_id="")
    provider_data = dict(payment.provider_data or {})
    provider_data["click"] = {}
    payment.provider_data = provider_data
    payment.provider_transaction_id = None
    db.session.commit()
    return payment


def test_mti_discovers_id_persists_and_delegates(app, db, sample_order, monkeypatch):
    payment = _blind_payment(db, sample_order)
    svc = _service(app)
    requests = []

    def fake_merchant_request(payload=None, *, configured_url=None, fallback_path, method="POST",
                              endpoint_label=None, expect_error_code=True):
        requests.append({"path": fallback_path, "label": endpoint_label, "method": method})
        if endpoint_label == "payment_status_by_mti":
            return {"error_code": 0, "payment_id": 555123, "merchant_trans_id": sample_order.order_number}
        if endpoint_label == "payment_status":
            return {"payment_status": 1, "payment_id": 555123}
        raise AssertionError(f"unexpected endpoint {endpoint_label}")

    svc.merchant_request = fake_merchant_request

    result = svc.check_payment_status(payment)
    # Fallback fired: first by_mti, then the normal status endpoint with the discovered id.
    assert [r["label"] for r in requests] == ["payment_status_by_mti", "payment_status"]
    assert f"/{sample_order.order_number}/" in requests[0]["path"]
    assert result["provider_transaction_id"] == "555123"
    db.session.expire_all()
    assert (payment.provider_data or {})["click"]["click_paydoc_id"] == "555123"


def test_mti_not_found_on_all_dates_returns_not_found(app, db, sample_order, monkeypatch):
    payment = _blind_payment(db, sample_order)
    # Make the payment straddle-proof: created yesterday so BOTH dates are queried.
    payment.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.session.commit()
    svc = _service(app)
    paths = []

    def fake_merchant_request(payload=None, *, configured_url=None, fallback_path, method="POST",
                              endpoint_label=None, expect_error_code=True):
        paths.append(fallback_path)
        return {"error_code": -16, "error_note": "Payment not found"}

    svc.merchant_request = fake_merchant_request
    result = svc.check_payment_status(payment)
    assert result == {"status": "not_found", "not_found": True, "provider_transaction_id": None, "raw": None}
    assert len(paths) == 2  # created-date then today


def test_mti_transport_error_propagates(app, db, sample_order, monkeypatch):
    payment = _blind_payment(db, sample_order)
    svc = _service(app)

    def fake_merchant_request(payload=None, **kwargs):
        raise PaymentError("Click merchant API HTTP error (GET ...): 502")

    svc.merchant_request = fake_merchant_request
    with pytest.raises(PaymentError):
        svc.check_payment_status(payment)
