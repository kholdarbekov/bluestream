"""Integration / e2e tests: delivery & return effects on the PLACE balance.

Renegotiated by the 2026-07-27 place re-key. Balances used to be written PER
``(user_id, address_id)`` and SUMMED at read time across an address group
(``get_group_union_balance``). A physical PLACE — the address group when the
address is grouped, else the address itself — now owns exactly ONE balance row,
so per-pair isolation is deleted by design and the union has nothing to sum.
Every asserted TOTAL below is carried over unchanged; the per-pair *slices* are
replaced by the stronger "there is only one row" assertion, and the isolation
that genuinely survives — LEDGER attribution stays per-person — is asserted
explicitly.

Covers contract items 1, 2, 3, 4, 10 as re-stated for places:

  1. Attribution isolation — a delivery through (u2, addrB) writes a ledger row
     for u2 only; the grouped OTHER member's ledger is untouched (their shared
     POOL does move, and that is the point of the re-key).
  2. The place reflects cross-user grouped writes — after +N through addrB
     where addrA & addrB share a group, ``get_place_balance`` at BOTH addresses
     rises by N.
  3. Net-neutral delivery (THE USER-REPORTED regression) — deliver +N then
     return -N through the same address => the place is unchanged and the other
     member's ledger is unchanged. That is CORRECT; this pins it so it can't
     silently change.
  4. Net delivery / net return move the place balance by exactly the signed amount.
 10. TRUE e2e — OrderService drives the DELIVERED status edge
     (order_service.py ~1660 `_handle_status_change_actions`), which fires
     `record_bottles_delivered` to (order.user_id, order.delivery_address_id).
     We assert the real DELIVERY ledger row + place balance reflect it.
     Plus (f) idempotency: re-recording the same order_id never double-applies.

Numbers are deliberately distinct so a sign error is caught.

The grouped+linked scenario is built directly via models
(CanonicalCustomer / AddressGroup), matching the existing customer_link and
bottle-union unit tests. Uses the function-scoped `db` fixture (create_all /
drop_all per test); each test builds its own users / addresses / group.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order, OrderItem
from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.order_service import OrderService
from shared.enums import BottleLedgerEventType, OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _user(db, email, phone, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL):
    u = User(
        email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
        first_name="T", last_name="U", user_type=user_type, role=role,
        is_verified=True, created_at=datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id, *, group_id=None, full="home"):
    a = UserAddress(
        user_id=user_id, full_address=full, city="Tashkent",
        latitude=41.31, longitude=69.28, address_group_id=group_id,
    )
    db.session.add(a)
    db.session.commit()
    return a


def _link_and_group(db, u1, u2):
    """Link u1 & u2 under one canonical customer and create a shared address
    group. Returns (canonical, group). Direct-model setup mirrors the existing
    customer_link tests (fast + reliable)."""
    canonical = CanonicalCustomer(primary_user_id=u1.id)
    db.session.add(canonical)
    db.session.commit()
    u1.canonical_customer_id = canonical.id
    u2.canonical_customer_id = canonical.id
    db.session.commit()
    group = AddressGroup(canonical_customer_id=canonical.id, label="home")
    db.session.add(group)
    db.session.commit()
    return canonical, group


def _bottle_product(db, *, per_unit):
    """A product that tracks returnable bottles at `per_unit` per unit."""
    from business_app.models.product import Product, ProductCategory

    category = ProductCategory(name="Water", description="w", is_active=True)
    db.session.add(category)
    db.session.commit()
    product = Product(
        name="Pure Water 19L", description="d", category_id=category.id,
        size="19L", volume=19.0, volume_unit="L", base_price=Decimal("15000.00"),
        stock_quantity=100, min_stock_level=1, max_stock_level=500, is_active=True,
        tracks_returnable_bottles=True, returnable_bottles_per_unit=Decimal(str(per_unit)),
        created_at=datetime.now(UTC),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _order_with_item(db, user, product, address, *, order_number, quantity, status):
    order = Order(
        user_id=user.id, order_number=order_number, status=status,
        subtotal=Decimal("30000.00"), delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("30000.00"), delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    item = OrderItem(
        order_id=order.id, product_id=product.id, quantity=quantity,
        unit_price=Decimal("15000.00"),
        total_price=Decimal("15000.00") * Decimal(str(quantity)),
    )
    db.session.add(item)
    db.session.commit()
    return order


# --------------------------------------------------------------------------- #
# (a) TRUE e2e — OrderService DELIVERED edge fires the delivery primitive
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestOrderFlowDeliveredE2E:
    def test_delivered_status_edge_records_ledger_and_place_balance(self, app, db):
        """Drive OrderService.update_order_status(...DELIVERED) for an order to a
        grouped address; assert the REAL DELIVERY ledger row attributed to u2 and
        that the PLACE balance at BOTH addresses reflects it (contract 10 + 2).

        The old ``get_balance(u1, addrA) is None`` assertion — u1's own pair was
        never written — is replaced by its place-scoped equivalent: both
        addresses resolve to the SAME single row, and u1 wrote no ledger entry.
        """
        u1 = _user(db, "e2e1@example.com", "+998900100001")
        u2 = _user(db, "e2e2@example.com", "+998900100002")
        admin = _user(db, "e2eadmin@example.com", "+998900100009", role=UserRole.ADMIN,
                      user_type=UserType.STAFF)
        _canonical, group = _link_and_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id, full="A")
        addr_u2 = _addr(db, u2.id, group_id=group.id, full="B")

        # product: 2 returnable bottles per unit; order line qty 2 => 4 bottles.
        product = _bottle_product(db, per_unit="2")
        order = _order_with_item(
            db, u2, product, addr_u2, order_number="ORD-E2E-1",
            quantity=2, status=OrderStatus.CONFIRMED,
        )

        # The place starts empty (no balance row yet).
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("0.00")

        # REAL product path: CONFIRMED -> DELIVERED transition.
        OrderService().update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)

        # DELIVERY ledger row exists with the order idempotency key, on (u2, addrB).
        delivery_row = BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").first()
        assert delivery_row is not None
        assert delivery_row.event_type == BottleLedgerEventType.DELIVERY
        assert delivery_row.user_id == u2.id
        assert delivery_row.address_id == addr_u2.id
        assert delivery_row.quantity == Decimal("4.00")

        # The place's balance row landed, reachable from EITHER member address —
        # one row, not two that happen to sum.
        row_b = BottleTrackingService.get_place_balance_row(addr_u2.id)
        assert row_b is not None
        assert row_b.balance == Decimal("4.00")
        assert BottleTrackingService.get_place_balance_row(addr_u1.id).id == row_b.id
        assert BottleBalance.query.count() == 1

        # u1 wrote nothing — attribution stays per-person even though the pool is shared.
        assert BottleLedger.query.filter_by(user_id=u1.id).count() == 0

        # The place at either grouped address reflects the cross-user delivery.
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("4.00")
        assert BottleTrackingService.get_place_balance(addr_u2.id) == Decimal("4.00")

    def test_delivered_edge_is_idempotent_when_refired(self, app, db):
        """Re-driving the DELIVERED edge for the same order must not double-apply
        the delivery (idempotency key delivery:{order_id})."""
        u = _user(db, "e2eidem@example.com", "+998900100011")
        admin = _user(db, "e2eidemadmin@example.com", "+998900100019", role=UserRole.ADMIN,
                      user_type=UserType.STAFF)
        addr = _addr(db, u.id, full="solo")
        product = _bottle_product(db, per_unit="3")
        order = _order_with_item(
            db, u, product, addr, order_number="ORD-E2E-IDEM",
            quantity=2, status=OrderStatus.CONFIRMED,  # 3*2 = 6 bottles
        )

        svc = OrderService()
        svc.update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)

        # Re-fire only the status-change actions (the transition itself is now
        # terminal). This is the same trigger method that records the delivery.
        db.session.refresh(order)
        svc._handle_status_change_actions(order, OrderStatus.DELIVERED, updated_by=admin.id, commit=True)

        rows = BottleLedger.query.filter_by(
            order_id=order.id, event_type=BottleLedgerEventType.DELIVERY
        ).all()
        assert len(rows) == 1  # not doubled
        assert BottleTrackingService.get_place_balance(addr.id) == Decimal("6.00")


# --------------------------------------------------------------------------- #
# (b) Per-pair isolation
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestPerPairIsolation:
    def test_delivery_to_grouped_member_pools_at_the_place_and_spares_the_other_ledger(self, app, db):
        """+N through addrB pools into the shared place; the grouped member
        (u1, addrA) LEDGER is unchanged (contract 1, re-stated for places).

        Was ``..._leaves_other_row_and_ledger_untouched``, which asserted u2's
        pair reached 7 while u1's stayed at 5. Those are one pool now: 5 + 7 =
        12. Both quantities survive in that total, and the half of the old claim
        that is still true — u1 wrote nothing — is asserted unchanged.
        """
        u1 = _user(db, "iso1@example.com", "+998900200001")
        u2 = _user(db, "iso2@example.com", "+998900200002")
        _canonical, group = _link_and_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id, full="A")
        addr_u2 = _addr(db, u2.id, group_id=group.id, full="B")

        svc = BottleTrackingService()
        # Seed u1's own pair with a real INITIAL_BALANCE ledger row (=5).
        svc.set_initial_balance(u1.id, addr_u1.id, Decimal("5"), actor_user_id=u1.id)
        u1_ledger_before = BottleLedger.query.filter_by(user_id=u1.id, address_id=addr_u1.id).count()
        assert u1_ledger_before == 1

        # Deliver 7 to u2's grouped pair.
        svc.record_bottles_delivered(
            order_id=9001, user_id=u2.id, address_id=addr_u2.id, quantity=Decimal("7"),
        )
        db.session.commit()

        # The shared place holds u1's seeded 5 plus the delivered 7.
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("12.00")
        assert BottleTrackingService.get_place_balance(addr_u2.id) == Decimal("12.00")
        assert BottleBalance.query.count() == 1
        # u1's ledger is untouched (still only the initial-balance row).
        assert (
            BottleLedger.query.filter_by(user_id=u1.id, address_id=addr_u1.id).count()
            == u1_ledger_before
        )
        # u2's pair has exactly one DELIVERY row.
        u2_delivery_rows = BottleLedger.query.filter_by(
            user_id=u2.id, address_id=addr_u2.id, event_type=BottleLedgerEventType.DELIVERY
        ).all()
        assert len(u2_delivery_rows) == 1
        assert u2_delivery_rows[0].quantity == Decimal("7.00")


# --------------------------------------------------------------------------- #
# (c) Union reflects the cross-user grouped delivery
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestUnionReflectsCrossUserDelivery:
    def test_place_rises_by_delivered_qty_at_both_addresses(self, app, db):
        """The place holds 3; delivering +5 through addrB makes it 8, read at
        BOTH addresses (contract 2).

        The old "each pair keeps its own slice" (3 and 5) is deleted by design;
        the replacement — exactly one balance row — is strictly stronger.
        """
        u1 = _user(db, "un1@example.com", "+998900300001")
        u2 = _user(db, "un2@example.com", "+998900300002")
        _canonical, group = _link_and_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id, full="A")
        addr_u2 = _addr(db, u2.id, group_id=group.id, full="B")

        svc = BottleTrackingService()
        svc.set_initial_balance(u1.id, addr_u1.id, Decimal("3"), actor_user_id=u1.id)
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("3.00")

        svc.record_bottles_delivered(
            order_id=9101, user_id=u2.id, address_id=addr_u2.id, quantity=Decimal("5"),
        )
        db.session.commit()

        # The place rose by exactly 5, read at both grouped addresses.
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("8.00")
        assert BottleTrackingService.get_place_balance(addr_u2.id) == Decimal("8.00")
        # It is ONE pool — the cross-user write minted no second row.
        assert BottleBalance.query.count() == 1


# --------------------------------------------------------------------------- #
# (d) Net-neutral delivery — THE USER-REPORTED regression pin
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestNetNeutralDeliveryRegression:
    def test_deliver_then_return_leaves_the_place_and_other_ledger_unchanged(self, app, db):
        """USER-REPORTED SCENARIO (regression pin): a cross-user grouped
        net-neutral delivery — deliver +3 then return -3 through addrB — leaves
        the PLACE balance unchanged and u1's ledger unchanged. This is CORRECT
        behavior (contract 3). The old "u2's pair nets to 0" is subsumed: there
        is one pool, and it is exactly where it started."""
        u1 = _user(db, "nn1@example.com", "+998900400001")
        u2 = _user(db, "nn2@example.com", "+998900400002")
        _canonical, group = _link_and_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id, full="A")
        addr_u2 = _addr(db, u2.id, group_id=group.id, full="B")

        svc = BottleTrackingService()
        svc.set_initial_balance(u1.id, addr_u1.id, Decimal("3"), actor_user_id=u1.id)
        place_before = BottleTrackingService.get_place_balance(addr_u1.id)
        assert place_before == Decimal("3.00")

        # Deliver +3, then return -3 to the SAME (u2, addrB) pair.
        svc.record_bottles_delivered(
            order_id=9201, user_id=u2.id, address_id=addr_u2.id, quantity=Decimal("3"),
        )
        svc.record_bottles_returned(
            u2.id, addr_u2.id, Decimal("3"), order_id=9201, delivery_id=None,
        )
        db.session.commit()

        # The place is unchanged — the net-neutral delivery added nothing.
        assert BottleTrackingService.get_place_balance(addr_u1.id) == place_before == Decimal("3.00")
        assert BottleTrackingService.get_place_balance(addr_u2.id) == place_before
        # u1's ledger is untouched (still only the initial-balance row).
        assert BottleLedger.query.filter_by(user_id=u1.id).count() == 1
        # u2 wrote both legs.
        assert BottleLedger.query.filter_by(user_id=u2.id).count() == 2


# --------------------------------------------------------------------------- #
# (e) Net delivery (no return) increases union; a later return decreases it
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestNetDeliveryMovesUnion:
    def test_net_delivery_increases_then_return_decreases_the_place(self, app, db):
        """Deliver +5 => place += 5; then return -2 => place -= 2 (contract 4).
        Distinct qtys so a sign/rounding error is caught."""
        u1 = _user(db, "nd1@example.com", "+998900500001")
        u2 = _user(db, "nd2@example.com", "+998900500002")
        _canonical, group = _link_and_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id, full="A")
        addr_u2 = _addr(db, u2.id, group_id=group.id, full="B")

        svc = BottleTrackingService()
        svc.set_initial_balance(u1.id, addr_u1.id, Decimal("2"), actor_user_id=u1.id)
        assert BottleTrackingService.get_place_balance(addr_u2.id) == Decimal("2.00")

        # Net delivery (no return): +5.
        svc.record_bottles_delivered(
            order_id=9301, user_id=u2.id, address_id=addr_u2.id, quantity=Decimal("5"),
        )
        db.session.commit()
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("7.00")

        # A later return of 2 on the same pair: union -= 2.
        svc.record_bottles_returned(
            u2.id, addr_u2.id, Decimal("2"), order_id=9301, delivery_id=None,
        )
        db.session.commit()
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(addr_u2.id) == Decimal("5.00")
        assert BottleBalance.query.count() == 1


# --------------------------------------------------------------------------- #
# (f) Idempotency at the primitive level
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestDeliveryIdempotency:
    def test_record_bottles_delivered_twice_same_order_no_double_apply(self, app, db):
        """Calling record_bottles_delivered twice with the same order_id returns
        the existing ledger row and does NOT double-apply the balance
        (idempotency key delivery:{order_id}) — contract (f)."""
        u = _user(db, "idem@example.com", "+998900600001")
        addr = _addr(db, u.id, full="solo")

        svc = BottleTrackingService()
        first = svc.record_bottles_delivered(
            order_id=9401, user_id=u.id, address_id=addr.id, quantity=Decimal("4"),
        )
        second = svc.record_bottles_delivered(
            order_id=9401, user_id=u.id, address_id=addr.id, quantity=Decimal("4"),
        )
        db.session.commit()

        # Same ledger row returned; balance applied once.
        assert second.id == first.id
        assert BottleTrackingService.get_place_balance(addr.id) == Decimal("4.00")
        assert (
            BottleLedger.query.filter_by(idempotency_key="delivery:9401").count() == 1
        )
        # Exactly one DELIVERY ledger row overall for this place.
        assert (
            BottleLedger.query.filter_by(
                user_id=u.id, address_id=addr.id, event_type=BottleLedgerEventType.DELIVERY
            ).count()
            == 1
        )
