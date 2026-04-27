"""Payment-protocol integration matrix (TST-001).

Provider × outcome × flow matrix for Payme + Click + cash + loyalty webhooks
and the post-payment lifecycle. Drives:

- Webhook state machine: each (provider, outcome, flow) cell asserts the
  Payment + Order + PaymentTransaction state at rest.
- Concurrent-webhook idempotency: replay coalesces to one effect.
- Refund reversals mid-flow: full + partial.
- Provider-timeout circuit: Click outbound raises ProviderUnavailableError;
  upstream surface preserved.
- Reconciliation: stranded PENDING flips on next reconcile run.

Fiscalization (OFD) is out of scope for this matrix — see TST-011 for the
companion follow-up. Tests patch ``queue_click_fiscalization`` to a no-op so
fiscalization paths don't sneak in via post-payment hooks.

All outbound provider calls go through fakes in
``tests/integration/fake_gateways.py`` with strict scripting (unscripted
calls raise ``UnscriptedGatewayCall``). No real Click/Payme sandbox calls.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.payment import Payment, PaymentTransaction
from business_app.models.user import UserAddress
from business_app.utils.constants import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    PaymeState,
)
from business_app.utils.exceptions import ProviderUnavailableError
from tests.integration.fake_gateways import (
    FakeClickMerchant,
    FakePayme,
    TEST_CLICK_SHOP_SECRET_KEY,
    TEST_PAYME_SECRET_KEY,
    UnscriptedGatewayCall,
    apply_test_provider_secrets,
    make_click_webhook_form,
    make_payme_webhook_body,
)


WEBHOOK_PATH = '/api/v1/payments/webhook/{provider}'


# --------------------------------------------------------------------------- #
# Module-level fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def matrix_app(app):
    """Apply test provider secrets and return the same app fixture."""
    apply_test_provider_secrets(app)
    # Payme uses the *_with_billing variants for signature verification on
    # webhook receipts; mirror the primary key.
    app.config['PAYME_MERCHANT_ID_WITH_BILLING'] = app.config['PAYME_MERCHANT_ID']
    app.config['PAYME_SECRET_KEY_WITH_BILLING'] = app.config['PAYME_SECRET_KEY']
    return app


@pytest.fixture
def matrix_client(matrix_app):
    return matrix_app.test_client()


@pytest.fixture
def payme_basic_auth_header():
    """Build the Basic-auth header Payme webhook signature verification expects."""
    creds = f"Paycom:{TEST_PAYME_SECRET_KEY}".encode('utf-8')
    return {'Authorization': 'Basic ' + base64.b64encode(creds).decode('ascii')}


@pytest.fixture
def no_fiscalization(monkeypatch):
    """Stub out post-payment fiscalization triggers (TST-011 territory).

    ``_handle_successful_payment`` calls ``queue_click_fiscalization`` for
    Click + Card payments after a successful webhook. Fiscalization has its
    own retry/idempotency semantics that the OFD matrix (TST-011) covers.
    Patch at the PaymentService level so any service instance picks it up.
    """
    from business_app.services.payment_service import PaymentService
    monkeypatch.setattr(
        PaymentService,
        'queue_click_fiscalization',
        lambda self, payment_id: None,
        raising=True,
    )


@pytest.fixture
def fake_click_merchant(monkeypatch, matrix_app):
    """Strict fake patched onto ``request_with_retry`` for Click outbound.

    ``CLICK_TEST_MODE`` is forced to False so ``merchant_request`` actually
    enters the request path that calls the patched function (test_mode mode
    short-circuits with a synthetic success).
    """
    matrix_app.config['CLICK_TEST_MODE'] = False
    fake = FakeClickMerchant()
    monkeypatch.setattr(
        'business_app.services.click_payment_provider_service.request_with_retry',
        fake,
        raising=True,
    )
    yield fake


@pytest.fixture
def sample_address(db, sample_user):
    """Default delivery address for sample_user.

    ARCH-006 enforces ``delivery_address_id IS NOT NULL`` on the
    PENDING → CONFIRMED transition. The shared ``sample_order`` fixture
    intentionally creates orders without an address (covers pre-CONFIRMED
    states), so this finding shadows it for tests that drive an order to
    paid state.
    """
    address = UserAddress(
        user_id=sample_user.id,
        title='Home',
        full_address='123 Test Street, Tashkent',
        latitude=41.2995,
        longitude=69.2401,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def order_with_address(db, sample_order, sample_address):
    """Attach the test address to ``sample_order`` so paid-state
    transitions clear the ARCH-006 guard."""
    sample_order.delivery_address_id = sample_address.id
    db.session.commit()
    return sample_order


@pytest.fixture
def fake_payme_outbound(monkeypatch):
    """Strict fake patched onto ``PaymeProvider._payme_request``.

    Used only by tests that exercise Payme outbound (subscribe API, status
    polling). Webhook-driven tests don't need this — they hit the inbound
    JSON-RPC handlers directly.
    """
    from business_app.services.providers.payme_provider import PaymeProvider
    fake = FakePayme()
    monkeypatch.setattr(PaymeProvider, '_payme_request', lambda self, m, p: fake(m, p), raising=True)
    yield fake


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _seed_payme_payment(db, order, payment_id_str: str = 'PAY_PAYME_TEST') -> Payment:
    """Pre-create a PENDING Payme payment for a given order.

    Real flow creates the payment row inside ``CreateTransaction``. Some
    tests want to skip ahead to ``PerformTransaction`` directly without
    reproducing the full handshake every time.
    """
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.PAYME,
        amount=order.total_amount,
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id=payment_id_str,
        provider_data={},
    )
    db.session.add(payment)
    db.session.commit()
    return payment


def _seed_click_payment(
    db,
    order,
    payment_id_str: str = 'PAY_CLICK_TEST',
    *,
    click_paydoc_id: str = '20240101000001',
) -> Payment:
    """Seed a PENDING Click Payment row.

    ``click_paydoc_id`` is required by ``_resolve_click_payment_id`` for the
    refund/status/reconcile paths. Default seeds a deterministic value so
    refund/reconcile cells don't have to set up provider_data themselves.
    """
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=order.total_amount,
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id=payment_id_str,
        provider_data={'click': {'click_paydoc_id': str(click_paydoc_id)}},
    )
    db.session.add(payment)
    db.session.commit()
    return payment


def _payme_amount(order) -> int:
    """Convert order total to Payme's tiyin units (×100)."""
    return int(float(order.total_amount) * 100)


