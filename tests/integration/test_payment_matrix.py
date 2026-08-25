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
from business_app.utils.constants import (
    PaymeState,
)
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import ProviderUnavailableError
from business_app.utils.payment_projection import order_is_payable_online
from tests.integration.fake_gateways import (
    FakeClickMerchant,
    FakePayme,
    TEST_CLICK_SHOP_SECRET_KEY,
    TEST_PAYME_SECRET_KEY,
    UnscriptedGatewayCall,
    make_click_signature,
    make_click_webhook_form,
    make_payme_webhook_body,
)


WEBHOOK_PATH = '/api/v1/payments/webhook/{provider}'


# --------------------------------------------------------------------------- #
# Module-level fixtures
#
# NOTE: ``matrix_app`` / ``matrix_client`` / ``no_fiscalization`` /
# ``sample_address`` / ``order_with_address`` now live in
# ``tests/integration/conftest.py`` so they are shared (via pytest fixture
# discovery) with ``test_click_crash_recovery.py``. Do not redefine them here.
# --------------------------------------------------------------------------- #

@pytest.fixture
def payme_basic_auth_header():
    """Build the Basic-auth header Payme webhook signature verification expects."""
    creds = f"Paycom:{TEST_PAYME_SECRET_KEY}".encode('utf-8')
    return {'Authorization': 'Basic ' + base64.b64encode(creds).decode('ascii')}


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

    def test_click_complete_with_provider_error_keeps_live_payment_payable(
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

        # B1 fix round 2: this cell asserted cancel-on-live-order, which is
        # the defect. A declined attempt on an unresolved order leaves the
        # payment payable so the customer can retry the same link; the -9
        # protocol answer to Click is unchanged. The release/cancel boundary is
        # covered by the resolved-order cells in the unit guard file.
        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None

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

    def _post_click_prepare_for_complete(self, matrix_client, order, click_trans_id):
        prepare_body = make_click_webhook_form(
            action='0',
            click_trans_id=click_trans_id,
            merchant_trans_id=order.order_number,
            amount=str(int(order.total_amount)),
            secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        )
        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=prepare_body,
            content_type='application/x-www-form-urlencoded',
        )
        return resp.get_json()['merchant_prepare_id']

    def _build_click_complete_form(
        self, *, order, click_trans_id, merchant_prepare_id, click_paydoc_id='9988776655',
    ):
        import time as _time
        import uuid as _uuid
        sign_time = str(int(_time.time()))
        body = {
            'click_trans_id': str(click_trans_id),
            'service_id': '55',
            'merchant_trans_id': str(order.order_number),
            'merchant_prepare_id': str(merchant_prepare_id),
            'click_paydoc_id': str(click_paydoc_id),
            'amount': str(int(order.total_amount)),
            'action': '1',
            'sign_time': sign_time,
            'error': '0',
            'error_note': 'Success',
        }
        body['sign_string'] = make_click_signature(body, TEST_CLICK_SHOP_SECRET_KEY)
        body['timestamp'] = sign_time
        body['nonce'] = f'click-test-{click_trans_id}-1-{sign_time}-{_uuid.uuid4().hex[:8]}'
        return body

    def test_click_complete_with_missing_error_field_does_not_mark_paid(
        self, matrix_app, matrix_client, db, sample_order, no_fiscalization,
    ):
        _seed_click_payment(db, sample_order)
        merchant_prepare_id = self._post_click_prepare_for_complete(
            matrix_client, sample_order, 'click-tx-missing-err',
        )

        body = self._build_click_complete_form(
            order=sample_order,
            click_trans_id='click-tx-missing-err',
            merchant_prepare_id=merchant_prepare_id,
        )
        body.pop('error')

        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200
        assert resp.get_json()['error'] == -8

        # B1 fix round 2: `sample_order` is PENDING, i.e. UNRESOLVED, so this
        # callback may no longer end the payment. The cell's real subject —
        # "does not mark paid" — is untouched: the -8 answer and the order
        # staying out of CONFIRMED still assert it. See
        # tests/unit/test_update_payment_status_live_order_guard.py for the rule.
        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None
        order = Order.query.get(sample_order.id)
        assert order.status != OrderStatus.CONFIRMED

    def test_click_complete_with_empty_string_error_does_not_mark_paid(
        self, matrix_app, matrix_client, db, sample_order, no_fiscalization,
    ):
        _seed_click_payment(db, sample_order)
        merchant_prepare_id = self._post_click_prepare_for_complete(
            matrix_client, sample_order, 'click-tx-empty-err',
        )

        body = self._build_click_complete_form(
            order=sample_order,
            click_trans_id='click-tx-empty-err',
            merchant_prepare_id=merchant_prepare_id,
        )
        body['error'] = ''

        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200
        assert resp.get_json()['error'] == -8

        # B1 fix round 2: `sample_order` is PENDING, i.e. UNRESOLVED, so this
        # callback may no longer end the payment. The cell's real subject —
        # "does not mark paid" — is untouched: the -8 answer and the order
        # staying out of CONFIRMED still assert it. See
        # tests/unit/test_update_payment_status_live_order_guard.py for the rule.
        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.PENDING
        order = Order.query.get(sample_order.id)
        assert order.status != OrderStatus.CONFIRMED

    def test_click_complete_zero_error_no_click_trans_id_does_not_mark_paid(
        self, matrix_app, matrix_client, db, sample_order, no_fiscalization,
    ):
        _seed_click_payment(db, sample_order)
        merchant_prepare_id = self._post_click_prepare_for_complete(
            matrix_client, sample_order, 'click-tx-no-trans',
        )

        body = self._build_click_complete_form(
            order=sample_order,
            click_trans_id='',
            merchant_prepare_id=merchant_prepare_id,
        )

        resp = matrix_client.post(
            WEBHOOK_PATH.format(provider='click'),
            data=body,
            content_type='application/x-www-form-urlencoded',
        )
        assert resp.status_code == 200
        assert resp.get_json()['error'] == -8

        # B1 fix round 2: `sample_order` is PENDING, i.e. UNRESOLVED, so this
        # callback may no longer end the payment. The cell's real subject —
        # "does not mark paid" — is untouched: the -8 answer and the order
        # staying out of CONFIRMED still assert it. See
        # tests/unit/test_update_payment_status_live_order_guard.py for the rule.
        payment = Payment.query.filter_by(order_id=sample_order.id).one()
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None
        order = Order.query.get(sample_order.id)
        assert order.status != OrderStatus.CONFIRMED

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

    # INVERTED BY B4a (owner ruling 2026-08-24). These two cells used to assert
    # that a full refund flips a Click payment to CANCELLED and a partial one to
    # PARTIALLY_REFUNDED — i.e. they PINNED the behaviour the owner's rule
    # outlaws: "we never ever cancel card / click paid payments", because the
    # fiscal receipt filed for the payment cannot be un-submitted. They now
    # assert the refusal, and that the gateway is never dialled.
    #
    # `PaymentStatus.PARTIALLY_REFUNDED` becomes unreachable in production with
    # them: `payment_service.py` was its only writer and Payme — the one
    # surviving caller of `process_refund` — always passes the full amount.

    def test_full_refund_of_a_click_payment_is_refused(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        from business_app.services.payment_service import PaymentService
        from business_app.utils.exceptions import ValidationError
        payment = self._completed_click_payment(db, sample_order)

        with matrix_app.app_context():
            with pytest.raises(ValidationError):
                PaymentService().process_refund(
                    payment.id, payment.amount, reason='customer-requested',
                )

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.COMPLETED
        assert fake_click_merchant.calls == [], (
            'the Click merchant API must never be dialled for a refund'
        )

    def test_partial_refund_of_a_click_payment_is_refused_too(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        from business_app.services.payment_service import PaymentService
        from business_app.utils.exceptions import ValidationError
        payment = self._completed_click_payment(db, sample_order)

        with matrix_app.app_context():
            with pytest.raises(ValidationError):
                PaymentService().process_refund(payment.id, Decimal('5000.00'), reason='partial')

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.COMPLETED
        assert fake_click_merchant.calls == []

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
        """Click outbound timeout surfaces as ProviderUnavailableError to the caller.

        RETARGETED BY B4a. This cell used to drive the timeout through
        ``process_refund``, which no longer makes an outbound call at all (the
        owner's rule: a card/Click payment is never reversed) — and it was the
        ONLY PAY-003 coverage of the click merchant client's timeout behaviour,
        so deleting it with the refund branch would have dropped that silently.
        It now drives the same seam through ``check_payment_status``, the
        outbound GET the reconcile sweep really makes.
        """
        from business_app.services.payment_service import PaymentService

        payment = _seed_click_payment(db, sample_order)
        payment.status = PaymentStatus.PENDING
        payment.provider_data = {'click': {'click_paydoc_id': '20240101000007'}}
        db.session.commit()

        fake_click_merchant.script(
            method='GET',
            url_contains='',
            raise_exc=ProviderUnavailableError(
                'Click upstream timed out', provider='click', retry_after_seconds=30,
            ),
            label='click-status-timeout',
        )

        with matrix_app.app_context():
            with pytest.raises(ProviderUnavailableError):
                PaymentService()._get_click_provider_service().check_payment_status(payment)

        # Payment status preserved — nothing partially applied.
        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.PENDING


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

        # Click status endpoint — return code 2 (verified "success") for the
        # reconcile poll. ``_map_payment_status`` (corrected 2026-07-09 against
        # Click's live merchant API): 2=completed, -1/-2=cancelled, -3=failed,
        # 0/1/anything else=pending (created/processing are not terminal).
        fake_click_merchant.script(
            method='GET',
            url_contains='',
            json_body={
                'payment_status': 2,
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

    def test_click_pending_past_timeout_with_unknown_status_leaves_pending_and_alerts(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        """Positive-evidence contract (Task 7): past PAYMENT_TIMEOUT_MINUTES with
        an *unknown/ambiguous* gateway status must NOT auto-cancel — the charge
        may exist and blind-cancelling would strand a real payment. The payment
        is left PENDING and flagged for manual review exactly once
        (``provider_data.click.reconcile_alerted_at``).

        Skipped on SQLite: ``Payment.created_at`` is declared
        ``DateTime(timezone=True)`` but SQLite drops tz info on round-trip.
        Postgres preserves the tz; this cell runs under the Postgres-backed
        integration lane (mirrors TST-004's pattern).
        """
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
        # merchant_request before reconcile can route into the timeout branch.
        # payment_status = 0 maps to PENDING via ``_map_payment_status`` — a
        # recognized-but-unresolved status, i.e. NOT affirmative evidence of
        # cancellation. Under the positive-evidence contract this leaves the
        # payment PENDING (only ``not_found`` / gateway cancelled/failed cancel).
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

        assert result['cancelled'] == 0, result
        assert result['unchanged'] >= 1, result

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.PENDING
        assert (payment_refreshed.provider_data or {}).get('click', {}).get('reconcile_alerted_at'), (
            "unknown-past-timeout must write the one-shot review flag"
        )


# --------------------------------------------------------------------------- #
# 6. Reconcile prevention gate — PAY-007 fix (Task 1)
# --------------------------------------------------------------------------- #

@pytest.mark.integration
@pytest.mark.payment
class TestReconcilePreventionGate:
    """PAY-007 fix: timeout auto-cancel must be skipped for orders that are
    already past PENDING (CONFIRMED / in-fulfillment / DELIVERED etc.).
    """

    def _stale_click_payment(self, db, order) -> Payment:
        """Seed a PENDING Click payment aged past the timeout threshold."""
        payment = _seed_click_payment(db, order)
        payment.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.session.commit()
        return payment

    def test_reconcile_skips_timeout_cancel_for_confirmed_order(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        """PAY-007 gate: a PENDING payment whose order is CONFIRMED must NOT be
        auto-cancelled by the reconcile task even after the timeout window.

        Skipped on SQLite — same timezone-awareness limitation as the sibling
        timeout test (see test_click_pending_past_timeout_with_unknown_status_auto_cancels).
        """
        if _db.engine.url.get_backend_name() == 'sqlite':
            pytest.skip(
                "SQLite drops timezone on DateTime round-trip; reconcile "
                "timeout comparison requires tz-aware datetimes (Postgres only)."
            )

        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()
        payment = self._stale_click_payment(db, sample_order)

        # Script an unrecognised status so the reconcile flows into the
        # timeout branch rather than the completed/cancelled branch.
        fake_click_merchant.script(
            method='GET',
            url_contains='',
            json_body={
                'payment_status': 0,
                'error_code': 0,
                'payment_id': 999_010,
            },
            label='click-status-unknown-confirmed-order',
        )

        from business_app.tasks.payment_tasks import reconcile_pending_payments
        with matrix_app.app_context():
            result = reconcile_pending_payments()

        assert result['cancelled'] == 0, (
            f"Gate should prevent cancel for CONFIRMED order; got counts={result}"
        )
        assert result['unchanged'] >= 1, result

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.PENDING, (
            "Payment must remain PENDING — order is in-fulfillment"
        )

    def test_reconcile_does_not_cancel_a_live_order_on_affirmative_gateway_cancel(
        self, matrix_app, db, sample_order, fake_click_merchant, no_fiscalization,
    ):
        """B1 (2026-08-25) — THIS CELL'S EXPECTATION WAS INVERTED.

        It used to assert that affirmative gateway evidence
        (``payment_status=-2`` -> CANCELLED) cancels our payment row regardless of
        the order's state, and it was named
        ``test_reconcile_cancels_pending_order_on_affirmative_gateway_cancel``.
        That is now the OPPOSITE of the governing rule, so it was rewritten rather
        than left to contradict
        ``tests/unit/test_update_payment_status_live_order_guard.py``.

        Why the old expectation was wrong: a gateway cancel describes ONE
        abandoned Click attempt, not the payability of the order. Writing
        CANCELLED makes the Phase 4A PREPARE guard (``order_is_payable_online``,
        which requires PENDING/PROCESSING) refuse every future attempt — so the
        customer could never pay again on an order they still owe for, under a
        policy (Phase 4D) that promises the link stays payable through delivery.

        NOTE this cell never actually ran in the default lane: it self-skips on
        SQLite, which is a large part of why the defect survived.

        Skipped on SQLite — same timezone-awareness limitation.
        """
        if _db.engine.url.get_backend_name() == 'sqlite':
            pytest.skip(
                "SQLite drops timezone on DateTime round-trip; reconcile "
                "timeout comparison requires tz-aware datetimes (Postgres only)."
            )

        # sample_order is already PENDING by default.
        payment = self._stale_click_payment(db, sample_order)

        # payment_status = -2 maps to CANCELLED via ``_map_payment_status``
        # (corrected 2026-07-09: negative codes are Click's error/cancel
        # range). This is the STRONGEST evidence the gateway can give us — and
        # under B1 even that must not end the payment while the order is live,
        # because it describes one abandoned attempt, not the order's fate.
        fake_click_merchant.script(
            method='GET',
            url_contains='',
            json_body={
                'payment_status': -2,
                'error_code': 0,
                'payment_id': 999_011,
            },
            label='click-status-cancelled-pending-order',
        )

        from business_app.tasks.payment_tasks import reconcile_pending_payments
        with matrix_app.app_context():
            result = reconcile_pending_payments()

        assert result['cancelled'] == 0, (
            f"a live order's payment must survive an abandoned Click attempt; got counts={result}"
        )

        _db.session.expire_all()
        payment_refreshed = Payment.query.get(payment.id)
        assert payment_refreshed.status == PaymentStatus.PENDING
        assert order_is_payable_online(payment_refreshed.order, payment_refreshed) is True, (
            "the customer must still be able to PREPARE a fresh attempt on the same link"
        )
