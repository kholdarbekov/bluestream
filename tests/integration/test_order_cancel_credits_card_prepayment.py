"""B4a — a cancelled card/Click order settles as prepaid customer balance.

THE OWNER'S RULE, verbatim (2026-08-24):

    "the payment that is done via click/card is non-returnable. We don't return
     the payment. The reason is we can't undo fiscalization once we submit it.
     Cancelling payment only makes chaos in the payment vs fiscalization. So our
     final business logic is we never ever cancel card / click paid payments. We
     can cancel the order itself, and in that case the payment will settle as
     prepaid customer balance."

So after an order cancellation on a paid CLICK/CARD order:

  * the gateway is NEVER contacted — no ``DELETE /payment/reversal``;
  * ``payment.status`` stays COMPLETED and ``order.is_paid`` stays True (a
    cancelled-but-paid order is the honest description, and the rule requires
    it — a CANCELLED payment row holding real money IS the payment-vs-
    fiscalization divergence the rule exists to prevent);
  * the money reappears as a customer prepaid-credit ``CashCollectionEvent``;
  * still-RESERVED marking codes go back to the pool, USED ones do not;
  * the fiscalization record is parked at NOT_REQUIRED unless a receipt was
    already filed, in which case it is left strictly alone.

That NOT_REQUIRED write is the single most dangerous item in B4 and the reason
:class:`TestTheFiscalizationBrake` exists. Until B4, ``process_refund`` writing
``status = CANCELLED`` was the ONLY thing keeping a cancelled order out of
``reconcile_completed_payment_side_effects`` (payment_tasks.py) — which sweeps
COMPLETED CLICK/CARD payments for SEVEN DAYS with no order filter, and whose
``process_click_fiscalization`` draws FRESH marking codes and files a tax
receipt. B4 stops writing CANCELLED, so the brake has to become explicit.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import OrderItem
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    Payment,
    PaymentFiscalization,
)
from business_app.models.product import ProductFiscalProfile, ProductMarkingCode
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from shared.enums import (
    FiscalizationStatus,
    MarkingCodeStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)

CANCEL_URL = "/api/v1/orders/{order_id}/cancel"


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _credit_events(user_id):
    """Every live cash-collection event belonging to this customer."""
    return (
        CashCollectionEvent.query.filter(
            CashCollectionEvent.customer_id == user_id,
            CashCollectionEvent.voided_at.is_(None),
        )
        .order_by(CashCollectionEvent.id)
        .all()
    )


def _total_credited(user_id):
    return sum((Decimal(str(e.amount or 0)) for e in _credit_events(user_id)), Decimal("0.00"))


@pytest.fixture
def paid_click_order(app, db, sample_order, sample_product, sample_address):
    """A CONFIRMED order paid in full by Click, really holding a RESERVED code.

    The reservation goes through ``reserve_required_marking_codes`` rather than
    being hand-stamped, because ``release_reserved_marking_codes`` discovers what
    to release through the append-only ``OrderItemMarkingCodeAllocation`` ledger
    (``_codes_currently_held``). A hand-stamped RESERVED row would be invisible
    to the release and the marking-code assertions would pass vacuously.
    """
    sample_order.delivery_address_id = sample_address.id
    sample_order.payment_method = PaymentMethod.CLICK
    sample_order.subtotal = Decimal("15000.00")
    sample_order.delivery_fee = Decimal("0.00")
    sample_order.total_amount = Decimal("15000.00")
    sample_order.status = OrderStatus.CONFIRMED

    sample_product.barcode = "4780011111111"
    db.session.add(
        ProductFiscalProfile(
            product_id=sample_product.id,
            spic="SPIC-B4",
            package_code="PACK-B4",
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
        amount_collected=Decimal("15000.00"),
        outstanding_amount=Decimal("0.00"),
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        payment_id="click-b4-paid",
        provider_transaction_id="click-tx-b4",
        provider_data={"click": {"click_paydoc_id": "20240101000004"}},
    )
    code = ProductMarkingCode(
        product_id=sample_product.id,
        code="MARK-B4-001\x1dVERIFY-B4-001",
        status=MarkingCodeStatus.AVAILABLE,
    )
    db.session.add_all([order_item, payment, code])
    db.session.commit()

    reserved = PaymentFiscalizationService().reserve_required_marking_codes(payment)
    db.session.commit()
    assert reserved.get("reserved") == 1, f"fixture failed to reserve a code: {reserved}"

    payment.paid_at = payment.created_at
    sample_order.is_paid = True
    sample_order.paid_at = payment.created_at
    db.session.commit()

    db.session.expire_all()
    return sample_order, Payment.query.get(payment.id), ProductMarkingCode.query.get(code.id)


@pytest.fixture
def gateway_is_a_landmine(monkeypatch):
    """Any attempt to reverse money at the gateway fails the test loudly.

    ``PaymentService.process_refund`` is the ONE door to a Click reversal from
    an order cancellation, so trapping it is exactly the "the gateway was never
    contacted" assertion — and it traps the CLICK_TEST_MODE short-circuit too,
    which a ``merchant_request`` patch would sail straight past.
    """
    from business_app.services.payment_service import PaymentService

    def _boom(self, *args, **kwargs):  # pragma: no cover - the failure IS the point
        raise AssertionError(
            "process_refund was reached from an order cancellation: the owner's rule "
            "says a card/Click payment is NEVER reversed."
        )

    monkeypatch.setattr(PaymentService, "process_refund", _boom, raising=True)


@pytest.mark.integration
@pytest.mark.payment
class TestCancelPaidClickOrderSettlesAsPrepaidCredit:
    def test_customer_cancel_credits_the_money_and_never_calls_the_gateway(
        self, app, client, db, paid_click_order, sample_user, gateway_is_a_landmine
    ):
        order, payment, code = paid_click_order

        response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))
        assert response.status_code == 200, response.get_json()

        db.session.expire_all()
        order = type(order).query.get(order.id)
        payment = Payment.query.get(payment.id)
        code = ProductMarkingCode.query.get(code.id)

        # The money came back as customer prepaid credit, in full.
        events = _credit_events(sample_user.id)
        assert len(events) == 1, f"expected exactly one credit event, got {[e.to_dict() for e in events]}"
        assert Decimal(str(events[0].amount)) == Decimal("15000.00")
        assert Decimal(str(events[0].unapplied_amount)) == Decimal("15000.00")
        assert (events[0].proof_data or {}).get("flow") == "order_cancel_prepaid_credit"

        # The payment is untouched: the bank really took this money.
        assert payment.status is PaymentStatus.COMPLETED
        assert Decimal(str(payment.amount_collected)) == Decimal("15000.00")

        # A cancelled, PAID order — the honest description the rule requires.
        assert order.status is OrderStatus.CANCELLED
        assert order.is_paid is True

        # Unissued codes go back to the pool.
        assert code.status is MarkingCodeStatus.AVAILABLE

        # And the brake is on.
        fiscalization = PaymentFiscalization.query.filter_by(payment_id=payment.id).first()
        assert fiscalization is not None
        assert fiscalization.status is FiscalizationStatus.NOT_REQUIRED

    def test_cancelling_twice_credits_exactly_once(
        self, app, client, db, paid_click_order, sample_user, gateway_is_a_landmine
    ):
        """Both defences at once: ``_claim_status_transition`` and the
        ``order-cancel-credit:{payment.id}`` idempotency key."""
        from business_app.services.order_service import OrderService

        order, payment, _code = paid_click_order

        first = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))
        assert first.status_code == 200, first.get_json()

        second = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))
        assert second.status_code == 400, "a second cancel is refused, not re-settled"

        # And the direct service call an admin dropdown would make.
        with app.app_context():
            try:
                OrderService().update_order_status(order.id, OrderStatus.CANCELLED)
            except Exception:
                pass

        db.session.expire_all()
        assert len(_credit_events(sample_user.id)) == 1
        assert _total_credited(sample_user.id) == Decimal("15000.00")


@pytest.mark.integration
@pytest.mark.payment
class TestTheFiscalizationBrake:
    def test_seven_day_sweep_does_not_requeue_a_cancelled_order(
        self, app, client, db, paid_click_order, sample_user, gateway_is_a_landmine, monkeypatch
    ):
        from business_app.tasks import payment_tasks

        order, payment, code = paid_click_order

        response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))
        assert response.status_code == 200, response.get_json()

        requeued = []
        monkeypatch.setattr(
            payment_tasks.process_click_fiscalization_task,
            "delay",
            lambda payment_id: requeued.append(payment_id),
            raising=True,
        )
        reserved_calls = []
        monkeypatch.setattr(
            PaymentFiscalizationService,
            "reserve_required_marking_codes",
            lambda self, p, **kw: reserved_calls.append(p.id),
            raising=True,
        )

        with app.app_context():
            counts = payment_tasks.reconcile_completed_payment_side_effects()

        assert requeued == [], f"the sweep re-queued a cancelled order: {counts}"
        assert reserved_calls == [], "the sweep drew fresh marking codes for a cancelled order"
        assert counts["fiscalization_requeued"] == 0
        assert counts["confirmation_redispatched"] == 0, (
            "a cancelled order must not get a 'payment confirmed' notification"
        )

        db.session.expire_all()
        assert ProductMarkingCode.query.get(code.id).status is MarkingCodeStatus.AVAILABLE

    def test_an_already_queued_task_landing_after_the_cancel_reserves_nothing(
        self, app, client, db, paid_click_order, sample_user, gateway_is_a_landmine, monkeypatch
    ):
        """A ``process_click_fiscalization_task`` queued at payment-completion
        time and delivered after the cancel. Only the PREDICATE stops it — the
        NOT_REQUIRED status alone does not, because the task re-reads the row
        and walks straight past a non-terminal status into
        ``reserve_required_marking_codes``."""
        order, payment, _code = paid_click_order

        response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))
        assert response.status_code == 200, response.get_json()

        reserved_calls = []
        monkeypatch.setattr(
            PaymentFiscalizationService,
            "reserve_required_marking_codes",
            lambda self, p, **kw: reserved_calls.append(p.id),
            raising=True,
        )

        with app.app_context():
            fiscalization = PaymentFiscalizationService().process_click_fiscalization(payment.id)

        assert fiscalization.status is FiscalizationStatus.NOT_REQUIRED
        assert reserved_calls == [], "fresh marking codes were drawn for a cancelled order"

    def test_a_filed_receipt_survives_a_return_untouched(
        self, app, db, paid_click_order, sample_user, gateway_is_a_landmine
    ):
        """The receipt stands. ``queue_click_fiscalization`` must never downgrade
        a COMPLETED record, and USED codes never go back to the pool."""
        from business_app.services.order_service import OrderService

        order, payment, code = paid_click_order

        order.status = OrderStatus.OUT_FOR_DELIVERY
        code.status = MarkingCodeStatus.USED
        fiscalization = PaymentFiscalizationService().ensure_fiscalization_record(payment)
        fiscalization.status = FiscalizationStatus.COMPLETED
        fiscalization.provider_receipt_url = "https://ofd.example/receipt/1"
        db.session.commit()

        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.RETURNED)

        db.session.expire_all()
        assert PaymentFiscalization.query.filter_by(payment_id=payment.id).first().status is (
            FiscalizationStatus.COMPLETED
        )
        assert ProductMarkingCode.query.get(code.id).status is MarkingCodeStatus.USED
        # The money still settles as credit — the receipt being filed changes
        # nothing about who is owed the money.
        assert _total_credited(sample_user.id) == Decimal("15000.00")


@pytest.mark.integration
@pytest.mark.payment
class TestTheAmountIsNettedNotGross:
    def test_an_edit_down_credit_is_not_paid_a_second_time(
        self, app, db, paid_click_order, sample_user, admin_user, gateway_is_a_landmine
    ):
        """``_cascade_cash`` already handed 5,000 back as credit when the order
        was edited down. Crediting ``amount_collected`` gross at cancel time
        would pay the customer 20,000 for a 15,000 charge."""
        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.order_service import OrderService
        from shared.enums import CashCollectionSource

        order, payment, _code = paid_click_order

        with app.app_context():
            CashCollectionService().post_collection(
                customer_id=order.user_id,
                amount=Decimal("5000.00"),
                source=CashCollectionSource.ADMIN_ADJUSTMENT,
                recorded_by_user_id=admin_user.id,
                order_id=order.id,
                notes="Order edit refund: total dropped by 5000.00",
                proof_data={"flow": "order_edit_refund", "payment_id": payment.id, "order_id": order.id},
                idempotency_key=f"order_edit_refund:{order.id}:5000.00",
            )

        db.session.expire_all()
        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.CANCELLED)

        db.session.expire_all()
        assert _total_credited(sample_user.id) == Decimal("15000.00"), (
            "the gateway took 15,000 once; the customer must be credited 15,000 once"
        )

    def test_a_repriced_up_card_order_with_no_door_cash_leaves_no_phantom_receivable(
        self, app, db, paid_click_order, sample_user, gateway_is_a_landmine
    ):
        """NEW-1 — the dead-order projection re-assertion must not sit behind
        `if cash_allocations:`.

        THE SHAPE THIS MODULE EXISTS FOR (prod order 961): a 15,000 Click order,
        driver adds a line at the door, `_recompute_totals` writes
        `amount=20,000 / PARTIALLY_PAID / outstanding=5,000 / is_paid=False`, and
        the customer then changes their mind and cancels from OUT_FOR_DELIVERY
        (explicitly supported by `cancel_order`). No door cash was ever
        collected, so there are no allocations to reverse — and with the
        re-assertion nested one `if` too deep, nothing cleans the projection.

        The payment is left PARTIALLY_PAID with `outstanding_amount = 5,000` and
        `is_paid = False` on a CANCELLED order: a phantom receivable, and a
        direct contradiction of the rule this change enforces. It is kept out of
        the allocators today only by the `Order.status == DELIVERED` conjunct
        that every `open_receivable_clause()` call site must remember to add —
        the tripwire this file's own docstring warns about. Do not leave a fresh
        instance of that mine three lines from being defused.
        """
        from business_app.services.order_service import OrderService

        order, payment, _code = paid_click_order

        # Reprice upward at the door, exactly as `_recompute_totals` does.
        order.status = OrderStatus.OUT_FOR_DELIVERY
        order.total_amount = Decimal("20000.00")
        payment.amount = Decimal("20000.00")
        payment.amount_collected = Decimal("15000.00")
        payment.outstanding_amount = Decimal("5000.00")
        payment.status = PaymentStatus.PARTIALLY_PAID
        payment.paid_at = None
        order.is_paid = False
        order.paid_at = None
        db.session.commit()

        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.CANCELLED)

        db.session.expire_all()
        # The gateway money still becomes credit...
        assert _total_credited(sample_user.id) == Decimal("15000.00")

        # ...and NO phantom receivable is left behind on a dead order.
        refreshed = Payment.query.get(payment.id)
        assert Decimal(str(refreshed.outstanding_amount)) == Decimal("0.00"), (
            "a CANCELLED order must not owe money"
        )
        assert refreshed.status is PaymentStatus.COMPLETED
        assert Decimal(str(refreshed.amount)) == Decimal("15000.00"), (
            "amount is reduced to what is actually held, so any later re-projection stays at 0"
        )
        assert type(order).query.get(order.id).is_paid is True

    def test_only_the_gateway_portion_is_credited_when_door_cash_funded_part(
        self, app, db, paid_click_order, sample_user, delivery_driver, gateway_is_a_landmine
    ):
        """An edited-up card order whose delta was paid with door cash. That cash
        is ALREADY booked as an allocation on this payment; re-crediting it would
        hand the customer money the gateway never took."""
        from business_app.services.order_service import OrderService

        order, payment, _code = paid_click_order

        # Reprice upward, then settle the delta in place with door cash.
        payment.amount = Decimal("20000.00")
        payment.amount_collected = Decimal("20000.00")
        payment.outstanding_amount = Decimal("0.00")
        payment.status = PaymentStatus.COMPLETED
        order.total_amount = Decimal("20000.00")
        from shared.enums import CashCollectionSource

        event = CashCollectionEvent(
            event_id="CCE-B4-DOOR-CASH",
            customer_id=order.user_id,
            collector_user_id=delivery_driver.id,
            source=CashCollectionSource.DELIVERY_COMPLETION,
            amount=Decimal("5000.00"),
            unapplied_amount=Decimal("0.00"),
            notes="door cash for the added line",
        )
        db.session.add(event)
        db.session.flush()
        db.session.add(
            CashCollectionAllocation(
                cash_collection_event_id=event.id,
                payment_id=payment.id,
                order_id=order.id,
                allocated_amount=Decimal("5000.00"),
                allocation_mode="auto",
            )
        )
        db.session.commit()

        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.CANCELLED)

        db.session.expire_all()
        cancel_events = [
            e
            for e in _credit_events(sample_user.id)
            if (e.proof_data or {}).get("flow") == "order_cancel_prepaid_credit"
        ]
        assert len(cancel_events) == 1
        assert Decimal(str(cancel_events[0].amount)) == Decimal("15000.00"), (
            "only the 15,000 the gateway actually took is creditable"
        )

        # I3 — AND THE DOOR CASH IS HANDED BACK, not quietly kept. Subtracting it
        # is arithmetically right but would leave the customer 5,000 short with
        # no refund route of any kind now that the gateway rail is closed. The
        # allocation is reversed into its own event's unapplied pool instead, so
        # the customer ends up whole: 15,000 of new credit + 5,000 restored.
        door_event = CashCollectionEvent.query.filter_by(event_id="CCE-B4-DOOR-CASH").first()
        assert Decimal(str(door_event.unapplied_amount)) == Decimal("5000.00"), (
            "the customer's banknotes were kept instead of being returned as credit"
        )
        assert Decimal(str(door_event.amount)) == Decimal("5000.00"), (
            "the driver handed those notes over and still owes the office the same total"
        )
        allocation = CashCollectionAllocation.query.filter_by(cash_collection_event_id=door_event.id).one()
        assert allocation.reversed_at is not None

        spendable = sum(
            (Decimal(str(e.unapplied_amount)) for e in _credit_events(sample_user.id)),
            Decimal("0.00"),
        )
        assert spendable == Decimal("20000.00"), "the customer paid 20,000 and must get 20,000 back"

        # And the dead-order projection is re-asserted: no phantom receivable on
        # an order nobody owes anything for, and the order still reads paid.
        refreshed = Payment.query.get(payment.id)
        assert refreshed.status is PaymentStatus.COMPLETED
        assert Decimal(str(refreshed.outstanding_amount)) == Decimal("0.00")
        assert type(order).query.get(order.id).is_paid is True


@pytest.mark.integration
@pytest.mark.payment
class TestNothingIsMintedOutOfMoneyWeNeverTook:
    def test_an_unpaid_click_order_cancel_mints_no_credit(
        self, app, client, db, paid_click_order, sample_user, gateway_is_a_landmine
    ):
        """The abandoned-checkout case, and the reason the primitive is gated on
        ``{COMPLETED, PARTIALLY_PAID}``: a PENDING Click payment is a link the
        customer never paid. Crediting it would hand out money the gateway never
        took."""
        order, payment, _code = paid_click_order
        payment.status = PaymentStatus.PENDING
        payment.amount_collected = Decimal("0.00")
        payment.outstanding_amount = payment.amount
        payment.paid_at = None
        order.is_paid = False
        order.paid_at = None
        db.session.commit()

        response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))
        assert response.status_code == 200, response.get_json()

        db.session.expire_all()
        assert _credit_events(sample_user.id) == []
        # And the existing terminal-state sync still cancels the never-paid row.
        assert Payment.query.get(payment.id).status is PaymentStatus.CANCELLED

    # NOTE: the grocery hold-back cell that stood here was DELETED in fix round 1.
    # It pinned "a grocery customer is credited nothing", which the owner then
    # reversed: credit them exactly like everyone else and suppress only the
    # corporate-contract mirror. Its replacement is
    # TestGroceryIsCreditedWithTheMirrorSuppressed below. Left in place it would
    # have kept asserting the behaviour the ruling removed.


# --------------------------------------------------------------------------- #
# FIX ROUND 1 — the edges B4a did not follow.
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.payment
class TestFiledReceiptSurvivesAdminRetry:
    """C1 — B4a made a filed tax receipt ERASABLE, and this is the new hole.

    `process_click_fiscalization`'s COMPLETED short-circuit is `and not force`.
    `POST /admin/payments/<id>/fiscalization/retry` passes `force=True`, so it
    falls through to the `payment_requires_click_fiscalization` check — which
    B4a's `order_is_dead` clause now makes FALSE for a dead order — and
    overwrites the COMPLETED fiscalization with NOT_REQUIRED plus a fresh
    `completed_at`. The record that a receipt was submitted for a real USED
    marking code is destroyed.

    Before B4a the predicate was True and this click took the fiscalize branch,
    so the hole did not exist. It is spec §6.2's own invariant.
    """

    def test_admin_retry_on_a_returned_order_cannot_erase_a_filed_receipt(
        self, app, client, db, paid_click_order, admin_user, gateway_is_a_landmine
    ):
        from flask_jwt_extended import create_access_token
        from business_app.services.order_service import OrderService

        order, payment, code = paid_click_order
        order.status = OrderStatus.OUT_FOR_DELIVERY
        code.status = MarkingCodeStatus.USED
        fiscalization = PaymentFiscalizationService().ensure_fiscalization_record(payment)
        fiscalization.status = FiscalizationStatus.COMPLETED
        fiscalization.provider_receipt_url = "https://ofd.example/receipt/c1"
        fiscalization.provider_status = "submitted"
        db.session.commit()

        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.RETURNED)
            token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})

        db.session.expire_all()
        assert type(order).query.get(order.id).status is OrderStatus.RETURNED, "precondition: the order is dead"

        response = client.post(
            f"/api/v1/admin/payments/{payment.id}/fiscalization/retry",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
        )
        assert response.status_code in (200, 400), response.get_json()

        db.session.expire_all()
        refreshed = PaymentFiscalization.query.filter_by(payment_id=payment.id).first()
        assert refreshed.status is FiscalizationStatus.COMPLETED, (
            "a FILED receipt was overwritten with NOT_REQUIRED by an admin retry"
        )
        assert refreshed.provider_receipt_url == "https://ofd.example/receipt/c1"
        assert ProductMarkingCode.query.get(code.id).status is MarkingCodeStatus.USED


@pytest.mark.integration
@pytest.mark.payment
class TestAdminDeliveryReturnSettlesTheMoney:
    """C2 — a live admin surface took the customer's money with no ledger row.

    `AdminDeliveryService._apply_status_update` writes
    `delivery.order.status = OrderStatus.RETURNED` DIRECTLY, bypassing
    `update_order_status` entirely and duplicating only the CASH block. So none
    of B4a's siblings ran: no credit, no marking-code release, no fiscalization
    brake. And the admin refund route that could once have rescued it was
    deleted in the same change, leaving the state unrecoverable in-app.
    """

    def _admin_headers(self, app, admin_user):
        from flask_jwt_extended import create_access_token

        with app.app_context():
            token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _assigned_delivery(self, db, order, driver):
        from business_app.models.delivery import Delivery, DeliveryPerson
        from shared.enums import DeliveryStatus

        person = DeliveryPerson.query.filter_by(user_id=driver.id).first()
        if person is None:
            person = DeliveryPerson(
                user_id=driver.id,
                full_name=driver.full_name or "Delivery Driver",
                phone=driver.phone,
                is_active=True,
                is_available=True,
            )
            db.session.add(person)
            db.session.flush()
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=person.id,
            status=DeliveryStatus.ASSIGNED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()
        return delivery

    def test_marking_a_delivery_returned_credits_the_card_payment(
        self, app, client, db, paid_click_order, sample_user, admin_user, delivery_driver, gateway_is_a_landmine
    ):
        order, payment, code = paid_click_order
        delivery = self._assigned_delivery(db, order, delivery_driver)

        response = client.put(
            f"/api/v1/admin/deliveries/{delivery.id}",
            headers=self._admin_headers(app, admin_user),
            json={"status": "returned"},
        )
        assert response.status_code == 200, response.get_json()

        db.session.expire_all()
        events = _credit_events(sample_user.id)
        assert len(events) == 1, "the admin delivery surface took the money with no ledger row"
        assert Decimal(str(events[0].amount)) == Decimal("15000.00")
        assert (events[0].proof_data or {}).get("flow") == "order_cancel_prepaid_credit"

        assert ProductMarkingCode.query.get(code.id).status is MarkingCodeStatus.AVAILABLE, (
            "reserved codes were stranded RESERVED forever"
        )
        assert PaymentFiscalization.query.filter_by(payment_id=payment.id).first().status is (
            FiscalizationStatus.NOT_REQUIRED
        )
        assert Payment.query.get(payment.id).status is PaymentStatus.COMPLETED


@pytest.mark.integration
@pytest.mark.payment
class TestGroceryIsCreditedWithTheMirrorSuppressed:
    """C3 — owner ruling: credit them exactly like everyone else, but suppress
    the corporate-contract mirror for this flow.

    `post_collection` mirrors every positive collection onto a grocery
    customer's contract via `settle_order_collection`, which for an AMOUNT-mode
    contract posts a COLLECT that reduces contract debt. A cancelled order was
    never CHARGEd (that happens at DELIVERED), so an unsuppressed mirror pays
    down a debt that does not exist. Holding the credit back instead was
    strictly WORSE than pre-B4a, where the customer at least got a gateway
    reversal — they got nothing, silently, with no route to fix it.
    """

    def test_a_grocery_customer_is_credited_and_their_contract_is_untouched(
        self, app, db, paid_click_order, sample_user, gateway_is_a_landmine
    ):
        from shared.enums import EntitySubtype, UserType
        from business_app.services.order_service import OrderService
        from business_app.models.corporate import (
            CorporateContract,
            CorporateContractStatus,
            CorporatePrepaymentAccount,
            CorporatePrepaymentEventType,
            CorporatePrepaymentLedger,
        )
        from shared.enums import CorporateContractTrackingMode
        from datetime import timedelta
        from uuid import uuid4

        order, payment, _code = paid_click_order
        sample_user.user_type = UserType.ENTITY
        sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
        db.session.commit()
        assert sample_user.is_grocery_store is True

        # 🔴 A REAL, ACTIVE, AMOUNT-MODE CONTRACT WITH A CHARGED BALANCE.
        # Counting `CorporateContract` rows was VACUOUS: with no contract,
        # `settle_order_collection` returns early before writing anything, and
        # the mirror never creates a contract in any case. The observable it
        # actually moves is a COLLECT row on `CorporatePrepaymentLedger` and the
        # account's `outstanding_amount`. Assert THOSE, or the risky half of the
        # owner's ruling has no test behind it.
        contract = CorporateContract(
            user_id=sample_user.id,
            contract_number=f"B4A-{uuid4().hex[:10]}",
            name="B4a grocery mirror guard",
            status=CorporateContractStatus.ACTIVE,
            start_date=datetime.now(UTC) - timedelta(days=1),
            currency="UZS",
            is_active=True,
            tracking_mode=CorporateContractTrackingMode.AMOUNT,
        )
        db.session.add(contract)
        db.session.flush()
        account = CorporatePrepaymentAccount(
            contract_id=contract.id,
            is_active=True,
            outstanding_amount=Decimal("90000.00"),
        )
        db.session.add(account)
        db.session.commit()

        debt_before = Decimal(str(account.outstanding_amount))
        collects_before = CorporatePrepaymentLedger.query.filter_by(
            contract_id=contract.id, event_type=CorporatePrepaymentEventType.COLLECT
        ).count()
        assert debt_before == Decimal("90000.00")

        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.CANCELLED)

        db.session.expire_all()
        events = _credit_events(sample_user.id)
        assert len(events) == 1, "the owner ruled: credit a grocery customer like everyone else"
        assert Decimal(str(events[0].amount)) == Decimal("15000.00")
        assert Decimal(str(events[0].unapplied_amount)) == Decimal("15000.00")

        # And the mirror stayed suppressed. A COLLECT here would pay down 15,000
        # of contract debt that this cancelled order was never CHARGEd for.
        account = CorporatePrepaymentAccount.query.get(account.id)
        assert Decimal(str(account.outstanding_amount)) == debt_before, (
            "the corporate-contract mirror fired: contract debt moved for a cancelled order"
        )
        assert (
            CorporatePrepaymentLedger.query.filter_by(
                contract_id=contract.id, event_type=CorporatePrepaymentEventType.COLLECT
            ).count()
            == collects_before
        ), "a COLLECT ledger row was written for money that was never charged"


@pytest.mark.integration
@pytest.mark.payment
class TestTheBrakeOnlyTouchesFiscalizedRails:
    """M1 — the brake fired for EVERY payment on a dead order, so a payme or
    cash order gained a `PaymentFiscalization` row it never had. That also made
    the "payme is byte-for-byte unchanged" claim false."""

    @pytest.mark.parametrize("method", [PaymentMethod.PAYME, PaymentMethod.CASH])
    def test_a_non_fiscalized_rail_gains_no_fiscalization_row(
        self, app, db, paid_click_order, method, gateway_is_a_landmine
    ):
        from business_app.services.order_service import OrderService

        order, payment, _code = paid_click_order
        PaymentFiscalization.query.filter_by(payment_id=payment.id).delete()
        payment.payment_method = method
        order.payment_method = method
        if method == PaymentMethod.CASH:
            payment.collected_by = payment.user_id
        db.session.commit()

        with app.app_context():
            OrderService().update_order_status(order.id, OrderStatus.CANCELLED)

        db.session.expire_all()
        assert PaymentFiscalization.query.filter_by(payment_id=payment.id).count() == 0, (
            f"a {method.value} payment must never gain a Click fiscalization record"
        )
