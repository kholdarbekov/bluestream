"""Fine issuance rides on the PLACE balance (spec §4.3) + two long-standing bugs.

Three behaviours are pinned here:

1. ``FINE_ISSUED`` ledger metadata records the REAL ``fine_id`` — the metadata
   used to be built before the flush that assigns the id, so every issuance
   entry stored ``fine_id: None`` and could never be joined back to its fine.
2. Fine EVALUATION context is the PLACE, not a per-person slice. Two coworkers
   at one office hold ONE pool of empties, and that pool is what an auditor must
   see at issue time — a coworker's return nets into the same number the fined
   member is judged against. The fine ENTITY stays per-person: the admin still
   chooses which member and which address, and the scope is frozen on the fine.
3. The admin fine-by-address path returns 400 (not a 500) when the request
   carries no ``address_id``. A place that has never moved a bottle is a
   legitimate zero-balance issuance, not an error — the old 500 came from
   resolving a ``bottle_balance_id`` that no longer exists.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleLedger
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, UserRole, UserType

LAT, LNG = 41.3111, 69.2797


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name=email.split("@")[0], user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user):
    a = UserAddress(user_id=user.id, title="work", full_address="Office",
                    city="Tashkent", latitude=LAT, longitude=LNG)
    db.session.add(a)
    db.session.commit()
    return a


def _move(db, svc, user, address, qty, event_type=BottleLedgerEventType.DELIVERY):
    """One bottle movement at a place, attributed to `user`.

    A place can be SEEDED only once (BOTTLE_INITIAL_BALANCE_EXISTS), so every
    movement after the first is an ordinary ledger entry, not a second seed.
    """
    entry = svc._create_ledger_entry(user_id=user.id, address_id=address.id,
                                     event_type=event_type, quantity=Decimal(str(qty)))
    db.session.commit()
    return entry


def _fine_entry(user_id):
    return BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.FINE_ISSUED, user_id=user_id).one()


@pytest.mark.unit
class TestFineIssuance:
    def test_fine_issued_metadata_has_real_fine_id_and_place_balance(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        CustomerLinkService().create_place_group([a1.id, a2.id],
                                                 acting_admin_id=admin.id, reason="office")
        svc = BottleTrackingService()
        svc.set_initial_balance(u1.id, a1.id, Decimal("2"), actor_user_id=admin.id)
        _move(db, svc, u2, a2, 3)  # the coworker's delivery joins the SAME pool

        fine = svc.issue_fine(user_id=u1.id, address_id=a1.id,
                              quantity=Decimal("1"), fine_amount=Decimal("20000"),
                              actor_user_id=admin.id)

        entry = _fine_entry(u1.id)
        assert entry.entry_metadata["fine_id"] == fine.id  # was always None before
        assert entry.entry_metadata["place_balance_at_issue"] == 5.0

    def test_coworkers_return_nets_into_the_number_the_fined_member_is_judged_on(self, db):
        """The case that matters: a per-person slice would read 5 for the fined
        member, but the coworker has already handed 2 empties back at the same
        door, so the PLACE holds 3. The fine ENTITY stays on the chosen member
        and address; only the recorded evaluation context is the place."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        CustomerLinkService().create_place_group([a1.id, a2.id],
                                                 acting_admin_id=admin.id, reason="office")
        svc = BottleTrackingService()
        _move(db, svc, u1, a1, 5)
        _move(db, svc, u2, a2, -2, BottleLedgerEventType.RETURN_ON_DELIVERY)

        fine = svc.issue_fine(user_id=u1.id, address_id=a1.id,
                              quantity=Decimal("1"), fine_amount=Decimal("20000"),
                              actor_user_id=admin.id)

        entry = _fine_entry(u1.id)
        assert entry.entry_metadata["place_balance_at_issue"] == 3.0
        # One pool, one number — 3, never the 5 a per-person slice would show.
        assert float(BottleTrackingService.get_place_balance(a1.id)) == 3.0
        assert float(BottleTrackingService.get_place_balance(a2.id)) == 3.0
        # The fine ENTITY stays per-person: the chosen member, the chosen address,
        # and the scope frozen at issue so a later ungrouping cannot split it.
        assert fine.user_id == u1.id
        assert fine.address_id == a1.id
        assert fine.address_group_id == a1.address_group_id

    def test_ungrouped_place_is_the_address_itself(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u = _user(db, "solo@example.com", "+998900000007")
        a = _address(db, u)
        svc = BottleTrackingService()
        svc.set_initial_balance(u.id, a.id, Decimal("4"), actor_user_id=admin.id)
        fine = svc.issue_fine(user_id=u.id, address_id=a.id,
                              quantity=Decimal("1"), fine_amount=Decimal("20000"),
                              actor_user_id=admin.id)

        entry = _fine_entry(u.id)
        assert entry.entry_metadata["place_balance_at_issue"] == 4.0
        assert fine.address_group_id is None


@pytest.mark.integration
def test_admin_fine_without_address_id_is_400_not_500(client, db, admin_auth_headers):
    u = _user(db, "nofine@example.com", "+998900000006")
    _address(db, u)

    resp = client.post("/api/v1/admin/bottles/fines",
                       json={"userId": u.id, "quantity": 1, "fineAmount": 20000},
                       headers=admin_auth_headers)

    assert resp.status_code == 400, resp.get_json()


@pytest.mark.integration
def test_admin_fine_at_a_place_with_no_ledger_history_records_zero(client, db, admin_auth_headers):
    """There is no `bottle_balance_id` to resolve any more, so the old
    AttributeError-500 is structurally gone: an untouched place is simply a
    zero-balance issuance."""
    u = _user(db, "nohistory@example.com", "+998900000004")
    a = _address(db, u)
    assert BottleTrackingService.get_place_balance_row(a.id) is None

    resp = client.post("/api/v1/admin/bottles/fines",
                       json={"userId": u.id, "addressId": a.id,
                             "quantity": 1, "fineAmount": 20000},
                       headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    entry = _fine_entry(u.id)
    assert entry.entry_metadata["place_balance_at_issue"] == 0.0


@pytest.mark.integration
def test_admin_fine_by_address_with_balance_row_still_succeeds(client, db, admin_auth_headers):
    u = _user(db, "hasbal@example.com", "+998900000005")
    a = _address(db, u)
    admin = _user(db, "adm@example.com", "+998900000009")
    BottleTrackingService().set_initial_balance(u.id, a.id, Decimal("3"), actor_user_id=admin.id)

    resp = client.post("/api/v1/admin/bottles/fines",
                       json={"userId": u.id, "addressId": a.id,
                             "quantity": 1, "fineAmount": 20000},
                       headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    entry = _fine_entry(u.id)
    assert entry.entry_metadata["fine_id"] == resp.get_json()["data"]["id"]
    assert entry.entry_metadata["place_balance_at_issue"] == 3.0