# --------------------------------------------------------------------------- #
# 1. Webhook state machine
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.payment
class TestWebhookStateMachine:
    """Provider × outcome × flow: assert payment + order final state."""

    # ---- Click ------------------------------------------------------------

    def test_click_prepare_then_complete_marks_paid(
        self, matrix_app, matrix_client, db, order_with_address, no_fiscalization,
    ):
        order = order_with_address
        _seed_click_payment(db, order)
        # Prepare
        prepare_body = make_click_webhook_form(
            action='0',
            click_trans_id='click-tx-1',
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        )
        resp_prepare = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=prepare_body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp_prepare.status_code == 200
        body_prepare = resp_prepare.get_json()
        assert body_prepare['error'] == 0, body_prepare
        merchant_prepare_id = body_prepare['merchant_prepare_id']

        # Complete
        complete_body = make_click_webhook_form(
            action='1',
            click_trans_id='click-tx-1',
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(merchant_prepare_id),
        )
        resp_complete = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=complete_body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp_complete.status_code == 200
        assert resp_complete.get_json()['error'] == 0

        # Final state assertions
        payment = Payment.query.filter_by(order_id=order.id).one()
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.paid_at is not None
        assert payment.provider_transaction_id == 'click-tx-1'

        refreshed = Order.query.get(order.id)
        assert refreshed.status == OrderStatus.CONFIRMED

    def test_click_complete_with_provider_error_cancels_payment(
        self, matrix_app, matrix_client, db, sample_order, no_fiscalization,
    ):
        _seed_click_payment(db, sample_order)
        # Prepare first so complete has a merchant_prepare_id
        prepare_body = make_click_webhook_form(
            action='0',
            click_trans_id='click-tx-2',
            merchant_trans_id=sample_order.order_number,
            amount=str(int(sample_order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        )
        merchant_prepare_id = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=prepare_body,
            content_type='application/x-www-form-urlencoded',
        ).get_json()['merchant_prepare_id']

        # Complete with non-zero error => cancel
        complete_body = make_click_webhook_form(
            action='1',
            click_trans_id='click-tx-2',
            merchant_trans_id=sample_order.order_number,
            amount=str(int(sample_order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(merchant_prepare_id),
            error=-9,
            error_note='User cancelled',
        )
        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=complete_body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200

        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.CANCELLED
        assert payment.failure_reason == 'User cancelled'

    def test_click_amount_mismatch_returns_error_minus_2(
        self, matrix_app, matrix_client, db, sample_order, no_fiscalization,
    ):
        _seed_click_payment(db, sample_order)
        wrong_amount = str(int(sample_order.total_amount) + 1)
        prepare_body = make_click_webhook_form(
            action='0',
            click_trans_id='click-tx-3',
            merchant_trans_id=sample_order.order_number,
            amount=wrong_amount,
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        )
        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=prepare_body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200
        assert resp.get_json()['error'] == -2

        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.PENDING

    def test_click_invalid_signature_returns_signed_failure(
        self, matrix_app, matrix_client, db, sample_order, no_fiscalization,
    ):
        _seed_click_payment(db, sample_order)
        body = make_click_webhook_form(
            action='0',
            click_trans_id='click-tx-bad',
            merchant_trans_id=sample_order.order_number,
            amount=str(int(sample_order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        )
        body['sign_string'] = 'totally-bogus'
        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200
        # PAY-006 / signature failure: Click expects HTTP 200 + provider-format error
        assert resp.get_json()['error'] == -1

        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.PENDING

    # ---- Payme ------------------------------------------------------------

    def test_payme_check_then_create_then_perform_marks_paid(
        self, matrix_app, matrix_client, db, order_with_address, no_fiscalization, payme_basic_auth_header,
    ):
        order = order_with_address
        # Patch verify_payme_signature to True — Payme uses Basic-auth header
        # which we DO build correctly via fixture, but mock to avoid coupling
        # to the request-context plumbing in the verifier helper.
        with patch(
            'business_app.services.providers.payme_provider.PaymeProvider.verify_payme_signature',
            return_value=True,
        ):
            amount_tiyin = _payme_amount(order)

            # 1. CheckPerformTransaction
            body_check = make_payme_webhook_body(
                method='CheckPerformTransaction',
                params={'amount': amount_tiyin, 'account': {'order_id': order.id}},
                request_id=101,
            )
            resp_check = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=body_check,
                headers=payme_basic_auth_header,
            )
            assert resp_check.status_code == 200
            assert resp_check.get_json()['result'] == {'allow': True}

            # 2. CreateTransaction
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            body_create = make_payme_webhook_body(
                method='CreateTransaction',
                params={
                    'id': 'payme-tx-1',
                    'time': now_ms,
                    'amount': amount_tiyin,
                    'account': {'order_id': order.id},
                },
                request_id=102,
            )
            resp_create = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=body_create,
                headers=payme_basic_auth_header,
            )
            assert resp_create.status_code == 200
            create_result = resp_create.get_json()['result']
            assert create_result['state'] == PaymeState.CREATED.value

            # 3. PerformTransaction
            body_perform = make_payme_webhook_body(
                method='PerformTransaction',
                params={'id': 'payme-tx-1'},
                request_id=103,
            )
            resp_perform = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=body_perform,
                headers=payme_basic_auth_header,
            )
            assert resp_perform.status_code == 200
            perform_result = resp_perform.get_json()['result']
            assert perform_result['state'] == PaymeState.COMPLETED.value

            payment = Payment.query.filter_by(order_id=order.id, payment_method=PaymentMethod.PAYME).one()
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.provider_transaction_id == 'payme-tx-1'
            refreshed = Order.query.get(order.id)
            assert refreshed.status == OrderStatus.CONFIRMED

    def test_payme_check_perform_invalid_amount(
        self, matrix_app, matrix_client, db, sample_order, payme_basic_auth_header,
    ):
        with patch(
            'business_app.services.providers.payme_provider.PaymeProvider.verify_payme_signature',
            return_value=True,
        ):
            body = make_payme_webhook_body(
                method='CheckPerformTransaction',
                params={'amount': _payme_amount(sample_order) + 1, 'account': {'order_id': sample_order.id}},
                request_id=201,
            )
            resp = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=body,
                headers=payme_basic_auth_header,
            )
            assert resp.status_code == 200
            assert 'error' in resp.get_json()

    def test_payme_check_perform_unknown_order(
        self, matrix_app, matrix_client, db, sample_user, payme_basic_auth_header,
    ):
        with patch(
            'business_app.services.providers.payme_provider.PaymeProvider.verify_payme_signature',
            return_value=True,
        ):
            body = make_payme_webhook_body(
                method='CheckPerformTransaction',
                params={'amount': 18000 * 100, 'account': {'order_id': 999_999}},
                request_id=202,
            )
            resp = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=body,
                headers=payme_basic_auth_header,
            )
            assert resp.status_code == 200
            assert 'error' in resp.get_json()

    def test_payme_perform_unknown_transaction(
        self, matrix_app, matrix_client, db, sample_order, payme_basic_auth_header,
    ):
        with patch(
            'business_app.services.providers.payme_provider.PaymeProvider.verify_payme_signature',
            return_value=True,
        ):
            body = make_payme_webhook_body(
                method='PerformTransaction',
                params={'id': 'never-created'},
                request_id=203,
            )
            resp = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=body,
                headers=payme_basic_auth_header,
            )
            assert resp.status_code == 200
            assert 'error' in resp.get_json()
            err = resp.get_json()['error']
            # PaymeErrors.TRANSACTION_NOT_FOUND
            assert err['code'] in (-31003, -31050, -31000)

    def test_payme_invalid_signature_returns_jsonrpc_error(
        self, matrix_app, matrix_client, db, sample_order,
    ):
        # No basic-auth header at all -> verifier rejects.
        body = make_payme_webhook_body(
            method='CheckPerformTransaction',
            params={'amount': _payme_amount(sample_order), 'account': {'order_id': sample_order.id}},
            request_id=204,
        )
        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='payme'),
            json=body,
        )
        assert resp.status_code == 200
        # JSON-RPC error envelope per api/payments.py invalid-signature branch
        payload = resp.get_json()
        assert 'error' in payload
        assert payload['error']['code'] == -32504


