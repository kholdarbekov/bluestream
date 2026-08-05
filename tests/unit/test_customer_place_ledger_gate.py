"""Three-arm customer authorization gate + redacted place ledger (plan 2c task 4).

A place group can span DIFFERENT people (coworkers). Transparency inside the
group is intentional, so the authorization gate is the whole safety story:
these tests pin each allow arm, the stranger/missing denial, and the exact
field set the customer-facing serializer is allowed to emit.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.customer_link import CanonicalCustomer
from business_app.models.user import User, UserAddress
from business_app.serializers.bottle_serializers import serialize_customer_place_ledger_entry
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, UserRole, UserType

LAT, LNG = 41.3111, 69.2797


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="First" + email[0].upper(), last_name="Last",
             user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, is_verified=True,
             created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user):
    a = UserAddress(user_id=user.id, title="work", full_address="Office",
                    latitude=LAT, longitude=LNG)
    db.session.add(a)
    db.session.commit()
    return a


def _link(db, users):
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()


@pytest.mark.unit
class TestThreeArmGate:
    def test_own_address_allowed(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        a = _address(db, u)
        assert CustomerLinkService().can_view_address_history(u.id, a.id) is True

    def test_place_group_member_allowed(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        CustomerLinkService().create_place_group([a1.id, a2.id],
                                                 acting_admin_id=admin.id, reason="office")
        assert CustomerLinkService().can_view_address_history(u2.id, a1.id) is True

    def test_cluster_sibling_allowed(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1 = _address(db, u1)
        _link(db, [u1, u2])
        assert CustomerLinkService().can_view_address_history(u2.id, a1.id) is True

    def test_member_of_a_different_place_group_denied(self, db):
        """Grouping is not a global pass — only co-membership of THIS group is."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        u4 = _user(db, "d@example.com", "+998900000004")
        a1, a2 = _address(db, u1), _address(db, u2)
        a3, a4 = _address(db, u3), _address(db, u4)
        svc = CustomerLinkService()
        svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="office A")
        svc.create_place_group([a3.id, a4.id], acting_admin_id=admin.id, reason="office B")
        assert svc.can_view_address_history(u3.id, a1.id) is False

    def test_stranger_and_missing_denied(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1 = _address(db, u1)
        svc = CustomerLinkService()
        assert svc.can_view_address_history(u2.id, a1.id) is False
        assert svc.can_view_address_history(u1.id, 999999) is False


@pytest.mark.unit
class TestPlaceLedgerAndRedaction:
    def test_place_ledger_unions_members_and_serializer_redacts(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        CustomerLinkService().create_place_group([a1.id, a2.id],
                                                 acting_admin_id=admin.id, reason="office")
        svc = BottleTrackingService()
        # A place is one pool and can only be seeded once (BOTTLE_INITIAL_BALANCE_EXISTS
        # guard); u2's entry is a second movement on the same place, not a second seed.
        svc.set_initial_balance(u1.id, a1.id, Decimal("2"), actor_user_id=admin.id)
        svc._create_ledger_entry(user_id=u2.id, address_id=a2.id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))

        result = svc.get_place_ledger(a1.id, page=1, per_page=20)
        assert result["total"] == 2  # both members' entries visible from either address

        rows = [serialize_customer_place_ledger_entry(e, viewer_user_id=u1.id)
                for e in result["items"]]
        own = [r for r in rows if r["is_own"]]
        other = [r for r in rows if not r["is_own"]]
        assert len(own) == 1 and len(other) == 1
        assert other[0]["member_name"].startswith("First")
        forbidden = {"user_id", "actor_user_id", "notes", "idempotency_key",
                     "entry_metadata", "balance_after", "user_phone"}
        allowed = {"id", "address_id", "event_type", "quantity", "occurred_at",
                   "order_id", "order_number", "member_name", "is_own"}
        for r in rows:
            assert forbidden.isdisjoint(r.keys())
            # Whitelist, not just a blacklist: any newly leaked field fails here.
            assert set(r.keys()) == allowed

    def test_ungrouped_place_ledger_equals_pair_ledger(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u = _user(db, "solo@example.com", "+998900000007")
        a = _address(db, u)
        svc = BottleTrackingService()
        svc.set_initial_balance(u.id, a.id, Decimal("4"), actor_user_id=admin.id)
        result = svc.get_place_ledger(a.id)
        assert result["total"] == 1
        assert result["items"][0].address_id == a.id
