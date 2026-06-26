"""Integration tests for admin order-edit endpoints + cascade.

Covers:
  - POST /admin/orders/<id>/edit-preview (dry-run + impact summary)
  - POST /admin/orders/<id>/edit (apply + cascade)
  - GET  /admin/orders/<id>/edit-history (audit listing)
  - GET  /admin/orders/<id> now returns edit_window_remaining_hours / is_editable

End-to-end paths:
  1. Pre-delivery quantity increase on cash order — totals recomputed, history written.
  2. Pre-delivery quantity decrease on cash order — totals recomputed, history written.
  3. Delivered card order quantity decrease — blocked with card_paid_orders_can_only_grow.
  4. Delivered order > ORDER_EDIT_WINDOW_HOURS — blocked with edit_window_expired.
  5. Concurrent edit produces consistent OrderEditHistory rows.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.order import Order, OrderEditHistory, OrderItem
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _seed_order_with_item(
    sample_user,
    sample_product,
    *,
    status: OrderStatus = OrderStatus.CONFIRMED,
    payment_method: PaymentMethod = PaymentMethod.CASH,
    is_paid: bool = False,
    quantity: int = 4,
) -> Order:
    unit_price = Decimal(str(sample_product.base_price))
    subtotal = unit_price * Decimal(quantity)
    order = Order(
        user_id=sample_user.id,
        status=status,
        subtotal=subtotal,
        discount_amount=Decimal("0.00"),
        delivery_fee=Decimal("3000.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=subtotal + Decimal("3000.00"),
        payment_method=payment_method,
        is_paid=is_paid,
        created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.flush()

    item = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        quantity=quantity,
        unit_price=unit_price,
        discount_amount=Decimal("0.00"),
        total_price=subtotal,
    )
    _db.session.add(item)
    _db.session.commit()
    return order


def _seed_paid_card_delivered_order(sample_user, sample_product, *, quantity: int = 4) -> Order:
    order = _seed_order_with_item(
        sample_user,
        sample_product,
        status=OrderStatus.DELIVERED,
        payment_method=PaymentMethod.CARD,
        is_paid=True,
        quantity=quantity,
    )
    payment = Payment(
        user_id=sample_user.id,
        order_id=order.id,
        amount=Decimal(str(order.total_amount)),
        amount_collected=Decimal(str(order.total_amount)),
        outstanding_amount=Decimal("0.00"),
        payment_method=PaymentMethod.CARD,
        status=PaymentStatus.COMPLETED,
    )
    _db.session.add(payment)
    _db.session.commit()
    return order


# -------------------------------------------------------------------------
# Preview endpoint
# -------------------------------------------------------------------------


def test_edit_preview_pre_delivery_quantity_increase(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 6,
            }
        ],
        "reason": "customer added 2 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit-preview",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["blocking_reasons"] == []
    assert data["totals_after"]["subtotal"] > data["totals_before"]["subtotal"]
    assert data["is_post_delivery"] is False


def test_edit_preview_card_paid_decrease_creates_prepayment(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Card-paid decrease no longer blocks — it creates a prepayment credit.

    Golden rule: we never revert marking codes / fiscalization. The reduced
    amount becomes customer prepayment usable on future cash orders.
    """
    order = _seed_paid_card_delivered_order(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 2,
            }
        ],
        "reason": "customer received only 2 of 4 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit-preview",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["blocking_reasons"] == []
    # Warning surfaces the policy.
    assert any(
        "card_paid_decrease_creates_prepayment" in w for w in data["warnings"]
    )
    # Cascade summary should plan a prepayment credit.
    assert data["cascade_summary"]["payment"]["action"] == "create_prepayment_credit"
    assert data["cascade_summary"]["payment"]["payment_method_original"] == "card"


