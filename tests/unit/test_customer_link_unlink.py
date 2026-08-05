from datetime import datetime, UTC, timedelta
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import BottleLedgerEventType, OrderStatus, UserRole, UserStatus, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone, *, created=None):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=created or datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    db.session.add(a); db.session.commit()
    return a


@pytest.mark.unit
class TestUnlink:
    def test_unlink_detaches_and_promotes_primary_but_leaves_groups(self, db):
        """Phase 2: identity and geography are independent — unlink NEVER
        changes place-group membership."""
        u1 = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=5))
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")

        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        group = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="same home")

        result = svc.unlink_account(u1.id, actor_admin_id=admin.id, reason="mislink")

        db.session.refresh(u1); db.session.refresh(a1)
        assert u1.canonical_customer_id is None
        assert a1.address_group_id == group.id            # membership SURVIVES unlink
        assert result["remaining_member_ids"] == [u2.id]
        assert result["new_primary_user_id"] == u2.id
        assert CustomerLinkEvent.query.filter_by(event_type="unlink").count() == 1

    def test_unlink_lists_non_terminal_orders(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        order = Order(user_id=u2.id, order_number="ORD-OFD", status=OrderStatus.OUT_FOR_DELIVERY,
                      subtotal=0, delivery_fee=0, discount_amount=0, loyalty_discount=0, total_amount=0,
                      created_at=datetime.now(UTC))
        db.session.add(order); db.session.commit()

        result = svc.unlink_account(u1.id, actor_admin_id=admin.id, reason="r")
        assert order.id in [o["order_id"] for o in result["non_terminal_orders"]]

    def test_unlink_returns_empty_shape_when_not_linked(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        admin = _user(db, "admin@example.com", "+998900000009")

        result = CustomerLinkService().unlink_account(u1.id, actor_admin_id=admin.id, reason="r")

        assert result == {
            "canonical_customer_id": None,
            "remaining_member_ids": [],
            "new_primary_user_id": None,
            "non_terminal_orders": [],
        }
        assert CustomerLinkEvent.query.filter_by(event_type="unlink").count() == 0

    def test_unlink_calls_reservation_release_hook(self, db):
        """The hook is a no-op in 2a; Plan 2b wires the real
        CashCollectionService.release_out_of_scope_reservations through it."""
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")

        calls = []
        svc._release_out_of_scope_reservations = lambda leaving, remaining: calls.append(
            (leaving, remaining)) or 0

        svc.unlink_account(u1.id, actor_admin_id=admin.id, reason="r")
        assert calls == [([u1.id], [u2.id])]


@pytest.mark.unit
class TestUnlinkNeverTouchesBottles:
    def test_negative_grouped_place_survives_unlink_untouched(self, db):
        """Removal from a place group is the geography axis (see
        tests/unit/test_place_group_ungroup_split.py); netting itself is retired
        (spec §8). Unlink must not write a single ledger row either, even when
        the shared place is negative.

        Was `test_negative_grouped_pair_survives_unlink_untouched`, which seeded
        -4 on u1's pair and +6 on u2's. A place holds ONE row now, so a negative
        *sibling pair inside a positive group* is no longer representable; the
        temptation the test guards against is a negative PLACE, seeded here as
        -4.00 so the "even when negative" half of the intent survives intact.
        """
        u1 = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=5))
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        group = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="home")
        db.session.add(BottleBalance(address_group_id=group.id, balance=Decimal("-4.00")))
        db.session.commit()

        svc.unlink_account(u1.id, actor_admin_id=admin.id, reason="mislink")

        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 0
        assert BottleBalance.query.filter_by(address_group_id=group.id).one().balance == Decimal("-4.00")
        assert BottleBalance.query.count() == 1  # no per-member row was minted either
        db.session.refresh(a1)
        assert a1.address_group_id == group.id

    def test_legacy_canonical_owned_group_is_neither_ejected_nor_netted(self, db):
        """Pins the DELETION itself, not just the new behaviour.

        The Phase-1 eject + net-out keyed on
        ``AddressGroup.canonical_customer_id == <the cluster's canonical>``.
        Place groups are ownerless, so the ownerless-group tests above cannot
        catch that code coming back — only a group on the DEPRECATED owned
        column can. Built with the raw model (no service write-path mints owned
        groups any more) purely as a regression pin.
        """
        u1 = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=5))
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        link = svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")

        group = AddressGroup(canonical_customer_id=link["canonical_customer_id"], label="legacy")
        db.session.add(group); db.session.commit()
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        a1.address_group_id = group.id
        a2.address_group_id = group.id
        # Negative PLACE row (see the sibling test's docstring for why the old
        # -4/+6 pair split is no longer representable).
        db.session.add(BottleBalance(address_group_id=group.id, balance=Decimal("-4.00")))
        db.session.commit()

        svc.unlink_account(u1.id, actor_admin_id=admin.id, reason="mislink")

        db.session.refresh(a1); db.session.refresh(group)
        assert a1.address_group_id == group.id                 # NOT ejected
        assert group.canonical_customer_id == link["canonical_customer_id"]
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 0
        assert BottleBalance.query.filter_by(address_group_id=group.id).one().balance == Decimal("-4.00")
        assert BottleBalance.query.count() == 1
        assert "shortfall" not in (
            CustomerLinkEvent.query.filter_by(event_type="unlink").one().reason or "").lower()
