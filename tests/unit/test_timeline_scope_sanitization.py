"""Payment-timeline privacy split (plan 2c, Task 6).

The money engine is scope-aware: cash posted for one person can settle a
DIFFERENT person's COD debt at a shared place. The order payment timeline must
therefore render two ways:

* customer arm (``viewer_user_id`` set) — allocations funded by an event whose
  ``customer_id`` sits OUTSIDE the viewer's cluster are neutralised: the
  allocation amount only, with the funding event's free-text ``notes`` and full
  ``collection_amount`` stripped (spec §7).
* admin arm (``viewer_user_id is None``) — the opposite: scope + the dual
  attribution stamps so a reviewer can trace every cross-customer settlement
  (spec §9).
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.customer_link import CanonicalCustomer
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from shared.enums import CashCollectionSource, OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType


def _user(db, email, phone):
    u = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name=email.split("@")[0],
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _order_payment(db, user, order_number):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal("15000.00"),
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"pay_{order_number}",
        outstanding_amount=Decimal("0.00"),
        amount_collected=Decimal("15000.00"),
        created_at=datetime.now(UTC),
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


def _allocation(db, event, payment, amount):
    alloc = CashCollectionAllocation(
        cash_collection_event_id=event.id,
        payment_id=payment.id,
        order_id=payment.order_id,
        allocated_amount=amount,
        allocation_order=1,
        allocation_mode="auto",
        source_customer_id=event.customer_id,
        beneficiary_user_id=payment.user_id,
    )
    db.session.add(alloc)
    db.session.commit()
    return alloc


def _event(db, customer, amount, *, scope_type="personal", scope_snapshot=None, notes=None, delivery_id=None):
    event = CashCollectionEvent(
        customer_id=customer.id,
        amount=amount,
        unapplied_amount=Decimal("0.00"),
        source=CashCollectionSource.DELIVERY_COMPLETION,
        occurred_at=datetime.now(UTC),
        scope_type=scope_type,
        scope_snapshot=scope_snapshot,
        notes=notes,
        delivery_id=delivery_id,
    )
    db.session.add(event)
    db.session.commit()
    return event


def _link(db, *users):
    """Put every given user in one canonical cluster."""
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.flush()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def _alloc_entry(timeline):
    entries = [e for e in timeline["timeline"] if e["type"] == "cash_collection_allocation"]
    assert len(entries) == 1, f"expected exactly one allocation entry, got {len(entries)}"
    return entries[0]


@pytest.mark.unit
class TestTimelineScopeSanitization:
    def test_out_of_cluster_event_renders_neutral_for_customer(self, db):
        viewer = _user(db, "v@example.com", "+998900000001")
        coworker = _user(db, "c@example.com", "+998900000002")
        order, payment = _order_payment(db, viewer, "ORD-1")
        event = _event(
            db,
            coworker,
            Decimal("50000.00"),
            scope_type="place",
            notes="internal driver note",
        )
        _allocation(db, event, payment, Decimal("15000.00"))

        timeline = CashCollectionService().get_order_payment_timeline(order.id, viewer_user_id=viewer.id)
        alloc_entry = _alloc_entry(timeline)

        # Kept: what the customer legitimately needs.
        assert alloc_entry["settled_by"] == "workplace_collection"
        assert alloc_entry["allocated_amount"] == 15000.0
        assert alloc_entry["allocation_mode"] == "auto"
        assert alloc_entry["reversed_at"] is None
        assert alloc_entry["timestamp"] is not None
        # Stripped, field by field: another person's payment size + internal prose.
        assert alloc_entry["collection_amount"] is None
        assert alloc_entry["notes"] is None
        assert alloc_entry["collection_event_id"] is None
        assert alloc_entry["collection_source"] is None
        assert alloc_entry["delivery_id"] is None
        # Admin-only stamps never reach a customer.
        assert "source_customer_id" not in alloc_entry
        assert "beneficiary_user_id" not in alloc_entry
        assert "scope_type" not in alloc_entry
        assert "scope_group_id" not in alloc_entry
        assert "scope_group_label" not in alloc_entry
        # Belt and braces: no serialized value anywhere carries the foreign
        # amount or the internal note.
        rendered = repr(alloc_entry)
        assert "internal driver note" not in rendered
        assert "50000" not in rendered

    def test_ex_member_frozen_scope_correction_also_renders_neutral(self, db):
        """A frozen-snapshot correction after the viewer left the group.

        The funding event is still owned by the workplace payer, so the viewer
        sees a workplace settlement rather than a mystery credit.
        """
        viewer = _user(db, "ex@example.com", "+998900000001")
        payer = _user(db, "payer@example.com", "+998900000002")
        order, payment = _order_payment(db, viewer, "ORD-EX")
        event = _event(
            db,
            payer,
            Decimal("90000.00"),
            scope_type="place",
            scope_snapshot={
                "group_id": 777,
                "address_ids": [],
                "place_user_ids": [],
                "orderer_cluster_user_ids": [],
            },
            notes="correction: recount at office",
        )
        _allocation(db, event, payment, Decimal("15000.00"))

        timeline = CashCollectionService().get_order_payment_timeline(order.id, viewer_user_id=viewer.id)
        alloc_entry = _alloc_entry(timeline)

        assert alloc_entry["settled_by"] == "workplace_collection"
        assert alloc_entry["allocated_amount"] == 15000.0
        assert alloc_entry["notes"] is None
        assert alloc_entry["collection_amount"] is None
        assert "correction: recount at office" not in repr(alloc_entry)

    def test_own_event_renders_full_for_customer_without_admin_stamps(self, db):
        viewer = _user(db, "v@example.com", "+998900000001")
        order, payment = _order_payment(db, viewer, "ORD-1")
        event = _event(db, viewer, Decimal("15000.00"), notes="ok")
        _allocation(db, event, payment, Decimal("15000.00"))

        timeline = CashCollectionService().get_order_payment_timeline(order.id, viewer_user_id=viewer.id)
        alloc_entry = _alloc_entry(timeline)

        assert alloc_entry["collection_amount"] == 15000.0
        assert alloc_entry["notes"] == "ok"
        assert alloc_entry["collection_event_id"] == event.id
        assert alloc_entry["collection_source"] == CashCollectionSource.DELIVERY_COMPLETION.value
        assert "settled_by" not in alloc_entry
        assert "source_customer_id" not in alloc_entry
        assert "scope_type" not in alloc_entry

    def test_cluster_sibling_event_renders_full_for_customer(self, db):
        """A linked second phone of the SAME person is the same wallet."""
        viewer = _user(db, "v@example.com", "+998900000001")
        sibling = _user(db, "v2@example.com", "+998900000002")
        _link(db, viewer, sibling)
        order, payment = _order_payment(db, viewer, "ORD-1")
        event = _event(db, sibling, Decimal("40000.00"), scope_type="cluster", notes="paid at door")
        _allocation(db, event, payment, Decimal("15000.00"))

        timeline = CashCollectionService().get_order_payment_timeline(order.id, viewer_user_id=viewer.id)
        alloc_entry = _alloc_entry(timeline)

        assert alloc_entry["collection_amount"] == 40000.0
        assert alloc_entry["notes"] == "paid at door"
        assert alloc_entry["collection_event_id"] == event.id
        assert "settled_by" not in alloc_entry
        assert "scope_type" not in alloc_entry

    def test_admin_arm_includes_scope_and_dual_stamps(self, db):
        owner = _user(db, "o@example.com", "+998900000001")
        payer = _user(db, "p@example.com", "+998900000002")
        order, payment = _order_payment(db, owner, "ORD-1")
        event = _event(
            db,
            payer,
            Decimal("50000.00"),
            scope_type="place",
            scope_snapshot={
                "group_id": 12345,
                "address_ids": [],
                "place_user_ids": [],
                "orderer_cluster_user_ids": [],
            },
            notes="collected at reception",
        )
        _allocation(db, event, payment, Decimal("15000.00"))

        timeline = CashCollectionService().get_order_payment_timeline(order.id)
        alloc_entry = _alloc_entry(timeline)

        assert alloc_entry["scope_type"] == "place"
        assert alloc_entry["scope_group_id"] == 12345
        assert alloc_entry["source_customer_id"] == payer.id
        assert alloc_entry["beneficiary_user_id"] == owner.id
        # Admin keeps the full source view.
        assert alloc_entry["collection_amount"] == 50000.0
        assert alloc_entry["notes"] == "collected at reception"
        assert "settled_by" not in alloc_entry

    def test_admin_arm_resolves_scope_group_label(self, db):
        from business_app.models.customer_link import AddressGroup

        group = AddressGroup(label="Office 12")
        db.session.add(group)
        db.session.commit()

        owner = _user(db, "o@example.com", "+998900000001")
        payer = _user(db, "p@example.com", "+998900000002")
        order, payment = _order_payment(db, owner, "ORD-1")
        event = _event(
            db,
            payer,
            Decimal("50000.00"),
            scope_type="place",
            scope_snapshot={"group_id": group.id, "address_ids": []},
        )
        _allocation(db, event, payment, Decimal("15000.00"))

        alloc_entry = _alloc_entry(CashCollectionService().get_order_payment_timeline(order.id))
        assert alloc_entry["scope_group_id"] == group.id
        assert alloc_entry["scope_group_label"] == "Office 12"

    def test_admin_arm_personal_event_degrades_to_todays_values(self, db):
        """Unlinked + ungrouped baseline: nothing changes but the added keys."""
        owner = _user(db, "o@example.com", "+998900000001")
        order, payment = _order_payment(db, owner, "ORD-1")
        event = _event(db, owner, Decimal("15000.00"), notes="cash at door")
        _allocation(db, event, payment, Decimal("15000.00"))

        alloc_entry = _alloc_entry(CashCollectionService().get_order_payment_timeline(order.id))

        assert alloc_entry["scope_type"] == "personal"
        assert alloc_entry["scope_group_id"] is None
        assert alloc_entry["scope_group_label"] is None
        assert alloc_entry["source_customer_id"] == owner.id
        assert alloc_entry["beneficiary_user_id"] == owner.id
        assert alloc_entry["collection_amount"] == 15000.0
        assert alloc_entry["notes"] == "cash at door"
        assert alloc_entry["allocated_amount"] == 15000.0

    def test_default_call_is_admin_arm(self, db):
        """Every existing caller (no kwarg) keeps the un-sanitized rendering."""
        owner = _user(db, "o@example.com", "+998900000001")
        payer = _user(db, "p@example.com", "+998900000002")
        order, payment = _order_payment(db, owner, "ORD-1")
        event = _event(db, payer, Decimal("50000.00"), scope_type="place", notes="internal")
        _allocation(db, event, payment, Decimal("15000.00"))

        alloc_entry = _alloc_entry(CashCollectionService().get_order_payment_timeline(order.id))
        assert alloc_entry["notes"] == "internal"
        assert alloc_entry["collection_amount"] == 50000.0
        assert "settled_by" not in alloc_entry


@pytest.mark.unit
class TestTimelineViewerPlumbing:
    """The customer routes must select the customer arm.

    Without these, reverting a call site would silently restore the leak while
    the service-level tests above stayed green.
    """

    _PATCH_TARGET = "business_app.services.cash_collection_service.CashCollectionService.get_order_payment_timeline"

    def _spy(self, monkeypatch):
        from unittest.mock import Mock

        spy = Mock(return_value={"payment_id": 1, "timeline": []})
        monkeypatch.setattr(self._PATCH_TARGET, spy)
        return spy

    def test_customer_order_detail_passes_viewer(
        self, client, db, sample_order, sample_payment, sample_user, auth_headers, monkeypatch
    ):
        spy = self._spy(monkeypatch)
        response = client.get(f"/api/v1/orders/{sample_order.id}", headers=auth_headers)
        assert response.status_code == 200
        spy.assert_called_once_with(sample_order.id, viewer_user_id=sample_user.id)

    def test_customer_order_tracking_passes_viewer(
        self, client, db, sample_order, sample_payment, sample_user, auth_headers, monkeypatch
    ):
        spy = self._spy(monkeypatch)
        response = client.get(f"/api/v1/orders/{sample_order.id}/track", headers=auth_headers)
        assert response.status_code == 200
        spy.assert_called_once_with(sample_order.id, viewer_user_id=sample_user.id)

    def test_customer_payment_timeline_route_passes_viewer(
        self, client, db, sample_order, sample_payment, sample_user, auth_headers, monkeypatch
    ):
        spy = self._spy(monkeypatch)
        response = client.get(f"/api/v1/payments/orders/{sample_order.id}/timeline", headers=auth_headers)
        assert response.status_code == 200
        spy.assert_called_once_with(sample_order.id, viewer_user_id=sample_user.id)

    def test_admin_order_detail_keeps_admin_arm(
        self, client, db, sample_order, sample_payment, admin_auth_headers, monkeypatch
    ):
        spy = self._spy(monkeypatch)
        response = client.get(f"/api/v1/admin/orders/{sample_order.id}", headers=admin_auth_headers)
        assert response.status_code == 200
        spy.assert_called_once_with(sample_order.id)
