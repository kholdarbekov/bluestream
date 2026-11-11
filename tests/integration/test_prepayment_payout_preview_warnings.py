"""Return prepayment: what the preview warns about (spec 2026-11-10, Task 3).

Every test drives the admin preview (and, where credit must stay put, the apply) route with the body the
modal sends. Credit is booked by the real writer (`post_collection`); a hold is booked by the real
reservation path (`reserve_customer_prepaid_credit_for_payment`).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.models.subscription import Subscription
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, SubscriptionFrequency, SubscriptionStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_place_group,
    make_user,
)

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/staff/cash-reconciliation/customers/{}"
PREVIEW = BASE + "/prepayment-payouts/preview"
APPLY = BASE + "/prepayment-payouts"


def _credit(user, amount, admin):
    event = CashCollectionService().post_collection(
        customer_id=user.id,
        amount=Decimal(amount),
        source="admin_adjustment",
        recorded_by_user_id=admin.id,
        notes="Prepaid in cash at the office",
        occurred_at=datetime.now(UTC) - timedelta(days=1),
    )
    return event.id


def _held_order(db, user, total, *, status=OrderStatus.CONFIRMED):
    """An unpaid COD order that holds the customer's credit through the real reservation path."""
    order, payment = delivered_cod_order(db, user, total=Decimal(total), status=status)
    reserved = CashCollectionService().reserve_customer_prepaid_credit_for_payment(payment)
    db.session.commit()
    return order, reserved


def _preview(client, headers, customer_id, **body):
    body.setdefault("paid_by", "office")
    response = client.post(PREVIEW.format(customer_id), json=body, headers=headers)
    return response.status_code, response.get_json()


def _live_reservations(db):
    db.session.expire_all()
    return [
        Decimal(str(a.allocated_amount))
        for a in CashCollectionAllocation.query.filter(
            CashCollectionAllocation.reversed_at.is_(None),
            CashCollectionAllocation.allocation_mode == "prepaid_reservation",
        ).all()
    ]