# --------------------------------------------------------------------------- #
# 2. Concurrent webhooks (idempotency at the HTTP layer — PAY-002)
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.payment
class TestConcurrentWebhooks:
    """Replayed/concurrent webhooks must collapse to one effect."""

    def test_click_complete_replay_creates_one_transaction(
        self, matrix_app, matrix_client, db, order_with_address, no_fiscalization,
    ):
        """PAY-002: a replayed Click webhook (same click_trans_id, different
        nonce — mirroring real gateway retry) must collapse to one effect."""
        order = order_with_address
        _seed_click_payment(db, order)
        prepare_body = make_click_webhook_form(
            action='0',
            click_trans_id='click-replay-1',
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        )
        merchant_prepare_id = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=prepare_body,
            content_type='application/x-www-form-urlencoded',
        ).get_json()['merchant_prepare_id']

        # Build two bodies with the same gateway txn id but distinct nonces:
        # the WebhookSignatureVerifier nonce store would otherwise reject the
        # second hit as a replay attack BEFORE the PAY-002 guard runs.
        complete_body_1 = make_click_webhook_form(
            action='1',
            click_trans_id='click-replay-1',
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(merchant_prepare_id),
        )
        complete_body_2 = make_click_webhook_form(
            action='1',
            click_trans_id='click-replay-1',
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
            merchant_prepare_id=str(merchant_prepare_id),
        )

        resp1 = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=complete_body_1,
            content_type='application/x-www-form-urlencoded',
        )
        resp2 = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=complete_body_2,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp1.status_code == 200 and resp2.status_code == 200
        # Both responses succeed (PAY-002 returns cached success on duplicate).
        assert resp1.get_json().get('error') == 0
        assert resp2.get_json().get('error') in (0, None), resp2.get_json()

        payment = Payment.query.filter_by(order_id=order.id).one()
        assert payment.status == PaymentStatus.COMPLETED

        # Exactly one click_complete PaymentTransaction row — PAY-002 collapsed
        # the second hit before the handler ran a second time.
        complete_txs = PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type='click_complete',
        ).all()
        assert len(complete_txs) == 1, (
            f"Expected exactly one click_complete tx; found {len(complete_txs)}"
        )

    def test_payme_perform_replay_completes_idempotently(
        self, matrix_app, matrix_client, db, order_with_address, no_fiscalization, payme_basic_auth_header,
    ):
        order = order_with_address
        with patch(
            'business_app.services.providers.payme_provider.PaymeProvider.verify_payme_signature',
            return_value=True,
        ):
            amount_tiyin = _payme_amount(order)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            create_body = make_payme_webhook_body(
                method='CreateTransaction',
                params={
                    'id': 'payme-replay-1',
                    'time': now_ms,
                    'amount': amount_tiyin,
                    'account': {'order_id': order.id},
                },
                request_id=301,
            )
            matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=create_body,
                headers=payme_basic_auth_header,
            )

            # Two perform attempts with the same params.id but distinct nonces
            # (mirrors real Payme retry behavior). PAY-002 collapses on params.id.
            perform_body_1 = make_payme_webhook_body(
                method='PerformTransaction',
                params={'id': 'payme-replay-1'},
                request_id=302,
            )
            perform_body_2 = make_payme_webhook_body(
                method='PerformTransaction',
                params={'id': 'payme-replay-1'},
                request_id=303,
            )

            resp1 = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=perform_body_1,
                headers=payme_basic_auth_header,
            )
            resp2 = matrix_client.post(
                WEBHOOK_PATH.format(provider='payme'),
                json=perform_body_2,
                headers=payme_basic_auth_header,
            )
            assert resp1.status_code == 200 and resp2.status_code == 200
            assert resp1.get_json()['result']['state'] == PaymeState.COMPLETED.value
            assert resp2.get_json()['result']['state'] == PaymeState.COMPLETED.value

            payment = Payment.query.filter_by(order_id=order.id).one()
            assert payment.status == PaymentStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 3. Refund reversals mid-flow
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.payment
class TestRefundReversalsMidFlow:

    def _completed_click_payment(self, db, sample_order) -> Payment:
        payment = _seed_click_payment(db, sample_order)
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = datetime.now(timezone.utc)
        payment.provider_transaction_id = 'click-tx-refund'
        db.session.commit()
        return payment

    def test_full_refund_flips_to_cancelled(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        from business_app.services.payment_service import PaymentService
        payment = self._completed_click_payment(db, sample_order)

        fake_click_merchant.script(
            method='DELETE',
            url_contains='',  # match any URL — refund hits a configured endpoint
            json_body={'error_code': 0, 'error_note': 'OK'},
            label='click-refund-ok',
        )

        with matrix_app.app_context():
            ok = PaymentService().process_refund(
                payment.id, payment.amount, reason='customer-requested',
            )
        assert ok is True

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.CANCELLED

    def test_partial_refund_flips_to_partially_refunded(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        from business_app.services.payment_service import PaymentService
        payment = self._completed_click_payment(db, sample_order)
        fake_click_merchant.script(
            method='DELETE',
            url_contains='',
            json_body={'error_code': 0, 'error_note': 'OK'},
            label='click-partial-refund',
        )

        partial = Decimal('5000.00')
        with matrix_app.app_context():
            ok = PaymentService().process_refund(payment.id, partial, reason='partial')
        assert ok is True

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.PARTIALLY_REFUNDED

    def test_payme_cancel_after_complete_refunds_via_order_service(
        self, matrix_app, matrix_client, db, order_with_address, no_fiscalization, payme_basic_auth_header,
    ):
        order = order_with_address
        with patch(
            'business_app.services.providers.payme_provider.PaymeProvider.verify_payme_signature',
            return_value=True,
        ):
            amount_tiyin = _payme_amount(order)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            # Run create+perform to drive payment to COMPLETED.
            for body, rid in [
                (
                    make_payme_webhook_body(
                        method='CreateTransaction',
                        params={
                            'id': 'payme-tx-cancel',
                            'time': now_ms,
                            'amount': amount_tiyin,
                            'account': {'order_id': order.id},
                        },
                        request_id=401,
                    ),
                    401,
                ),
                (
                    make_payme_webhook_body(
                        method='PerformTransaction',
                        params={'id': 'payme-tx-cancel'},
                        request_id=402,
                    ),
                    402,
                ),
            ]:
                matrix_client.post(
                    WEBHOOK_PATH.format(provider='payme'),
                    json=body,
                    headers=payme_basic_auth_header,
                )

            # CancelTransaction after COMPLETED triggers OrderService.cancel_order
            # + refund. Patch OrderService.cancel_order to a no-op so we don't
            # need the full order-cancellation surface.
            with patch(
                'business_app.services.order_service.OrderService.cancel_order',
                return_value=None,
            ):
                cancel_body = make_payme_webhook_body(
                    method='CancelTransaction',
                    params={'id': 'payme-tx-cancel', 'reason': 5},
                    request_id=403,
                )
                resp = matrix_client.post(
                    WEBHOOK_PATH.format(provider='payme'),
                    json=cancel_body,
                    headers=payme_basic_auth_header,
                )

            assert resp.status_code == 200
            payload = resp.get_json()
            assert 'result' in payload, payload
            assert payload['result']['state'] == PaymeState.REFUNDED.value


# --------------------------------------------------------------------------- #
# 4. Provider timeout / circuit (PAY-003)
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.payment
class TestProviderTimeoutCircuit:

    def test_click_outbound_timeout_raises_provider_unavailable(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        """Click outbound timeout surfaces as ProviderUnavailableError to the caller."""
        from business_app.services.payment_service import PaymentService

        payment = _seed_click_payment(db, sample_order)
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = datetime.now(timezone.utc)
        db.session.commit()

        fake_click_merchant.script(
            method='DELETE',
            url_contains='',
            raise_exc=ProviderUnavailableError(
                'Click upstream timed out', provider='click', retry_after_seconds=30,
            ),
            label='click-refund-timeout',
        )

        with matrix_app.app_context():
            with pytest.raises(ProviderUnavailableError):
                PaymentService().process_refund(
                    payment.id, payment.amount, reason='timeout-test',
                )

        # Payment status preserved — refund did not partially apply.
        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 5. Reconciliation catches stranded PENDING (PAY-007)
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.payment
class TestReconciliationCatchesStrandedPending:

    def test_click_pending_past_threshold_flips_to_completed(
        self, matrix_app, db, order_with_address, fake_click_merchant, no_fiscalization,
    ):
        """A PENDING Click payment older than reconcile threshold + gateway
        reports completed → reconcile flips status to COMPLETED."""
        from business_app.tasks.payment_tasks import reconcile_pending_payments

        payment = _seed_click_payment(db, order_with_address)
        # Age the payment so reconcile picks it up. Keep tz-aware — Payment
        # uses ``DateTime(timezone=True)``; the in-Python comparison
        # ``payment.created_at < timeout_threshold`` blows up on naive vs aware.
        payment.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        db.session.commit()

        # Click status endpoint — return code 1 for the reconcile poll.
        # ``_map_payment_status``: 1=completed, 2/-1/-2=cancelled, 3/-3=failed.
        fake_click_merchant.script(
            method='GET',
            url_contains='',
            json_body={
                'payment_status': 1,
                'error_code': 0,
                'payment_id': 999_001,
            },
            label='click-status-completed',
        )

        with matrix_app.app_context():
            result = reconcile_pending_payments()

        assert result['completed'] >= 1, result

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.COMPLETED

    def test_click_pending_past_timeout_with_unknown_status_auto_cancels(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        """Past PAYMENT_TIMEOUT_MINUTES with no recognizable gateway status =>
        auto-cancel.

        Skipped on SQLite: ``Payment.created_at`` is declared
        ``DateTime(timezone=True)`` but SQLite drops tz info on round-trip.
        The reconcile branch ``payment.created_at < timeout_threshold``
        raises ``TypeError: can't compare offset-naive and offset-aware
        datetimes`` because the retrieved value is naive while
        ``timeout_threshold`` (built from ``datetime.now(timezone.utc)``)
        is aware. Postgres preserves the tz; this cell will run under the
        Postgres-backed integration lane (mirrors TST-004's pattern). The
        success-status reconcile cell above passes because that branch
        exits before the timeout comparison runs.
        """
        from sqlalchemy import inspect as sa_inspect
        from business_app.tasks.payment_tasks import reconcile_pending_payments

        if _db.engine.url.get_backend_name() == 'sqlite':
            pytest.skip(
                "SQLite drops timezone on DateTime round-trip; reconcile "
                "timeout comparison requires tz-aware datetimes (Postgres only). "
                "Tracked alongside TST-001 / TST-011 Postgres-lane work."
            )

        payment = _seed_click_payment(db, sample_order)
        payment.created_at = datetime.now(timezone.utc) - timedelta(minutes=120)
        db.session.commit()

        # error_code MUST be 0 — non-zero raises PaymentError out of
        # merchant_request before reconcile can route to the auto-cancel
        # branch. payment_status = 0 (or any non-recognised int) maps to
        # PENDING via ``_map_payment_status``, which then triggers
        # auto-cancel because the payment is past PAYMENT_TIMEOUT_MINUTES.
        fake_click_merchant.script(
            method='GET',
            url_contains='',
            json_body={
                'payment_status': 0,
                'error_code': 0,
                'payment_id': 999_002,
            },
            label='click-status-unknown-pending',
        )

        with matrix_app.app_context():
            result = reconcile_pending_payments()

        assert result['cancelled'] >= 1, result

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.CANCELLED
