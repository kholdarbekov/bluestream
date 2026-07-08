"""Late genuine Click Completes landing on non-live payments (policy: accept/reverse/credit)."""

from decimal import Decimal

import pytest

from business_app import db
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, Payment, PaymentTransaction
from shared.enums import CashCollectionSource, OrderStatus, PaymentMethod, PaymentStatus

from tests.integration.fake_gateways import make_click_webhook_form
from tests.integration.test_payment_matrix import _seed_click_payment  # plain helper, safe to import


WEBHOOK_URL = "/api/v1/payments/webhook/click"


def _post_complete(client, order, payment, click_trans_id, *, error=0, click_paydoc_id="20260708000042"):
    from tests.integration.fake_gateways import TEST_CLICK_SHOP_SECRET_KEY

    form = make_click_webhook_form(
        action="1",
        click_trans_id=click_trans_id,
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        merchant_prepare_id=str(payment.id),
        error=error,
        click_paydoc_id=click_paydoc_id,
    )
    return client.post(WEBHOOK_URL, data=form, content_type="application/x-www-form-urlencoded")


class TestLateCompleteAcceptAndFulfill:
    def test_genuine_late_complete_on_cancelled_payment_accepts_and_fulfills(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        # Simulate the reconcile auto-cancel that raced the debit.
        payment.status = PaymentStatus.CANCELLED
        payment.failure_reason = "Auto-cancelled: gateway status unknown past timeout"
        db.session.commit()

        resp = _post_complete(matrix_client, order, payment, click_trans_id="910001")
        assert resp.status_code == 200
        assert resp.get_json()["error"] == 0  # success, NOT -9

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.failure_reason is None
        assert payment.provider_transaction_id == "910001"
        assert Order.query.get(order.id).status == OrderStatus.CONFIRMED
        assert Order.query.get(order.id).is_paid is True
        txn = PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type="click_complete_late_accepted"
        ).one()
        assert txn.provider_transaction_id == "910001"
        assert txn.success is True

    def test_genuine_late_complete_on_failed_payment_accepts(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.FAILED
        db.session.commit()

        resp = _post_complete(matrix_client, order, payment, click_trans_id="910002")
        assert resp.get_json()["error"] == 0
        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.COMPLETED

    def test_non_genuine_complete_on_cancelled_payment_keeps_minus_9(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.CANCELLED
        db.session.commit()

        # error != 0 -> not a genuine debit -> existing -9 behavior.
        resp = _post_complete(matrix_client, order, payment, click_trans_id="910003", error=-1)
        assert resp.get_json()["error"] == -9
        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.CANCELLED

    def test_winner_ids_not_clobbered_by_late_callback(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        """A duplicate Complete (different trans id) must not overwrite the winner's ids."""
        order = order_with_address
        payment = _seed_click_payment(db, order)
        r1 = _post_complete(matrix_client, order, payment, click_trans_id="910010")
        assert r1.get_json()["error"] == 0

        r2 = _post_complete(matrix_client, order, payment, click_trans_id="910011")
        assert r2.get_json()["error"] == -4
        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.provider_transaction_id == "910010"  # winner preserved
        assert (payment.provider_data or {}).get("click", {}).get("click_trans_id") == "910010"


class TestDuplicateChargeAutoReversal:
    def test_second_genuine_complete_records_duplicate_and_enqueues_reversal(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        r1 = _post_complete(matrix_client, order, payment, click_trans_id="920001", click_paydoc_id="777001")
        assert r1.get_json()["error"] == 0

        enqueued = []
        from business_app.tasks import payment_tasks as pt

        monkeypatch.setattr(
            pt.reverse_click_payment_task, "delay", lambda *a, **k: enqueued.append((a, k))
        )
        r2 = _post_complete(matrix_client, order, payment, click_trans_id="920002", click_paydoc_id="777002")
        assert r2.get_json()["error"] == -4

        db.session.expire_all()
        txn = PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type="click_duplicate_charge"
        ).one()
        assert txn.status == "pending_reversal"
        assert txn.provider_transaction_id == "920002"
        assert txn.success is False
        # Exact enqueue payload: the INCOMING paydoc id, never the winner's.
        assert enqueued == [((payment.id, "777002", "920002"), {})]

    def test_kill_switch_disables_auto_reversal_but_keeps_record(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        matrix_app.config["CLICK_DUPLICATE_AUTO_REVERSAL_ENABLED"] = False
        order = order_with_address
        payment = _seed_click_payment(db, order)
        _post_complete(matrix_client, order, payment, click_trans_id="920011", click_paydoc_id="777011")

        enqueued = []
        from business_app.tasks import payment_tasks as pt

        monkeypatch.setattr(pt.reverse_click_payment_task, "delay", lambda *a, **k: enqueued.append((a, k)))
        r2 = _post_complete(matrix_client, order, payment, click_trans_id="920012", click_paydoc_id="777012")
        assert r2.get_json()["error"] == -4
        assert enqueued == []
        db.session.expire_all()
        assert PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type="click_duplicate_charge"
        ).count() == 1


class TestLateDebitAfterOfflineSettlement:
    def _settle_offline(self, payment):
        """Simulate convert_electronic_order_to_cash + driver cash collection."""
        # Read the (post-commit-expired) user_id BEFORE mutating so the load it
        # triggers doesn't autoflush a half-set cash payment.
        collector_id = payment.user_id
        payment.payment_method = PaymentMethod.CASH
        payment.status = PaymentStatus.COMPLETED
        # ck_payments_cash_completed_requires_collector: a completed cash payment
        # must record its collector. Any valid user id satisfies the FK + check.
        payment.collected_by = collector_id
        payment.order.payment_method = PaymentMethod.CASH
        payment.order.is_paid = True
        db.session.commit()

    def test_late_debit_after_cash_settlement_credits_customer(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        self._settle_offline(payment)

        notified = []
        from business_app.services.notification_service import NotificationService

        monkeypatch.setattr(
            NotificationService,
            "send_notification",
            lambda self, user_id, key, template_data=None, **kw: notified.append((user_id, key, template_data)),
        )

        resp = _post_complete(matrix_client, order, payment, click_trans_id="940001", click_paydoc_id="778001")
        assert resp.get_json()["error"] == -4

        db.session.expire_all()
        event = CashCollectionEvent.query.filter_by(idempotency_key="click-late-debit:940001").one()
        assert event.customer_id == payment.user_id
        # order_id lives in proof_data, not on the event: a BACKFILL collection
        # cannot target a non-DELIVERED-COD order, so the credit is customer-level.
        assert event.proof_data["order_id"] == order.id
        assert event.source == CashCollectionSource.BACKFILL
        assert event.amount == Decimal(str(order.total_amount))
        assert event.proof_data["click_trans_id"] == "940001"
        assert event.proof_data["flow"] == "click_late_debit"

        txn = PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type="click_late_after_offline_settlement"
        ).one()
        assert txn.status == "credited"

        assert len(notified) == 1
        assert notified[0][0] == payment.user_id
        assert notified[0][1] == "payment_late_debit_credited"
        assert notified[0][2]["order_number"] == order.order_number

    def test_replayed_late_debit_does_not_double_credit(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        order = order_with_address
        payment = _seed_click_payment(db, order)
        self._settle_offline(payment)
        from business_app.services.notification_service import NotificationService

        monkeypatch.setattr(NotificationService, "send_notification", lambda *a, **k: None)

        _post_complete(matrix_client, order, payment, click_trans_id="940002", click_paydoc_id="778002")
        _post_complete(matrix_client, order, payment, click_trans_id="940002", click_paydoc_id="778002")
        db.session.expire_all()
        assert CashCollectionEvent.query.filter_by(idempotency_key="click-late-debit:940002").count() == 1

    def test_late_debit_on_dead_cancelled_order_credits_and_keeps_cancelled(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        """Amendment (2026-07-08): a genuine debit on a CANCELLED payment whose
        order is dead (NOT PENDING, NOT settled) must NOT re-fulfill — it falls
        to the prepaid-credit path; payment and order stay cancelled."""
        order = order_with_address
        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.CANCELLED
        payment.failure_reason = "Auto-cancelled: gateway status unknown past timeout"
        order.status = OrderStatus.CANCELLED
        order.is_paid = False
        db.session.commit()

        from business_app.services.notification_service import NotificationService

        monkeypatch.setattr(NotificationService, "send_notification", lambda *a, **k: None)

        resp = _post_complete(matrix_client, order, payment, click_trans_id="940003", click_paydoc_id="778003")
        assert resp.get_json()["error"] == -4

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.CANCELLED
        assert Order.query.get(order.id).status == OrderStatus.CANCELLED

        event = CashCollectionEvent.query.filter_by(idempotency_key="click-late-debit:940003").one()
        assert event.customer_id == payment.user_id
        assert event.proof_data["order_id"] == order.id
        assert event.source == CashCollectionSource.BACKFILL
