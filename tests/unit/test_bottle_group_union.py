"""Unit coverage for the PLACE balance read (`get_place_balance`).

Renegotiated by the 2026-07-27 place re-key. This file used to test
``get_group_union_balance``, which summed one ``BottleBalance`` row per
(user, address) pair across a group. There is now exactly ONE balance row per
physical place — the address group when the address is grouped, else the
address itself — so there is no union left to compute. The *numbers* asserted
here are unchanged; only the vehicle that produces them is.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType, UserRole, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id, group_id=None):
    a = UserAddress(user_id=user_id, full_address="x, Tashkent", city="Tashkent",
                    latitude=41.31, longitude=69.28, address_group_id=group_id)
    db.session.add(a); db.session.commit()
    return a


def _place_bal(db, address, amount):
    """Seed the PLACE's single balance row for `address`.

    Group-keyed when the address is grouped, address-keyed otherwise — the
    `(address_group_id IS NULL) <> (address_id IS NULL)` CHECK admits nothing else.
    """
    b = BottleBalance(
        address_group_id=address.address_group_id,
        address_id=None if address.address_group_id is not None else address.id,
        balance=Decimal(str(amount)),
    )
    db.session.add(b); db.session.commit()
    return b


@pytest.mark.unit
class TestPlaceBalance:
    def test_ungrouped_address_is_its_own_place(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        a = _addr(db, u.id)
        _place_bal(db, a, 4)
        assert BottleTrackingService.get_place_balance(a.id) == Decimal("4.00")

    def test_grouped_addresses_across_two_users_share_one_pool(self, db):
        """Was `test_grouped_addresses_across_two_users_sum`: 3 + 2 summed across
        two per-user rows. The two coworkers now hold ONE pooled row; the total
        read at either address is still 5."""
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        canonical = CanonicalCustomer(primary_user_id=u1.id)
        db.session.add(canonical); db.session.commit()
        u1.canonical_customer_id = canonical.id
        u2.canonical_customer_id = canonical.id
        db.session.commit()
        group = AddressGroup(canonical_customer_id=canonical.id, label="home")
        db.session.add(group); db.session.commit()

        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)  # same physical home via phone-2
        _place_bal(db, a1, 5)

        # The place balance at either grouped address is the same total.
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("5.00")

    def test_over_collection_nets_within_the_place(self, db):
        """Was `test_negative_pair_nets_within_group`: 6 on one pair, -5 on the
        other, netting to 1 at read time. A place has one row, so there are no
        pairs left to net ACROSS — the netting now happens IN the row, through
        the ledger. Same arithmetic (6 - 5 == 1), same invariant: a return is
        SUMMED, never abs'd or dropped."""
        u1 = _user(db, "a@example.com", "+998900000001")
        canonical = CanonicalCustomer(primary_user_id=u1.id)
        db.session.add(canonical); db.session.commit()
        u1.canonical_customer_id = canonical.id
        db.session.commit()
        group = AddressGroup(canonical_customer_id=canonical.id, label="home")
        db.session.add(group); db.session.commit()
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u1.id, group_id=group.id)

        svc = BottleTrackingService()
        svc._create_ledger_entry(
            user_id=u1.id, address_id=a1.id,
            event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"),
        )
        # Over-collected at the OTHER member address — same pool, so it debits
        # the same row rather than stranding a negative sibling.
        svc.record_bottles_returned(u1.id, a2.id, Decimal("5"), order_id=None, delivery_id=None)
        db.session.commit()

        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("1.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("1.00")
