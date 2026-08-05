"""Admin cash-reconciliation session views expose collection-event scope.

Phase 2 made a collection PLACE-scoped (settling a shared workplace's debts,
whoever owns them) or CLUSTER-scoped (settling a linked person's other
accounts). The admin session-detail modal previously attributed every event
solely to the posting customer, so an admin could not tell a workplace
collection from personal cash, nor trace where the money actually went.

These views are ADMIN-facing, so full attribution is wanted here — the exact
opposite of the customer-facing sanitization: nothing is redacted.

The added fields are ADDITIVE. An ordinary personal-scope session must keep
every pre-existing key at its pre-existing value, with the new fields degrading
to ``'personal'`` / ``None`` / ``None``.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.customer_link import AddressGroup
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, Payment
from business_app.models.user import User
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.utils.password_security import hash_password
from shared.enums import CashCollectionSource, OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name=email.split("@")[0], user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _paid_order_with_payment(db, owner, order_number, payment_id):
    order = Order(user_id=owner.id, order_number=order_number, status=OrderStatus.DELIVERED,
                  subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                  total_amount=Decimal("15000.00"), payment_method=PaymentMethod.CASH,
                  created_at=datetime.now(UTC))
    db.session.add(order)
    db.session.flush()
    payment = Payment(order_id=order.id, user_id=owner.id, payment_method=PaymentMethod.CASH,
                      amount=Decimal("15000.00"), currency="UZS", status=PaymentStatus.PENDING,
                      payment_id=payment_id, outstanding_amount=Decimal("0.00"),
                      created_at=datetime.now(UTC))
    db.session.add(payment)
    db.session.commit()
    return order, payment


@pytest.mark.unit
def test_serialize_collection_event_carries_scope_and_stamps(db):
    """A place-scoped event carries the REAL group id/label and the REAL dual stamps."""
    payer = _user(db, "p@example.com", "+998900000001")
    owner = _user(db, "o@example.com", "+998900000002")
    group = AddressGroup(label="Acme office")
    db.session.add(group)
    db.session.commit()

    order, payment = _paid_order_with_payment(db, owner, "ORD-1", "pay_1")

    event = CashCollectionEvent(
        customer_id=payer.id, amount=Decimal("15000.00"), unapplied_amount=Decimal("0.00"),
        source=CashCollectionSource.DELIVERY_COMPLETION, occurred_at=datetime.now(UTC),
        scope_type="place",
        scope_snapshot={"group_id": group.id, "address_ids": [], "place_user_ids": [],
                        "orderer_cluster_user_ids": []},
    )
    db.session.add(event)
    db.session.commit()
    alloc = CashCollectionAllocation(
        cash_collection_event_id=event.id, payment_id=payment.id, order_id=order.id,
        allocated_amount=Decimal("15000.00"), allocation_order=1, allocation_mode="auto",
        source_customer_id=payer.id, beneficiary_user_id=owner.id,
    )
    db.session.add(alloc)
    db.session.commit()

    data = DriverReconciliationService()._serialize_collection_event(event)
    assert data["scope_type"] == "place"
    assert data["scope_group_id"] == group.id
    assert data["scope_group_label"] == "Acme office"
    [alloc_row] = data["allocations"]
    assert alloc_row["source_customer_id"] == payer.id
    assert alloc_row["beneficiary_user_id"] == owner.id
    # Full attribution: admin can see the money left the payer and landed on
    # someone else's order — the two ids genuinely differ.
    assert alloc_row["source_customer_id"] != alloc_row["beneficiary_user_id"]
    assert data["customer_id"] == payer.id
    assert alloc_row["order_number"] == "ORD-1"


@pytest.mark.unit
def test_cluster_scoped_event_reports_cluster_without_group_label(db):
    """Cluster scope surfaces as-is; the group label is place-only."""
    payer = _user(db, "p@example.com", "+998900000001")
    event = CashCollectionEvent(
        customer_id=payer.id, amount=Decimal("5000.00"), unapplied_amount=Decimal("5000.00"),
        source=CashCollectionSource.STANDALONE_MEETING, occurred_at=datetime.now(UTC),
        scope_type="cluster",
        scope_snapshot={"cluster_user_ids": [payer.id]},
    )
    db.session.add(event)
    db.session.commit()

    data = DriverReconciliationService()._serialize_collection_event(event)
    assert data["scope_type"] == "cluster"
    assert data["scope_group_id"] is None
    assert data["scope_group_label"] is None


@pytest.mark.unit
def test_personal_event_serializes_personal_scope(db):
    payer = _user(db, "p@example.com", "+998900000001")
    event = CashCollectionEvent(
        customer_id=payer.id, amount=Decimal("1000.00"), unapplied_amount=Decimal("1000.00"),
        source=CashCollectionSource.STANDALONE_MEETING, occurred_at=datetime.now(UTC),
    )
    db.session.add(event)
    db.session.commit()
    data = DriverReconciliationService()._serialize_collection_event(event)
    assert data["scope_type"] == "personal"
    assert data["scope_group_id"] is None
    assert data["scope_group_label"] is None


@pytest.mark.unit
def test_personal_scope_payload_is_otherwise_byte_identical(db):
    """Additive only: an ordinary personal event's pre-existing payload is untouched.

    Every key the view produced before Phase 2 must still carry exactly its old
    value; the only difference is the three new scope keys and the two new
    allocation stamps.
    """
    owner = _user(db, "o@example.com", "+998900000002")
    order, payment = _paid_order_with_payment(db, owner, "ORD-9", "pay_9")

    event = CashCollectionEvent(
        customer_id=owner.id, amount=Decimal("15000.00"), unapplied_amount=Decimal("0.00"),
        source=CashCollectionSource.DELIVERY_COMPLETION, occurred_at=datetime.now(UTC),
        order_id=order.id,
    )
    db.session.add(event)
    db.session.commit()
    alloc = CashCollectionAllocation(
        cash_collection_event_id=event.id, payment_id=payment.id, order_id=order.id,
        allocated_amount=Decimal("15000.00"), allocation_order=1, allocation_mode="auto",
    )
    db.session.add(alloc)
    db.session.commit()

    data = DriverReconciliationService()._serialize_collection_event(event)

    # Baseline (pre-Phase-2) event keys unchanged.
    baseline = event.to_dict()
    for key, value in baseline.items():
        assert data[key] == value, f"event key {key!r} changed"
    assert data["customer_name"] == owner.full_name
    assert data["customer_phone"] == owner.phone
    assert data["order_number"] == "ORD-9"

    # Exactly three new top-level keys.
    expected_new = {"scope_type", "scope_group_id", "scope_group_label"}
    old_keys = set(baseline) | {"customer_name", "customer_phone", "order_number", "allocations"}
    assert set(data) - old_keys == expected_new
    assert data["scope_type"] == "personal"

    # Exactly two new allocation keys; pre-existing allocation values unchanged.
    [alloc_row] = data["allocations"]
    old_alloc_keys = {"order_id", "order_number", "allocated_amount", "allocation_mode",
                      "reversed", "payment_status", "payment_outstanding_amount", "settlement"}
    assert set(alloc_row) - old_alloc_keys == {"source_customer_id", "beneficiary_user_id"}
    assert alloc_row["order_id"] == order.id
    assert alloc_row["order_number"] == "ORD-9"
    assert alloc_row["allocated_amount"] == 15000.0
    assert alloc_row["allocation_mode"] == "auto"
    assert alloc_row["reversed"] is False
    assert alloc_row["settlement"] == "fully"
    # Pre-migration / personal rows keep NULL stamps by design.
    assert alloc_row["source_customer_id"] is None
    assert alloc_row["beneficiary_user_id"] is None


@pytest.mark.unit
def test_session_render_does_not_n_plus_one_on_group_labels(db, count_queries):
    """One address_groups SELECT per DISTINCT group per render, not one per event.

    Measured: repeated ``AddressGroup.query.get(pk)`` does NOT short-circuit on
    the identity map in this runtime, so an unmemoized lookup costs one SELECT
    per place-scoped event in a view that renders every event of a session.
    """
    payer = _user(db, "p@example.com", "+998900000001")
    group = AddressGroup(label="Acme office")
    db.session.add(group)
    db.session.commit()
    group_id = group.id

    for _ in range(10):
        db.session.add(CashCollectionEvent(
            customer_id=payer.id, amount=Decimal("100.00"), unapplied_amount=Decimal("100.00"),
            source=CashCollectionSource.STANDALONE_MEETING, occurred_at=datetime.now(UTC),
            scope_type="place", scope_snapshot={"group_id": group_id},
        ))
    db.session.commit()
    db.session.expunge_all()
    events = CashCollectionEvent.query.all()
    assert len(events) == 10

    svc = DriverReconciliationService()
    cache = {}
    with count_queries() as counter:
        rows = [svc._serialize_collection_event(e, group_label_cache=cache) for e in events]

    group_selects = [s for s in counter.statements if "address_groups" in s]
    assert len(group_selects) == 1, f"N+1 on group labels: {len(group_selects)} selects for 10 events"
    # ...and every row still got the REAL label, not a blank from the memo.
    assert all(r["scope_group_label"] == "Acme office" for r in rows)
    assert all(r["scope_group_id"] == group_id for r in rows)


@pytest.mark.unit
def test_place_event_with_unknown_group_id_degrades_to_null_label(db):
    """A snapshot pointing at a deleted group must not explode the admin view."""
    payer = _user(db, "p@example.com", "+998900000001")
    event = CashCollectionEvent(
        customer_id=payer.id, amount=Decimal("1000.00"), unapplied_amount=Decimal("1000.00"),
        source=CashCollectionSource.STANDALONE_MEETING, occurred_at=datetime.now(UTC),
        scope_type="place", scope_snapshot={"group_id": 999999},
    )
    db.session.add(event)
    db.session.commit()

    data = DriverReconciliationService()._serialize_collection_event(event)
    assert data["scope_type"] == "place"
    assert data["scope_group_id"] == 999999
    assert data["scope_group_label"] is None
