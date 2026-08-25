"""B1: a provider-reported cancel must not end a LIVE order's payment.

THE DEFECT. ``PaymentService.update_payment_status`` had ONE branch for
"provider says cancelled/canceled/failed/error": write a terminal status on our
payment row and UNCONDITIONALLY release its reserved marking codes. Click's
``_map_payment_status`` really does produce that (codes -1/-2 -> cancelled,
-3 -> failed), and the branch is reached from two live entry points:

  * ``reconcile_pending_payments`` (payment_tasks.py:169 -> check_payment_status), and
  * ``GET /api/v1/payments/<id>/status`` (api/payments.py:454) — CUSTOMER-FACING,
    so a customer refreshing their own payment page could strip their own live
    order's marking codes.

Two things break when that fires on a live order:

  1. Phase 4A's PREPARE guard (``order_is_payable_online``) requires
     PENDING/PROCESSING. Once CANCELLED is written the customer can NEVER pay
     that link again — on an order they still owe for, under a policy that
     explicitly promises the link stays payable through delivery.
  2. The released codes are re-reserved by ANOTHER order minutes later
     (prod TG_000413_26 / TG_000414_26), so this order is delivered
     un-fiscalizable and someone else's labels print on its receipt.

THE RULE (policy Phase 4D + the owner's 2026-08-24 ruling): a payment's life
ends where the ORDER resolves — cash at the door, payment of the link, or order
cancellation. ONE abandoned Click attempt is not the end of the payment.

WHY THIS FILE EXISTS RATHER THAN A CASE IN AN EXISTING ONE.
``tests/unit/test_reconcile_positive_evidence.py`` monkeypatches
``PaymentService.check_payment_status`` WHOLESALE, so its ``{"status":
"cancelled"}`` case passed for years without ever executing the real cancel.
Every cell below therefore mocks ONLY the outermost gateway seam
(``ClickPaymentProviderService.check_payment_status``) and lets the real
``update_payment_status`` run, asserting on committed DB state.
"""

from decimal import Decimal

import pytest

from business_app.models.order import OrderItem
from business_app.models.payment import Payment, PaymentTransaction
from business_app.models.product import ProductFiscalProfile, ProductMarkingCode
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from business_app.services.payment_service import PaymentService
from tests.unit.test_click_fiscalization_service import (
    _configure_click_fiscal_context,
    _sign_click_payload,
    pending_marked_click_payment,  # noqa: F401  (fixture)
    payment_service,  # noqa: F401  (fixture)
)
from business_app.utils.payment_projection import order_is_payable_online
from shared.enums import MarkingCodeStatus, OrderStatus, PaymentMethod, PaymentStatus


@pytest.fixture
def click_payment_holding_a_code(db, sample_order, sample_product):
    """A PENDING Click payment on ``sample_order`` that really holds a RESERVED
    marking code, reserved through the PRODUCTION reservation path.

    The reservation must go through ``reserve_required_marking_codes`` rather
    than being hand-stamped: ``release_reserved_marking_codes`` discovers what to
    release via ``_codes_currently_held``, which keys on the append-only
    ``OrderItemMarkingCodeAllocation`` ledger. A hand-stamped RESERVED row with
    no ledger event would be invisible to the release, and the scope-boundary
    cell below would pass vacuously.
    """
    sample_order.payment_method = PaymentMethod.CLICK
    sample_order.delivery_fee = Decimal("0.00")
    sample_order.subtotal = Decimal("15000.00")
    sample_order.total_amount = Decimal("15000.00")

    sample_product.barcode = "4780011111111"
    db.session.add(
        ProductFiscalProfile(
            product_id=sample_product.id,
            spic="SPIC-B1",
            package_code="PACK-B1",
            units="1213733",
            vat_percent=Decimal("12.00"),
            fiscalization_enabled=True,
            requires_marking_codes=True,
        )
    )
    db.session.flush()

    order_item = OrderItem(
        order_id=sample_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal("15000.00"),
        total_price=Decimal("15000.00"),
    )
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=Decimal("15000.00"),
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id="click-b1-live-order",
        provider_data={"click": {"click_paydoc_id": "20240101000001"}},
    )
    code = ProductMarkingCode(
        product_id=sample_product.id,
        code="MARK-B1-001\x1dVERIFY-B1-001",
        status=MarkingCodeStatus.AVAILABLE,
    )
    db.session.add_all([order_item, payment, code])
    db.session.commit()

    reserved = PaymentFiscalizationService().reserve_required_marking_codes(payment)
    db.session.commit()
    assert reserved.get("reserved") == 1, f"fixture failed to reserve a code: {reserved}"

    db.session.expire_all()
    code = ProductMarkingCode.query.get(code.id)
    assert code.status == MarkingCodeStatus.RESERVED
    return Payment.query.get(payment.id), code


