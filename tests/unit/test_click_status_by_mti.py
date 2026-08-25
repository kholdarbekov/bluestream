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
    assert result == {
        "status": "not_found",
        "not_found": True,
        "ambiguous": False,
        "provider_transaction_id": None,
        "raw": None,
    }
    assert len(paths) == 2  # created-date then today


def test_mti_transport_error_propagates(app, db, sample_order, monkeypatch):
    payment = _blind_payment(db, sample_order)
    svc = _service(app)

    def fake_merchant_request(payload=None, **kwargs):
        raise PaymentError("Click merchant API HTTP error (GET ...): 502")

    svc.merchant_request = fake_merchant_request
    with pytest.raises(PaymentError):
        svc.check_payment_status(payment)


# ---------------------------------------------------------------------------
# Ambiguity is not absence.
#
# Prod incident TG_000413_26: `status_by_mti` collapsed EVERY non-zero Click
# error_code into "not found for this date", then reported the exhausted date
# loop as an affirmative `{"status": "not_found", "not_found": True}` — the one
# value `reconcile_pending_payments` accepts as licence to auto-cancel.
# Click answered `-12 "Неверные данные поставщика"` (a request/credential-level
# rejection) on 279 of 279 calls in the 30 days around the incident, so the
# "affirmative evidence only" hardening was running on manufactured evidence.
#
# Only a documented absence code (-16) may mean absence. Everything else is
# ambiguous and must leave the payment PENDING for a human.
# ---------------------------------------------------------------------------


def _mti_only(svc, response):
    """Stub merchant_request so status_by_mti returns `response` for every date."""
    calls = []

    def fake_merchant_request(payload=None, *, configured_url=None, fallback_path, method="POST",
                              endpoint_label=None, expect_error_code=True):
        calls.append(endpoint_label)
        if endpoint_label == "payment_status_by_mti":
            return response
        raise AssertionError(f"must not reach {endpoint_label} on an ambiguous answer")

    svc.merchant_request = fake_merchant_request
    return calls


@pytest.mark.parametrize(
    "response,label",
    [
        ({"error_code": -12, "error_note": "Неверные данные поставщика"}, "supplier-data rejection"),
        ({"error_code": -1, "error_note": "SIGN CHECK FAILED"}, "signature rejection"),
        ({"error_code": 0, "payment_id": None}, "success with no payment_id"),
        ({"error_code": 0, "payment_id": ""}, "success with empty payment_id"),
        ({"error_code": 0, "payment_id": "not-a-number"}, "non-numeric payment_id"),
    ],
)
def test_ambiguous_gateway_answer_is_never_reported_as_not_found(app, db, sample_order, response, label):
    payment = _blind_payment(db, sample_order)
    svc = _service(app)
    _mti_only(svc, response)

    result = svc.check_payment_status_by_mti(payment)

    assert result["not_found"] is False, f"{label} must not be treated as affirmative absence"
    assert result.get("ambiguous") is True, f"{label} must be flagged ambiguous"
    assert result["status"] != "not_found"


def test_documented_absence_code_still_reports_not_found(app, db, sample_order):
    """-16 is Click's real "Payment not found". It remains affirmative evidence."""
    payment = _blind_payment(db, sample_order)
    svc = _service(app)
    _mti_only(svc, {"error_code": -16, "error_note": "Payment not found"})

    result = svc.check_payment_status_by_mti(payment)

    assert result["not_found"] is True
    assert result["status"] == "not_found"


def test_ambiguous_answer_does_not_auto_cancel_past_timeout(app, db, sample_order, monkeypatch):
    """A -12 past the old timeout window must leave the payment PENDING.

    Belt and braces: Phase 4D removed the auto-cancel entirely, so nothing in
    reconcile can cancel any more. This still pins the SENSOR half — an ambiguous
    gateway answer must never be dressed up as evidence — so that re-introducing
    a cancel elsewhere cannot be fed a lie.
    """
    from datetime import datetime as _dt

    payment = _blind_payment(db, sample_order)
    payment.created_at = _dt.now(timezone.utc) - timedelta(hours=3)
    payment.status = PaymentStatus.PENDING
    db.session.commit()

    from business_app.services.payment_service import PaymentService

    def fake_check(self, payment_id):
        return {"status": "unknown", "not_found": False, "ambiguous": True,
                "provider_transaction_id": None, "raw": None}

    monkeypatch.setattr(PaymentService, "check_payment_status", fake_check)

    from business_app.tasks.payment_tasks import reconcile_pending_payments

    counts = reconcile_pending_payments.run()

    db.session.expire_all()
    assert payment.status == PaymentStatus.PENDING, "an ambiguous answer must never cancel"
    assert counts["cancelled"] == 0
    assert counts["unchanged"] >= 1