def test_edit_preview_blocks_outside_window(
    client, db, admin_auth_headers, sample_user, sample_product, app
):
    # Override window for this test
    app.config["ORDER_EDIT_WINDOW_HOURS"] = 72
    order = _seed_order_with_item(
        sample_user,
        sample_product,
        status=OrderStatus.DELIVERED,
        is_paid=True,
        quantity=4,
    )
    # Backdate the order so the window has expired. We backdate paid_at since
    # there's no delivery row in this fixture.
    order.paid_at = datetime.now(UTC) - timedelta(hours=200)
    _db.session.commit()

    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 5,
            }
        ],
        "reason": "too late",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit-preview",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert any("edit_window_expired" in r for r in data["blocking_reasons"])


# -------------------------------------------------------------------------
# Apply endpoint
# -------------------------------------------------------------------------


def test_apply_edit_pre_delivery_increase_writes_history(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 6,
            }
        ],
        "reason": "customer added 2 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["order_id"] == order.id
    history_id = data["history_id"]

    # Refetch from DB.
    refreshed = Order.query.get(order.id)
    assert refreshed.order_items[0].quantity == 6
    assert Decimal(str(refreshed.subtotal)) == Decimal(str(sample_product.base_price)) * 6

    history = OrderEditHistory.query.get(history_id)
    assert history is not None
    assert history.order_id == order.id
    assert history.reason == "customer added 2 bottles"
    assert history.diff["totals_after"]["subtotal"] > history.diff["totals_before"]["subtotal"]


def test_apply_edit_pre_delivery_decrease_writes_history(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 2,
            }
        ],
        "reason": "customer removed 2 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200

    refreshed = Order.query.get(order.id)
    assert refreshed.order_items[0].quantity == 2

    entries = OrderEditHistory.query.filter_by(order_id=order.id).all()
    assert len(entries) == 1


def test_apply_edit_rejects_short_reason(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 5,
            }
        ],
        "reason": "x",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code in (400, 422)


def test_apply_edit_no_changes_blocks(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 4,  # same as current
            }
        ],
        "reason": "no real change",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400


# -------------------------------------------------------------------------
# History endpoint
# -------------------------------------------------------------------------


