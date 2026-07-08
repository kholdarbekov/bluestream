"""POST /payments/create: race + revival semantics (one payment row per order, always)."""

import pytest
from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.payment import Payment
from shared.enums import PaymentMethod, PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


# Auth: the root conftest provides `auth_token`/`auth_headers` fixtures minting a
# JWT for `sample_user` (tests/conftest.py:381-404). `order_with_address` builds on
# `sample_order`, which belongs to `sample_user`, so `auth_headers` matches the
# order owner. Use a FRESH test client per test (session-scoped clients leak JWT
# cookies — known suite gotcha).


def test_integrity_error_race_returns_existing_payment(
    matrix_app, order_with_address, auth_headers, monkeypatch
):
    """Simulate the losing racer: create_payment raises IntegrityError; the route
    must recover by reusing the committed winner row instead of 500."""
    order = order_with_address
    client = matrix_app.test_client()
    winner = _seed_click_payment(db, order)

    from business_app.services.payment_service import PaymentService

    original = PaymentService.create_payment
    calls = {"n": 0}

    def racing_create(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT INTO payments", {}, Exception("uq_payments_order_id"))
        return original(self, *args, **kwargs)

    # Force the route past its reuse pre-check into create_payment.
    winner.status = PaymentStatus.CANCELLED
    db.session.commit()
    monkeypatch.setattr(PaymentService, "create_payment", racing_create)

    resp = client.post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "click"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()["data"]
    assert body["payment"]["id"] == winner.id
    assert body["payment_link"]["payment_url"]


def test_cross_method_race_reuses_winner_row_with_requested_method(
    matrix_app, order_with_address, auth_headers, monkeypatch
):
    """uq_payments_order_id is on order_id alone, so the racing winner can hold a
    DIFFERENT payment method. A losing click request must not inherit the winner's
    CASH method (nor answer 'cash created' while the row stays CLICK): the retry
    must UPDATE-reuse the winner row onto the requested method via the service SSOT."""
    order = order_with_address
    client = matrix_app.test_client()
    winner = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CASH,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.CANCELLED,
        payment_id="PAY_CASH_WINNER",
        provider_data={},
    )
    db.session.add(winner)
    db.session.commit()
    winner_id = winner.id

    from business_app.services.payment_service import PaymentService

    original = PaymentService.create_payment
    calls = {"n": 0}

    def racing_create(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("INSERT INTO payments", {}, Exception("uq_payments_order_id"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PaymentService, "create_payment", racing_create)

    resp = client.post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "click"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.get_json()["data"]
    assert body["payment"]["id"] == winner_id
    method_in_response = body["payment"].get("paymentMethod", body["payment"].get("payment_method"))
    assert method_in_response == "click"
    assert body["payment_link"]["payment_url"]

    db.session.expire_all()
    refreshed = Payment.query.filter_by(order_id=order.id).one()  # still exactly one row
    assert refreshed.id == winner_id
    assert refreshed.payment_method == PaymentMethod.CLICK
    assert refreshed.status == PaymentStatus.PENDING


def test_cancelled_payment_retry_revives_same_row(matrix_app, order_with_address, auth_headers):
    order = order_with_address
    client = matrix_app.test_client()
    payment = _seed_click_payment(db, order)
    payment.status = PaymentStatus.CANCELLED
    db.session.commit()

    resp = client.post(
        "/api/v1/payments/create",
        json={"order_id": order.id, "payment_method": "click"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    db.session.expire_all()
    refreshed = Payment.query.filter_by(order_id=order.id).one()  # still exactly one row
    assert refreshed.id == payment.id
    assert refreshed.status == PaymentStatus.PENDING