def test_held_credit_is_untouched_and_listed(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    order, reserved = _held_order(db, sample_user, 30000)
    assert reserved == Decimal("30000.00")

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    summary = body["data"]
    assert summary["free_balance"] == 20000.0
    assert summary["amount"] == 20000.0
    assert summary["blocking_reasons"] == []
    assert summary["warnings"]["pending_orders"] == [
        {"order_id": order.id, "order_number": order.order_number, "status": "confirmed", "held_amount": 30000.0}
    ]

    response = client.post(
        APPLY.format(sample_user.id),
        json={"paid_by": "office", "amount": 20000, "reason": "Customer moved away", "idempotency_key": "k-held"},
        headers=admin_claim_headers,
    )
    assert response.status_code in (200, 201), response.get_json()
    assert _live_reservations(db) == [Decimal("30000.00")]


def test_pending_cash_order_without_credit_is_listed_with_zero_held(
    client, db, admin_user, admin_claim_headers, sample_user
):
    _credit(sample_user, 50000, admin_user)
    order, _ = delivered_cod_order(db, sample_user, total=Decimal("15000"), status=OrderStatus.PENDING)

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["warnings"]["pending_orders"] == [
        {"order_id": order.id, "order_number": order.order_number, "status": "pending", "held_amount": 0.0}
    ]


def test_confirmed_order_with_a_rescheduled_delivery_holding_credit_is_listed(
    client, db, admin_user, admin_claim_headers, sample_user
):
    _credit(sample_user, 50000, admin_user)
    order, reserved = _held_order(db, sample_user, 12000)
    assert reserved == Decimal("12000.00")
    # An admin moved the delivery to a later day: RESCHEDULED is the DELIVERY's status; the order stays CONFIRMED.
    db.session.add(
        Delivery(
            order_id=order.id,
            status=DeliveryStatus.RESCHEDULED,
            scheduled_date=datetime.now(UTC) + timedelta(days=3),
            scheduled_time_slot="10:00-12:00",
        )
    )
    db.session.commit()

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["warnings"]["pending_orders"] == [
        {"order_id": order.id, "order_number": order.order_number, "status": "confirmed", "held_amount": 12000.0}
    ]


def test_credit_settled_before_delivery_counts_as_held(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    order, _ = _held_order(db, sample_user, 12000)
    consumed = CashCollectionService().settle_reserved_prepayment_if_covered(
        order.payment, actor_user_id=admin_user.id
    )
    db.session.commit()
    assert consumed == Decimal("12000.00")

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["warnings"]["pending_orders"] == [
        {"order_id": order.id, "order_number": order.order_number, "status": "confirmed", "held_amount": 12000.0}
    ]


def test_released_reservation_is_not_held(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    order, _ = _held_order(db, sample_user, 12000)
    CashCollectionService().release_reserved_prepayment_for_order(order.id, actor_user_id=admin_user.id)
    order.status = OrderStatus.CANCELLED
    db.session.commit()

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["free_balance"] == 50000.0
    assert body["data"]["warnings"]["pending_orders"] == []


def test_live_subscriptions_warn(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    address = make_address(db, sample_user)
    subs = {}
    for number, sub_status in (
        ("SUB-W-1", SubscriptionStatus.ACTIVE),
        ("SUB-W-2", SubscriptionStatus.PAUSED),
        ("SUB-W-3", SubscriptionStatus.CANCELLED),
    ):
        sub = Subscription(
            subscription_number=number,
            user_id=sample_user.id,
            status=sub_status,
            name="Standard",
            frequency=SubscriptionFrequency.MONTHLY,
            billing_amount=Decimal("50000.00"),
            next_delivery_date=datetime.now(UTC).date() + timedelta(days=7),
            delivery_address_id=address.id,
            payment_method=PaymentMethod.CARD,
            start_date=datetime.now(UTC),
        )
        db.session.add(sub)
        subs[sub_status] = sub
    db.session.commit()

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["warnings"]["live_subscriptions"] == [
        {"subscription_id": subs[SubscriptionStatus.ACTIVE].id, "status": "active"},
        {"subscription_id": subs[SubscriptionStatus.PAUSED].id, "status": "paused"},
    ]
    assert body["data"]["blocking_reasons"] == []


def test_place_debt_is_info_only(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    coworker = make_user(db)
    mine, theirs = make_address(db, sample_user), make_address(db, coworker)
    group = make_place_group(db, mine, theirs, label="Shared office")
    delivered_cod_order(db, coworker, address=theirs, total=Decimal("45000"))

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["warnings"]["place_debts"] == [
        {"place_group_id": group.id, "label": "Shared office", "amount": 45000.0}
    ]
    assert body["data"]["blocking_reasons"] == []
    assert body["data"]["is_applicable"] is True


def test_place_without_debt_is_not_listed(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    make_place_group(db, make_address(db, sample_user), label="Quiet office")

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    assert body["data"]["warnings"]["place_debts"] == []


def test_fully_held_credit_previews_with_block_not_error(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 30000, admin_user)
    order, reserved = _held_order(db, sample_user, 30000)
    assert reserved == Decimal("30000.00")

    status, body = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200, body
    summary = body["data"]
    assert summary["free_balance"] == 0.0
    assert summary["blocking_reasons"][0].startswith("no_free_balance:")
    assert summary["warnings"]["pending_orders"] == [
        {"order_id": order.id, "order_number": order.order_number, "status": "confirmed", "held_amount": 30000.0}
    ]


def test_preview_writes_nothing_with_warnings(client, db, admin_user, admin_claim_headers, sample_user):
    _credit(sample_user, 50000, admin_user)
    _held_order(db, sample_user, 30000)
    before = sorted((e.id, str(e.unapplied_amount)) for e in CashCollectionEvent.query.all())

    status, _ = _preview(client, admin_claim_headers, sample_user.id)

    assert status == 200
    db.session.expire_all()
    assert before == sorted((e.id, str(e.unapplied_amount)) for e in CashCollectionEvent.query.all())
    assert _live_reservations(db) == [Decimal("30000.00")]


def test_statement_places_are_exactly_the_extracted_place_debts(db, admin_user, sample_user):
    coworker = make_user(db)
    mine, theirs = make_address(db, sample_user), make_address(db, coworker)
    group = make_place_group(db, mine, theirs, label="Shared office")
    delivered_cod_order(db, coworker, address=theirs, total=Decimal("45000"))
    service = CashCollectionService()

    expected = [
        {
            "address_id": mine.id,
            "place_group_id": group.id,
            "label": "Shared office",
            "place_open_cod_debt_total": 45000.0,
            "place_active_cod_debt_count": 1,
        }
    ]

    assert service._cluster_place_debts([sample_user.id]) == expected
    assert service.get_customer_cod_statement(sample_user.id)["places"] == expected
