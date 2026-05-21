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