def test_history_endpoint_lists_entries_newest_first(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)

    # Apply two edits.
    for new_qty, reason in [(5, "first edit"), (6, "second edit")]:
        resp = client.post(
            f"/api/v1/admin/orders/{order.id}/edit",
            json={
                "items": [
                    {
                        "orderItemId": order.order_items[0].id,
                        "productId": sample_product.id,
                        "quantity": new_qty,
                    }
                ],
                "reason": reason,
            },
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        # Refresh order_items relationship so the next loop sees the latest id.
        _db.session.refresh(order)

    resp = client.get(
        f"/api/v1/admin/orders/{order.id}/edit-history",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["total"] == 2
    # Newest first.
    assert data["entries"][0]["reason"] == "second edit"
    assert data["entries"][1]["reason"] == "first edit"


def test_order_detail_includes_edit_window_fields(
    client, db, admin_auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    resp = client.get(
        f"/api/v1/admin/orders/{order.id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    order_data = resp.get_json()["data"]["order"]
    assert "is_editable" in order_data
    assert "edit_window_remaining_hours" in order_data
    assert "edit_history_count" in order_data
    # Pre-delivery orders are editable with no window restriction.
    assert order_data["is_editable"] is True
    assert order_data["edit_window_remaining_hours"] is None
    assert order_data["edit_history_count"] == 0


# -------------------------------------------------------------------------
# Full cascade scenarios (plan §9)
# -------------------------------------------------------------------------


def _seed_paid_cash_delivered_order(sample_user, sample_product, *, quantity: int = 4) -> Order:
    order = _seed_order_with_item(
        sample_user,
        sample_product,
        status=OrderStatus.DELIVERED,
        payment_method=PaymentMethod.CASH,
        is_paid=True,
        quantity=quantity,
    )
    payment = Payment(
        user_id=sample_user.id,
        order_id=order.id,
        amount=Decimal(str(order.total_amount)),
        amount_collected=Decimal(str(order.total_amount)),
        outstanding_amount=Decimal("0.00"),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.COMPLETED,
        collected_by=sample_user.id,  # completed cash requires a collector (ARCH-006)
    )
    _db.session.add(payment)
    _db.session.commit()
    return order


def test_apply_edit_delivered_cash_decrease_creates_prepayment(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Delivered cash decrease writes prepayment cash event with
    driver_cash_session_id=None and leaves Payment.amount intact (audit)."""
    from business_app.models.payment import CashCollectionEvent

    order = _seed_paid_cash_delivered_order(sample_user, sample_product, quantity=6)
    original_total = Decimal(str(order.total_amount))
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 4,
            }
        ],
        "reason": "customer accepted 4 instead of 6 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    cascade = resp.get_json()["data"]["cascade_summary"]
    assert cascade["cash"]["action"] == "prepayment_created"
    assert cascade["cash"]["amount"] > 0

    # Customer-credit cash event was written, NOT bound to a driver session.
    refund_event = (
        CashCollectionEvent.query.filter_by(order_id=order.id)
        .filter(CashCollectionEvent.idempotency_key.like("order_edit_refund:%"))
        .first()
    )
    assert refund_event is not None
    assert refund_event.driver_cash_session_id is None
    assert refund_event.unapplied_amount > 0

    # Payment.amount intact for audit (post-delivery cash decrease policy).
    refreshed_order = Order.query.get(order.id)
    assert Decimal(str(refreshed_order.payment.amount)) == original_total


def test_apply_edit_delivered_card_decrease_creates_prepayment(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Card-paid decrease writes prepayment (cash-only-usable). Card is
    NOT refunded. Marking codes (if any) are preserved per golden rule."""
    from business_app.models.payment import CashCollectionEvent

    order = _seed_paid_card_delivered_order(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 2,
            }
        ],
        "reason": "customer kept only 2 of 4 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["cascade_summary"]["payment"]["action"] == "create_prepayment_credit"
    assert data["cascade_summary"]["cash"]["action"] == "prepayment_created"
    assert data["cascade_summary"]["cash"]["payment_method_original"] == "card"

    refund_event = (
        CashCollectionEvent.query.filter_by(order_id=order.id)
        .filter(CashCollectionEvent.idempotency_key.like("order_edit_refund:%"))
        .first()
    )
    assert refund_event is not None
    # Card payment record left intact (no gateway refund).
    refreshed = Order.query.get(order.id)
    assert refreshed.payment.payment_method == PaymentMethod.CARD


def test_apply_edit_delivered_card_increase_marks_outstanding(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Card-paid increase: card NOT re-charged; the delta is moved to
    Payment.outstanding_amount so the cash-collection flow can settle it."""
    order = _seed_paid_card_delivered_order(sample_user, sample_product, quantity=2)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 4,
            }
        ],
        "reason": "customer asked for 2 extra bottles after delivery",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    cascade = resp.get_json()["data"]["cascade_summary"]
    assert cascade["payment"]["action"] == "manual_cash_collection_required"
    assert cascade["cash"]["action"] == "additional_cash_collection_required"

    refreshed = Order.query.get(order.id)
    assert Decimal(str(refreshed.payment.outstanding_amount)) > Decimal("0")
    # Card payment_method preserved (no auto re-charge).
    assert refreshed.payment.payment_method == PaymentMethod.CARD


def test_apply_edit_rollback_on_mid_cascade_failure(
    client, db, admin_auth_headers, sample_user, sample_product, monkeypatch
):
    """If a cascade step raises mid-transaction, the whole edit rolls back:
    no OrderEditHistory row, item quantity unchanged."""
    from business_app.services.order_edit_service import OrderEditService

    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    original_qty = order.order_items[0].quantity

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic cascade failure")

    monkeypatch.setattr(
        OrderEditService, "_cascade_loyalty", _boom, raising=True
    )

    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 5,
            }
        ],
        "reason": "should roll back entirely",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code in (400, 500)

    # History row should NOT exist.
    history = OrderEditHistory.query.filter_by(order_id=order.id).all()
    assert history == []

    # Item quantity unchanged.
    refreshed = Order.query.get(order.id)
    assert refreshed.order_items[0].quantity == original_qty


# -------------------------------------------------------------------------
# Permission gating
# -------------------------------------------------------------------------


def test_apply_edit_rejects_non_admin(
    client, db, auth_headers, sample_user, sample_product
):
    order = _seed_order_with_item(sample_user, sample_product, quantity=4)
    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 5,
            }
        ],
        "reason": "should not be allowed",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=auth_headers,
    )
    assert resp.status_code in (401, 403)


# -------------------------------------------------------------------------
# Bottle-balance gating (pre-delivery vs delivered)
# -------------------------------------------------------------------------
#
# Business rule: the customer's returnable-bottle balance only moves when the
# order is actually DELIVERED. The delivery flow credits the *live* (already
# edited) order quantity, so a pre-delivery edit must make ZERO balance change;
# otherwise the reduction is applied twice (once by the edit, once by the
# smaller delivery credit).


def _attach_returnable_address(db, sample_user, sample_product):
    """Make the product track returnable bottles + give the customer an address.

    Returns the created UserAddress.
    """
    from business_app.models.user import UserAddress

    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="123 Test St",
        city="Tashkent",
    )
    db.session.add(address)
    db.session.flush()
    return address


def test_apply_edit_pre_delivery_does_not_touch_bottle_balance(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Reducing item qty while OUT_FOR_DELIVERY must NOT mutate the bottle balance.

    The delivery flow will later credit the already-reduced quantity, so any
    adjustment here double-counts the reduction.
    """
    from business_app.models.bottle import BottleBalance, BottleLedger
    from shared.enums import BottleLedgerEventType

    address = _attach_returnable_address(db, sample_user, sample_product)
    order = _seed_order_with_item(
        sample_user, sample_product, status=OrderStatus.OUT_FOR_DELIVERY, quantity=4
    )
    order.delivery_address_id = address.id
    db.session.commit()

    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 2,
            }
        ],
        "reason": "customer will receive only 2 bottles",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200

    adjustments = BottleLedger.query.filter_by(
        user_id=sample_user.id,
        address_id=address.id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
    ).all()
    assert adjustments == [], "pre-delivery edit must not write a bottle ADMIN_ADJUSTMENT"

    balance = BottleBalance.query.filter_by(
        user_id=sample_user.id, address_id=address.id
    ).first()
    assert balance is None, "pre-delivery edit must not create/modify a bottle balance"


def test_apply_edit_delivered_adjusts_bottle_balance(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Reducing item qty on a DELIVERED order DOES correct the bottle balance.

    Regression guard for the legitimate post-delivery correction path: the
    balance was already credited at delivery, so the edit must debit the delta.
    """
    from business_app.models.bottle import BottleBalance, BottleLedger
    from shared.enums import BottleLedgerEventType

    address = _attach_returnable_address(db, sample_user, sample_product)
    order = _seed_paid_cash_delivered_order(sample_user, sample_product, quantity=4)
    order.delivery_address_id = address.id
    # Delivery already credited 4 bottles to the customer.
    balance = BottleBalance(
        user_id=sample_user.id, address_id=address.id, balance=Decimal("4.00")
    )
    db.session.add(balance)
    db.session.commit()

    payload = {
        "items": [
            {
                "orderItemId": order.order_items[0].id,
                "productId": sample_product.id,
                "quantity": 2,
            }
        ],
        "reason": "customer kept only 2 of 4 bottles after delivery",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json=payload,
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200

    adjustments = BottleLedger.query.filter_by(
        user_id=sample_user.id,
        address_id=address.id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
    ).all()
    assert len(adjustments) == 1, "delivered edit must write exactly one bottle adjustment"
    assert Decimal(str(adjustments[0].quantity)) == Decimal("-2")

    db.session.refresh(balance)
    assert Decimal(str(balance.balance)) == Decimal("2.00")


# -------------------------------------------------------------------------
# Delivered cash INCREASE settled from customer over-collection credit
# (prod bug TG_000190_26: driver over-collected at the door, admin edits up)
# -------------------------------------------------------------------------


def _grant_unapplied_cash_credit(customer_id, amount, *, order_id=None, collector_user_id=None):
    """Seed an unapplied (available) customer prepayment credit — the surplus
    a driver leaves when they over-collect cash at delivery."""
    from business_app.models.payment import CashCollectionEvent
    from shared.enums import CashCollectionSource

    event = CashCollectionEvent(
        customer_id=customer_id,
        collector_user_id=collector_user_id,
        recorded_by_user_id=collector_user_id,
        order_id=order_id,
        amount=Decimal(str(amount)),
        currency="UZS",
        source=CashCollectionSource.DELIVERY_COMPLETION,
        unapplied_amount=Decimal(str(amount)),
    )
    _db.session.add(event)
    _db.session.commit()
    return event


def test_apply_edit_delivered_cash_increase_settled_from_overcollection_credit(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Reported prod bug (TG_000190_26): the driver collected one extra bottle's
    worth at the door (over-collection → customer prepayment credit). When the
    admin edits the order up to match, the increase must be settled from that
    credit so the order ends FULLY PAID, not showing a phantom outstanding."""
    from business_app.services.cash_collection_service import CashCollectionService

    order = _seed_paid_cash_delivered_order(sample_user, sample_product, quantity=4)
    unit_price = Decimal(str(sample_product.base_price))
    # Driver over-collected exactly one extra bottle at delivery, tied to this order.
    _grant_unapplied_cash_credit(
        sample_user.id, unit_price, order_id=order.id, collector_user_id=sample_user.id
    )

    payload = {
        "items": [
            {"orderItemId": order.order_items[0].id, "productId": sample_product.id, "quantity": 5}
        ],
        "reason": "customer took 1 extra bottle at the door; driver collected it",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit", json=payload, headers=admin_auth_headers
    )
    assert resp.status_code == 200
    cascade = resp.get_json()["data"]["cascade_summary"]
    assert cascade["cash"]["action"] == "covered_by_prepayment_credit"

    refreshed = Order.query.get(order.id)
    pay = refreshed.payment
    new_total = Decimal(str(refreshed.total_amount))
    assert Decimal(str(pay.amount)) == new_total
    assert Decimal(str(pay.amount_collected)) == new_total
    assert Decimal(str(pay.outstanding_amount)) == Decimal("0.00")
    assert pay.status == PaymentStatus.COMPLETED
    assert refreshed.is_paid is True
    # The over-collection credit was consumed by the increase.
    assert CashCollectionService().get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")


def test_apply_edit_delivered_cash_increase_partial_credit_leaves_residual(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """When the available credit covers only part of the increase, the residual
    stays outstanding and the order is correctly no longer fully paid."""
    order = _seed_paid_cash_delivered_order(sample_user, sample_product, quantity=4)
    unit_price = Decimal(str(sample_product.base_price))
    # Credit covers one of the two extra bottles.
    _grant_unapplied_cash_credit(
        sample_user.id, unit_price, order_id=order.id, collector_user_id=sample_user.id
    )

    payload = {
        "items": [
            {"orderItemId": order.order_items[0].id, "productId": sample_product.id, "quantity": 6}
        ],
        "reason": "customer took 2 extra bottles; driver collected one bottle's worth",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit", json=payload, headers=admin_auth_headers
    )
    assert resp.status_code == 200
    cascade = resp.get_json()["data"]["cascade_summary"]
    assert cascade["cash"]["action"] == "partially_covered_by_prepayment_credit"

    refreshed = Order.query.get(order.id)
    pay = refreshed.payment
    assert Decimal(str(pay.outstanding_amount)) == unit_price  # one bottle still owed
    assert pay.status == PaymentStatus.PARTIALLY_PAID
    assert refreshed.is_paid is False


def test_apply_edit_delivered_cash_increase_no_credit_marks_unpaid(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Increase with no available credit: the full delta is outstanding AND the
    order is flagged not-fully-paid (previously is_paid stayed True with a
    positive outstanding — an inconsistency that wrongly counted as COD debt)."""
    order = _seed_paid_cash_delivered_order(sample_user, sample_product, quantity=4)
    unit_price = Decimal(str(sample_product.base_price))

    payload = {
        "items": [
            {"orderItemId": order.order_items[0].id, "productId": sample_product.id, "quantity": 5}
        ],
        "reason": "customer took 1 extra bottle; cash to be collected later",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order.id}/edit", json=payload, headers=admin_auth_headers
    )
    assert resp.status_code == 200
    cascade = resp.get_json()["data"]["cascade_summary"]
    assert cascade["cash"]["action"] == "additional_cash_collection_required"

    refreshed = Order.query.get(order.id)
    pay = refreshed.payment
    assert Decimal(str(pay.outstanding_amount)) == unit_price
    assert pay.status == PaymentStatus.PARTIALLY_PAID
    assert refreshed.is_paid is False


def test_apply_edit_delivered_cash_increase_reclaims_overcollection_reserved_elsewhere(
    client, db, admin_auth_headers, sample_user, sample_product
):
    """Edge case (auto-release-and-reapply): the over-collection captured at
    THIS order's delivery was tentatively reserved against the customer's other
    pending order. Editing this order up reclaims that reservation and settles
    this order fully."""
    from business_app.services.cash_collection_service import CashCollectionService

    cash_service = CashCollectionService()
    order_a = _seed_paid_cash_delivered_order(sample_user, sample_product, quantity=4)
    unit_price = Decimal(str(sample_product.base_price))

    # Over-collection captured at order A's delivery.
    _grant_unapplied_cash_credit(
        sample_user.id, unit_price, order_id=order_a.id, collector_user_id=sample_user.id
    )

    # A pending cash order B with an outstanding balance.
    order_b = _seed_order_with_item(
        sample_user, sample_product, status=OrderStatus.CONFIRMED, is_paid=False, quantity=2
    )
    payment_b = Payment(
        user_id=sample_user.id,
        order_id=order_b.id,
        amount=Decimal(str(order_b.total_amount)),
        amount_collected=Decimal("0.00"),
        outstanding_amount=Decimal(str(order_b.total_amount)),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.PENDING,
    )
    _db.session.add(payment_b)
    _db.session.commit()

    # The surplus gets reserved against order B (so it is NOT "available").
    cash_service.reserve_customer_prepaid_credit_for_payment(payment_b)
    _db.session.commit()
    assert cash_service.get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")

    payload = {
        "items": [
            {"orderItemId": order_a.order_items[0].id, "productId": sample_product.id, "quantity": 5}
        ],
        "reason": "reclaim this order's own over-collection to settle the extra bottle",
    }
    resp = client.post(
        f"/api/v1/admin/orders/{order_a.id}/edit", json=payload, headers=admin_auth_headers
    )
    assert resp.status_code == 200
    cascade = resp.get_json()["data"]["cascade_summary"]
    assert cascade["cash"]["action"] == "covered_by_prepayment_credit"

    refreshed_a = Order.query.get(order_a.id)
    assert Decimal(str(refreshed_a.payment.outstanding_amount)) == Decimal("0.00")
    assert refreshed_a.is_paid is True
    # The reservation on order B was released as part of the reclaim.
    db.session.refresh(payment_b)
    reserved_b = Decimal(str((payment_b.provider_data or {}).get("cod_prepayment_reserved_amount", 0) or 0))
    assert reserved_b == Decimal("0.00")