def _gateway_says(monkeypatch, status: str):
    """Mock ONLY the outermost gateway seam.

    ``PaymentService.update_payment_status`` and ``PaymentService.check_payment_status``
    are deliberately left REAL — mocking them is exactly the blind spot that let
    this defect ship.
    """
    monkeypatch.setattr(
        ClickPaymentProviderService,
        "check_payment_status",
        lambda self, payment: {"status": status, "error_note": "User cancelled", "raw": {}},
    )


@pytest.mark.unit
@pytest.mark.payment
class TestLiveOrderSurvivesAProviderReportedCancel:
    @pytest.mark.parametrize("gateway_status", ["cancelled", "failed"])
    @pytest.mark.parametrize(
        "order_status",
        [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY],
    )
    def test_live_order_keeps_its_payment_and_its_codes(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch, order_status, gateway_status
    ):
        """REQUIRED TESTS 1 & 2: one abandoned Click attempt may not end the payment."""
        payment, code = click_payment_holding_a_code
        sample_order.status = order_status
        db.session.commit()

        _gateway_says(monkeypatch, gateway_status)
        PaymentService().check_payment_status(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        order = payment.order

        assert payment.status == PaymentStatus.PENDING, (
            f"gateway '{gateway_status}' on a {order_status.value} order must NOT end our payment"
        )
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.RESERVED
        assert ProductMarkingCode.query.get(code.id).order_id == order.id
        assert order_is_payable_online(order, payment) is True, (
            "the customer must still be able to PREPARE a fresh attempt on the same link"
        )

    def test_customer_refreshing_their_payment_page_cannot_strip_their_own_codes(
        self, client, auth_headers, db, sample_order, click_payment_holding_a_code, monkeypatch
    ):
        """REQUIRED TEST 5. The service-level cells above prove the rule; this one
        proves the CUSTOMER-FACING route actually reaches it.

        ``GET /payments/<id>/status`` (api/payments.py:454) calls
        ``update_payment_status`` on any PENDING payment with no guard of its own,
        so it is a second, unauthenticated-by-policy trigger for the same branch —
        and the one a real customer hits by pressing refresh. A service-only test
        would still pass if someone later re-inlined the cancel at this call site.
        """
        payment, code = click_payment_holding_a_code
        sample_order.status = OrderStatus.OUT_FOR_DELIVERY
        db.session.commit()

        _gateway_says(monkeypatch, "cancelled")
        response = client.get(f"/api/v1/payments/{payment.id}/status", headers=auth_headers)

        assert response.status_code == 200, response.get_json()
        assert response.get_json()["data"]["payment"]["status"] == PaymentStatus.PENDING.value

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.PENDING
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.RESERVED


@pytest.mark.unit
@pytest.mark.payment
class TestScopeBoundaryTheBranchIsNarrowedNotRemoved:
    @pytest.mark.parametrize(
        "order_status,expected_payment_status",
        [
            (OrderStatus.CANCELLED, PaymentStatus.CANCELLED),
            (OrderStatus.RETURNED, PaymentStatus.CANCELLED),
        ],
    )
    def test_dead_order_still_cancels_and_still_releases(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch, order_status, expected_payment_status
    ):
        """REQUIRED TEST 3: proves the fix NARROWED the branch rather than deleting it.

        The order has resolved, so there is nothing left to pay for and nothing
        left to fiscalize — the codes must go back to the pool for another order,
        which is the whole reason the release exists.
        """
        payment, code = click_payment_holding_a_code
        sample_order.status = order_status
        db.session.commit()

        _gateway_says(monkeypatch, "cancelled")
        PaymentService().check_payment_status(payment.id)

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == expected_payment_status
        assert Payment.query.get(payment.id).failure_reason == "User cancelled"
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.AVAILABLE
        assert ProductMarkingCode.query.get(code.id).order_id is None

    @pytest.mark.parametrize(
        "order_status",
        [OrderStatus.CONFIRMED, OrderStatus.OUT_FOR_DELIVERY],
    )
    def test_a_live_but_already_PAID_order_counts_as_RESOLVED(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch, order_status
    ):
        """🔶 PINNED so the boundary is visible — see the report's Fix round 1.

        An order can be live AND paid: settled on another rail (a completed
        sibling payment, a business-account settlement, an admin prepayment
        credit) while a stale Click attempt stays PENDING. The ruling defines
        "resolved" as paid OR dead, so this shape cancels and releases.

        What defuses it: ``release_reserved_marking_codes`` is PAYMENT-scoped —
        ``_codes_currently_held`` keys on this payment's own allocation ledger
        rows — so it frees only the codes THIS stale attempt reserved, not the
        ones held by whichever payment actually settled the order. In the common
        shapes those are a duplicate reservation and freeing them is correct.
        """
        payment, code = click_payment_holding_a_code
        sample_order.status = order_status
        sample_order.is_paid = True
        db.session.commit()

        _gateway_says(monkeypatch, "cancelled")
        PaymentService().check_payment_status(payment.id)

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.CANCELLED
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.AVAILABLE

    def test_delivered_and_PAID_order_has_RESOLVED_so_the_release_still_fires(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch
    ):
        """THE RESOLVED-ORDER BOUNDARY: delivered AND settled (cash at the door).

        Nothing further is owed and nothing further will be delivered, so a stale
        PENDING Click attempt on this order is genuinely dead: cancel it and give
        its codes back to the pool. This is the cell that proves "resolved" is not
        a synonym for "dead status" — a DELIVERED order resolves by being PAID.
        """
        payment, code = click_payment_holding_a_code
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = True
        db.session.commit()

        _gateway_says(monkeypatch, "cancelled")
        PaymentService().check_payment_status(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.CANCELLED
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.AVAILABLE
        assert order_is_payable_online(payment.order, payment) is False


@pytest.mark.unit
@pytest.mark.payment
class TestCaseBTheLinkAndTheCodesSurviveDelivery:
    """Policy case B — the population Phase 4 was built to serve.

    The customer took delivery and did NOT pay the driver. The order keeps the
    Click rail and a live payable link so the money can still arrive and the
    receipt can still be issued. The header table's promise is explicit: **link
    stays payable, codes retained**.

    DELIVERED is not in ``LIVE_ORDER_STATUSES``, so a guard written as
    ``order_is_live`` would still cancel here — the same permanent lockout as B1,
    one status further on, and on the exact population the policy exists for.
    That is why the guard asks "has this ORDER resolved?" rather than "is this
    order pre-delivery?".
    """

    @pytest.mark.parametrize("gateway_status", ["cancelled", "failed"])
    def test_delivered_unpaid_order_keeps_its_payment_and_its_codes(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch, gateway_status
    ):
        payment, code = click_payment_holding_a_code
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = False
        db.session.commit()

        _gateway_says(monkeypatch, gateway_status)
        PaymentService().check_payment_status(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        order = payment.order

        assert payment.status == PaymentStatus.PENDING, (
            f"case B: gateway '{gateway_status}' must not end the payment of a "
            "delivered order the customer still owes for"
        )
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.RESERVED, (
            "case B's contract is explicitly 'codes retained' — the receipt has "
            "still to be issued when the money lands"
        )
        assert ProductMarkingCode.query.get(code.id).order_id == order.id
        assert order_is_payable_online(order, payment) is True, (
            "the customer must still be able to settle a delivered debt by link"
        )


# --------------------------------------------------------------------------- #
# The SECOND expression of the same decision — the Click COMPLETE callback.
# --------------------------------------------------------------------------- #

@pytest.mark.unit
@pytest.mark.payment
class TestHandleCompleteObeysTheSameRule:
    """`handle_complete` carries the same gateway fact on the BUSIER path.

    B1's guard originally covered only `update_payment_status` — the 15-minute
    reconcile poll and the customer-facing GET. But a declined card arrives at
    `handle_complete` as `error != 0`, and that branch cancelled the payment and
    freed its codes with no order-state test at all. The terminal short-circuit
    above it only catches {COMPLETED, CANCELLED, FAILED}, so a PENDING payment on
    a live order fell straight through.

    Left unguarded, the SAME gateway fact produced opposite outcomes depending on
    which carrier arrived first — "keep the payment and its codes" via the poll,
    "kill both" via the callback — and which won was a race.

    These cells drive the REAL webhook: a real signed PREPARE reserves the codes,
    then a real signed COMPLETE declines.
    """

    def _signed_pair(self, provider, payment, *, error, error_note, click_paydoc_id="9988776655"):
        prepare = _sign_click_payload(provider, {
            "click_trans_id": "txn-b1-decline",
            "service_id": provider.service_id or "1",
            "merchant_trans_id": payment.order.order_number,
            "amount": str(payment.amount),
            "action": "0",
            "sign_time": "1700000001",
            "error": "0",
            "error_note": "Success",
        })
        complete = _sign_click_payload(provider, {
            "click_trans_id": "txn-b1-decline",
            "service_id": provider.service_id or "1",
            "merchant_trans_id": payment.order.order_number,
            "merchant_prepare_id": payment.id,
            "click_paydoc_id": click_paydoc_id,
            "amount": str(payment.amount),
            "action": "1",
            "sign_time": "1700000002",
            "error": error,
            "error_note": error_note,
        })
        return prepare, complete

    def _provider(self, app, payment_service):
        _configure_click_fiscal_context(app)
        app.config["CLICK_SHOP_SECRET_KEY"] = "click-secret"
        app.config["CLICK_TEST_MODE"] = True
        return ClickPaymentProviderService(payment_service=payment_service)

    def test_a_declined_card_on_a_LIVE_order_keeps_the_payment_and_its_codes(
        self, app, db, payment_service, pending_marked_click_payment
    ):
        """THE CELL THAT MATTERS: customer opens the link on a CONFIRMED order,
        PREPARE reserves the codes, the card is declined, Click sends error=-1.

        Before the guard we wrote CANCELLED and handed the codes back to the
        pool. The customer then taps the same link, PREPARE consults
        `order_is_payable_online`, sees a CANCELLED payment and answers -9:
        permanent lockout on an order they still owe for. Meanwhile the freed
        codes are re-reservable by another order and printable on ITS receipt.
        That is the TG_000413_26 / TG_000414_26 mechanism verbatim.
        """
        payment, marking_code = pending_marked_click_payment
        payment.order.status = OrderStatus.CONFIRMED
        db.session.commit()

        provider = self._provider(app, payment_service)
        prepare, complete = self._signed_pair(
            provider, payment, error="-1", error_note="Cancelled by Click"
        )

        provider.handle_prepare(prepare)
        db.session.refresh(marking_code)
        assert marking_code.status == MarkingCodeStatus.RESERVED

        response = provider.handle_complete(complete)

        db.session.refresh(payment)
        db.session.refresh(marking_code)

        # The protocol answer is unchanged — this transaction really was declined.
        assert response["error"] == -9
        # ...but OUR payment and OUR inventory survive it.
        assert payment.status == PaymentStatus.PENDING, (
            "a declined attempt on a live order must not end the payment"
        )
        assert marking_code.status == MarkingCodeStatus.RESERVED
        assert order_is_payable_online(payment.order, payment) is True, (
            "the customer must still be able to retry the same link"
        )

    def test_the_two_carriers_of_the_same_gateway_fact_now_AGREE(
        self, app, db, payment_service, pending_marked_click_payment, monkeypatch
    ):
        """The incoherence B1 would otherwise have created, pinned.

        One gateway fact — "this Click transaction was cancelled" — arriving by
        callback and by poll must produce the same outcome. Before this round the
        poll kept the payment and the callback killed it, and which won was a
        race.
        """
        payment, marking_code = pending_marked_click_payment
        payment.order.status = OrderStatus.CONFIRMED
        db.session.commit()

        provider = self._provider(app, payment_service)
        prepare, complete = self._signed_pair(
            provider, payment, error="-1", error_note="Cancelled by Click"
        )
        provider.handle_prepare(prepare)
        provider.handle_complete(complete)
        db.session.refresh(payment)
        via_callback = (payment.status, ProductMarkingCode.query.get(marking_code.id).status)

        # Same fact, other carrier.
        _gateway_says(monkeypatch, "cancelled")
        PaymentService().check_payment_status(payment.id)
        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        via_poll = (payment.status, ProductMarkingCode.query.get(marking_code.id).status)

        assert via_callback == via_poll == (PaymentStatus.PENDING, MarkingCodeStatus.RESERVED)

    # --- the two MALFORMED-PAYLOAD sites: ruled the same way, and why -------- #

    def _complete_only(self, provider, payment, overrides):
        base = {
            "click_trans_id": "txn-b1-malformed",
            "service_id": provider.service_id or "1",
            "merchant_trans_id": payment.order.order_number,
            "merchant_prepare_id": payment.id,
            "click_paydoc_id": "9988776655",
            "amount": str(payment.amount),
            "action": "1",
            "sign_time": "1700000002",
            "error": "0",
            "error_note": "Success",
        }
        base.update(overrides)
        signed = _sign_click_payload(provider, base)
        # `error` and `click_paydoc_id` are NOT part of Click's signature, so a
        # payload may be signed correctly and still be malformed — which is
        # exactly the shape these two branches exist to reject.
        for key, value in overrides.items():
            if value is None:
                signed.pop(key, None)
        return signed

    @pytest.mark.parametrize(
        "overrides,expected_error",
        [
            ({"error": None}, -8),                 # :519  missing error code
            ({"click_paydoc_id": ""}, -8),         # :617  success without identifiers
        ],
        ids=["missing_error_code", "success_without_identifiers"],
    )
    def test_a_MALFORMED_payload_on_a_live_order_also_keeps_the_payment_and_codes(
        self, app, db, payment_service, pending_marked_click_payment, overrides, expected_error
    ):
        """RULING on the two protocol-violation branches: SAME gate.

        The reviewer's read was that these are protocol violations rather than
        abandoned attempts, so possibly legitimately different. I ruled they are
        not — they warrant the gate MORE, not less:

        * A payload whose ``error`` field will not parse tells us NOTHING about
          the transaction. That is strictly less evidence than an affirmative
          cancel. If "cancelled" may not end a live order's payment, "we could
          not read this" certainly may not — that is acting on the absence of
          evidence, and it is the same positive-evidence contract the reconcile
          path already follows ("unknown/ambiguous => leave PENDING").
        * A payload CLAIMING success without its identifiers is worse still: the
          money may genuinely have moved. Cancelling and freeing the codes there
          is the TG_000413_26 shape with a real debit behind it.

        The counter-argument — that a broken integration would leak reserved
        inventory indefinitely — does not hold: the codes are freed when the
        ORDER resolves, so the order's own lifecycle collects them. Nothing
        leaks; it is merely deferred to the event that actually settles it.

        What deliberately does NOT change: the protocol response and the audit
        record. We still answer -8 (asserted below), and we still write the
        failed transaction row (asserted by
        ``test_the_audit_row_is_written_even_when_the_gate_SUPPRESSES_the_cancel``,
        which covers all three branches). The gate governs our payment
        lifecycle, not our reply to Click.
        """
        payment, marking_code = pending_marked_click_payment
        payment.order.status = OrderStatus.CONFIRMED
        db.session.commit()

        provider = self._provider(app, payment_service)
        prepare, _ = self._signed_pair(provider, payment, error="0", error_note="Success")
        provider.handle_prepare(prepare)
        db.session.refresh(marking_code)
        assert marking_code.status == MarkingCodeStatus.RESERVED

        response = provider.handle_complete(self._complete_only(provider, payment, overrides))

        db.session.refresh(payment)
        db.session.refresh(marking_code)
        assert response["error"] == expected_error, "the protocol answer must be unchanged"
        assert payment.status == PaymentStatus.PENDING
        assert marking_code.status == MarkingCodeStatus.RESERVED
        assert order_is_payable_online(payment.order, payment) is True

    @pytest.mark.parametrize(
        "overrides,expected_type",
        [
            ({"error": None}, "click_complete_invalid_payload"),
            ({"click_paydoc_id": ""}, "click_complete_invalid_success"),
            ({"error": "-1"}, "click_complete_cancelled"),
        ],
        ids=["missing_error_code", "success_without_identifiers", "affirmative_cancel"],
    )
    def test_the_audit_row_is_written_even_when_the_gate_SUPPRESSES_the_cancel(
        self, app, db, payment_service, pending_marked_click_payment, overrides, expected_type
    ):
        """The gate must not swallow the audit trail.

        All three `_record_transaction` calls sit OUTSIDE `if may_end` on
        purpose: the callback really arrived and really failed, and that is worth
        recording whatever we decided about the payment. Nothing but this test
        stops a future refactor tidying one of them inside the gate — at which
        point a suppressed cancel would leave no trace at all, and the next
        incident would have nothing to reconstruct from.
        """
        payment, marking_code = pending_marked_click_payment
        payment.order.status = OrderStatus.CONFIRMED
        db.session.commit()

        provider = self._provider(app, payment_service)
        prepare, _ = self._signed_pair(provider, payment, error="0", error_note="Success")
        provider.handle_prepare(prepare)

        provider.handle_complete(self._complete_only(provider, payment, overrides))
        db.session.commit()

        db.session.refresh(payment)
        # The gate suppressed the cancel...
        assert payment.status == PaymentStatus.PENDING
        assert ProductMarkingCode.query.get(marking_code.id).status == MarkingCodeStatus.RESERVED
        # ...and the audit row was still written.
        rows = PaymentTransaction.query.filter_by(
            payment_id=payment.id, transaction_type=expected_type
        ).all()
        assert len(rows) == 1, f"expected exactly one {expected_type} row, got {len(rows)}"
        assert rows[0].success is False
        assert rows[0].status == "cancelled"

    @pytest.mark.parametrize(
        "overrides",
        [{"error": None}, {"click_paydoc_id": ""}, {"error": "-1"}],
        ids=["missing_error_code", "success_without_identifiers", "affirmative_cancel"],
    )
    def test_every_branch_still_cancels_and_releases_once_the_ORDER_has_resolved(
        self, app, db, payment_service, pending_marked_click_payment, overrides
    ):
        """Scope boundary for all three callback branches: narrowed, not removed."""
        payment, marking_code = pending_marked_click_payment
        provider = self._provider(app, payment_service)
        prepare, _ = self._signed_pair(provider, payment, error="0", error_note="Success")
        provider.handle_prepare(prepare)
        db.session.refresh(marking_code)
        assert marking_code.status == MarkingCodeStatus.RESERVED

        payment.order.status = OrderStatus.CANCELLED
        db.session.commit()

        provider.handle_complete(self._complete_only(provider, payment, overrides))

        db.session.refresh(payment)
        db.session.refresh(marking_code)
        assert payment.status == PaymentStatus.CANCELLED
        assert marking_code.status == MarkingCodeStatus.AVAILABLE


# --------------------------------------------------------------------------- #
# The THIRD expression — and the one that defeats the guard inside one request.
# --------------------------------------------------------------------------- #

@pytest.mark.unit
@pytest.mark.payment
class TestVerificationTaskObeysTheSameRule:
    """`process_payment_verification` overwrote the guard's own decision.

    The sequence was self-defeating, all inside a single request:

        verify_payment -> check_payment_status -> update_payment_status
          -> B1's guard CORRECTLY refuses to end the payment (stays PENDING)
        -> verify_payment sees "not COMPLETED" and returns {"success": False}
        -> the task's else-branch writes FAILED anyway, and commits.

    `PaymentService.verify_payment` returns success=False for ANY non-COMPLETED
    payment, so a perfectly healthy PENDING Click payment on a live order is
    "verification failed". FAILED fails `order_is_payable_online`, so the next
    PREPARE answers -9 — the same permanent lockout B1 exists to prevent, on the
    same population, triggered by the customer's own
    `POST /api/v1/payments/<id>/verify`.

    Gated rather than deleted: see the report's Fix round 3.
    """

    def test_a_live_unpaid_order_survives_a_verification_sweep(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch
    ):
        """RED cell: PENDING must survive its own verification request."""
        from business_app.tasks.payment_tasks import process_payment_verification

        payment, code = click_payment_holding_a_code
        sample_order.status = OrderStatus.CONFIRMED
        db.session.commit()

        # Gateway still says "pending" — nothing has gone wrong at all.
        _gateway_says(monkeypatch, "pending")
        process_payment_verification.run(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.PENDING, (
            "a healthy PENDING payment is not a FAILED one; verification must not "
            "invent a terminal status for an order that is still live and unpaid"
        )
        assert payment.failure_reason is None
        assert ProductMarkingCode.query.get(code.id).status == MarkingCodeStatus.RESERVED
        assert order_is_payable_online(payment.order, payment) is True

    def test_a_gateway_cancel_during_verification_also_leaves_it_payable(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch
    ):
        """The compound case: the guard refuses, and the task must not undo it."""
        from business_app.tasks.payment_tasks import process_payment_verification

        payment, _code = click_payment_holding_a_code
        sample_order.status = OrderStatus.OUT_FOR_DELIVERY
        db.session.commit()

        _gateway_says(monkeypatch, "cancelled")
        process_payment_verification.run(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.PENDING
        assert order_is_payable_online(payment.order, payment) is True

    def test_case_B_delivered_unpaid_also_survives_verification(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch
    ):
        from business_app.tasks.payment_tasks import process_payment_verification

        payment, _code = click_payment_holding_a_code
        sample_order.status = OrderStatus.DELIVERED
        sample_order.is_paid = False
        db.session.commit()

        _gateway_says(monkeypatch, "pending")
        process_payment_verification.run(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.PENDING
        assert order_is_payable_online(payment.order, payment) is True

    @pytest.mark.parametrize(
        "order_status,is_paid",
        [(OrderStatus.CANCELLED, False), (OrderStatus.DELIVERED, True)],
        ids=["order_dead", "order_paid"],
    )
    def test_a_RESOLVED_order_still_records_the_verification_failure(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch, order_status, is_paid
    ):
        """Scope boundary: narrowed, not removed. Once the order has resolved a
        failed verification is real news and still gets written."""
        from business_app.tasks.payment_tasks import process_payment_verification

        payment, _code = click_payment_holding_a_code
        sample_order.status = order_status
        sample_order.is_paid = is_paid
        db.session.commit()

        _gateway_says(monkeypatch, "pending")
        process_payment_verification.run(payment.id)

        db.session.expire_all()
        assert Payment.query.get(payment.id).status == PaymentStatus.FAILED

    def test_verification_never_overwrites_an_ALREADY_TERMINAL_payment(
        self, db, sample_order, click_payment_holding_a_code, monkeypatch
    ):
        """The missing `status == PENDING` precondition.

        Writing a terminal status over an already-terminal one is its own bug: a
        COMPLETED payment must never be downgraded to FAILED by a stray
        verification, which would strand real money.
        """
        from business_app.tasks.payment_tasks import process_payment_verification

        payment, _code = click_payment_holding_a_code
        sample_order.status = OrderStatus.CANCELLED
        # CANCELLED, not COMPLETED, on purpose: a COMPLETED payment would take
        # the task's SUCCESS branch and pass this cell without ever exercising
        # the precondition. A terminal-but-not-completed payment is the shape
        # that actually reaches the else-branch and gets overwritten.
        payment.status = PaymentStatus.CANCELLED
        payment.failure_reason = "Cancelled at the door"
        db.session.commit()

        _gateway_says(monkeypatch, "pending")
        process_payment_verification.run(payment.id)

        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert payment.status == PaymentStatus.CANCELLED
        assert payment.failure_reason == "Cancelled at the door", (
            "the original terminal reason must survive; overwriting it loses why "
            "the payment actually ended"
        )
