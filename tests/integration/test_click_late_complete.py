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

    def test_late_debit_after_cash_settlement_restores_the_click_rail(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        """SUPERSEDED BEHAVIOUR — this used to credit the Click money and answer -4.

        Owner ruling 2026-08-24 (case C): Click processed a real debit, so the
        order settles on the Click rail and Click gets the fiscal receipt it is
        owed; the driver's banked cash becomes the customer's prepaid credit
        instead. Full end-to-end coverage, including the cash re-booking and the
        driver's session staying intact, lives in
        tests/integration/test_case_c_late_click_after_cash.py — this fixture
        hand-builds the settled state and so has no allocation to reverse.
        """
        order = order_with_address
        payment = _seed_click_payment(db, order)
        self._settle_offline(payment)

        resp = _post_complete(matrix_client, order, payment, click_trans_id="940001", click_paydoc_id="778001")
        assert resp.get_json()["error"] == 0

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.payment_method == PaymentMethod.CLICK, "the Click rail must be restored"
        assert payment.status == PaymentStatus.COMPLETED
        assert Order.query.get(order.id).payment_method == PaymentMethod.CLICK

        txn = PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type="click_late_complete_after_cash_settlement"
        ).one()
        assert txn.success is True

        # The money is booked on the payment, NOT parked as an orphan credit.
        assert CashCollectionEvent.query.filter_by(
            idempotency_key="click-late-debit:940001"
        ).count() == 0

    def test_replayed_late_debit_after_cash_settlement_is_idempotent(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, monkeypatch
    ):
        """A Click re-send must not credit twice, restore twice, or record a
        second transaction."""
        order = order_with_address
        payment = _seed_click_payment(db, order)
        self._settle_offline(payment)

        first = _post_complete(matrix_client, order, payment, click_trans_id="940002", click_paydoc_id="778002")
        second = _post_complete(matrix_client, order, payment, click_trans_id="940002", click_paydoc_id="778002")

        # error 0 is terminal, so the webhook idempotency layer caches it and
        # replays the SAME body (api/payments.py:786) rather than re-running the
        # handler. Either way the contract is: no error, and no second effect.
        assert first.get_json()["error"] == 0
        assert second.status_code == 200
        assert second.get_json()["error"] in (0, -4), "a protocol retry must be an idempotent ack"

        db.session.expire_all()
        assert PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type="click_late_complete_after_cash_settlement"
        ).count() == 1
        assert CashCollectionEvent.query.filter_by(
            idempotency_key="click-late-debit:940002"
        ).count() == 0

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


class TestLateCompleteOnLiveOrderPastPending:
    """Prod incident TG_000413_26 (order 1100 / payment 1204, 2026-08-21).

    The re-fulfil gate was `order.status == OrderStatus.PENDING`. The order was
    CONFIRMED and out for delivery when the genuine late debit landed, so the
    money fell through to `_credit_late_debit`, which labelled a live order
    "cancelled/unfulfillable", parked 54 000 as prepaid credit and left the
    order unpaid and un-fiscalized. The order was DELIVERED 57 minutes later.

    Note the same fact is read in two places and used to mean opposite things:
    `reconcile_pending_payments` treats past-PENDING as "still live, protect it"
    (payment_tasks.py PAY-007 guard), while this gate treated it as "dead".
    """

    @pytest.mark.parametrize("order_status", [
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
    ])
    def test_live_unpaid_order_past_pending_is_fulfilled_not_credited(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization, order_status
    ):
        order = order_with_address
        order.status = order_status
        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.CANCELLED
        payment.failure_reason = "Auto-cancelled: gateway status unknown past timeout"
        db.session.commit()

        resp = _post_complete(matrix_client, order, payment, click_trans_id="920001")

        assert resp.status_code == 200
        assert resp.get_json()["error"] == 0, "a live unpaid order must accept the debit"

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        order = Order.query.get(order.id)

        assert payment.status == PaymentStatus.COMPLETED
        assert payment.failure_reason is None
        assert order.is_paid is True, "the order the customer paid for must read paid"
        assert order.status == order_status, "settling must not move the order backwards"

        # The money must settle the order, NOT become floating customer credit.
        assert CashCollectionEvent.query.filter_by(
            source=CashCollectionSource.BACKFILL
        ).count() == 0, "a live order must never divert the debit to prepaid credit"

    def test_delivered_order_still_takes_the_credit_path(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        """A genuinely dead/settled order keeps the existing credit behaviour —
        the gate widens to LIVE statuses only, it does not become unconditional."""
        order = order_with_address
        order.status = OrderStatus.DELIVERED
        order.is_paid = True
        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.CANCELLED
        db.session.commit()

        resp = _post_complete(matrix_client, order, payment, click_trans_id="920002")

        assert resp.get_json()["error"] == -4
        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.CANCELLED
        assert CashCollectionEvent.query.filter_by(source=CashCollectionSource.BACKFILL).count() == 1

    def test_cancelled_order_still_takes_the_credit_path(
        self, matrix_client, matrix_app, db, order_with_address, no_fiscalization
    ):
        order = order_with_address
        order.status = OrderStatus.CANCELLED
        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.CANCELLED
        db.session.commit()

        resp = _post_complete(matrix_client, order, payment, click_trans_id="920003")

        assert resp.get_json()["error"] == -4
        db.session.expire_all()
        assert CashCollectionEvent.query.filter_by(source=CashCollectionSource.BACKFILL).count() == 1


class TestLateAcceptReReserveIsAllOrNothing:
    """`_accept_late_complete` has the same bare-`except Exception` shape as
    Case C: the debit is genuine, so the payment must end COMPLETED whatever
    the marking-code pool says. Before the all-or-nothing restructure the
    swallowed shortfall still left an earlier line's code committed as
    RESERVED against a payment no cancel-cascade will ever reach."""

    def test_a_short_pool_leaves_no_reservation_but_the_payment_still_completes(
        self, matrix_client, matrix_app, db, order_with_address, sample_product,
        no_fiscalization, two_line_order_with_one_short_pool,
    ):
        from business_app.models.order import OrderItemMarkingCodeAllocation
        from business_app.models.product import ProductMarkingCode
        from shared.enums import MarkingCodeStatus

        order = order_with_address
        product_a, _product_b = two_line_order_with_one_short_pool(order, sample_product)

        payment = _seed_click_payment(db, order)
        payment.status = PaymentStatus.CANCELLED
        payment.failure_reason = "Auto-cancelled: gateway status unknown past timeout"
        db.session.commit()

        resp = _post_complete(matrix_client, order, payment, click_trans_id="910777")
        assert resp.status_code == 200
        assert resp.get_json()["error"] == 0, "a genuine late debit must still be accepted"

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.payment_method == PaymentMethod.CLICK

        assert ProductMarkingCode.query.filter_by(
            product_id=product_a.id, status=MarkingCodeStatus.RESERVED
        ).count() == 0, (
            "product A's code must not be stranded RESERVED by a swallowed shortfall"
        )
        assert ProductMarkingCode.query.filter_by(
            product_id=product_a.id, status=MarkingCodeStatus.AVAILABLE
        ).count() == 1
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=order.id).count() == 0
