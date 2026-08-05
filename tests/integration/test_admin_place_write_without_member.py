"""Plan C Task 7: an admin bottle write names a PLACE, never a member.

The owner's model is that one person places the order and there is no coworker
selection or separation anywhere afterwards. For bottles the admin therefore
adjusts the *place*; the three admin write routes must accept a request with no
``user_id`` at all.

Why this is safe to derive rather than ask for: ``bottle_balances`` has no
``user_id`` column (``business_app/models/bottle.py``), so the balance axis is
already place-only. What still carries a user is the AUDIT row —
``bottle_ledger.user_id`` and ``bottle_fines.user_id`` are both NOT NULL — and
that is a record of which member's address the write was booked through, not a
slice of the pool. Deriving it cannot move a balance.

The derivation must be DETERMINISTIC: the representative address of the place
(lowest member address id — the same rule ``serialize_bottle_balance`` publishes
as ``representative_address_id``), and its owner. Two identical calls that
attribute to two different coworkers would be a defect, so every case below
sends the NON-representative member's address and still expects the
representative's owner.

An explicit ``user_id`` remains accepted, and remains guarded by
``_assert_user_in_scope`` — stripped of a required field, the routes must not
also lose their stranger check.
"""

from decimal import Decimal

import pytest

from business_app.models.bottle import BottleFine, BottleLedger
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.exceptions import ValidationError


pytestmark = pytest.mark.integration


def _representative_owner_id(db, group_id):
    """Independently recompute "lowest member address id, then its owner"."""
    from business_app.models.user import UserAddress

    rows = (
        db.session.query(UserAddress.id, UserAddress.user_id)
        .filter(UserAddress.address_group_id == group_id)
        .order_by(UserAddress.id.asc())
        .all()
    )
    return rows[0][1]


# ---------------------------------------------------------------------------
# The derivation itself
# ---------------------------------------------------------------------------


def test_derived_user_is_the_representative_members_owner(app, db, place, sample_user):
    """a1 (sample_user) has the lower id, so BOTH member addresses resolve to it."""
    svc = BottleTrackingService()

    from_a1 = svc.resolve_place_attribution_user_id(place["a1"].id)
    from_a2 = svc.resolve_place_attribution_user_id(place["a2"].id)

    assert from_a1 == from_a2 == _representative_owner_id(db, place["group"].id)
    assert from_a1 == sample_user.id


def test_derivation_is_stable_across_repeated_calls(app, db, place):
    """A derivation that can answer differently on two identical calls is a defect."""
    svc = BottleTrackingService()
    answers = {svc.resolve_place_attribution_user_id(place["a2"].id) for _ in range(5)}
    assert len(answers) == 1


def test_derivation_agrees_with_the_serializers_representative_address(app, db, place, admin_user):
    """One rule, not two: the id the admin UI sends and the user the service
    derives must come from the same "lowest member address" ordering."""
    from business_app.models.user import UserAddress
    from business_app.serializers.bottle_serializers import serialize_bottle_balance

    BottleTrackingService().admin_adjust_balance(
        user_id=None, address_id=place["a2"].id, adjustment=Decimal("1"),
        actor_user_id=admin_user.id, notes="seed",
    )
    db.session.commit()

    row = BottleTrackingService.get_place_balance_row(place["a1"].id)
    representative_id = serialize_bottle_balance(row)["representative_address_id"]
    representative_owner = db.session.query(UserAddress.user_id).filter(
        UserAddress.id == representative_id
    ).scalar()

    assert BottleTrackingService().resolve_place_attribution_user_id(
        place["a2"].id
    ) == representative_owner


def test_solo_place_derives_its_own_owner(app, db, user_address, sample_user):
    assert BottleTrackingService().resolve_place_attribution_user_id(
        user_address.id
    ) == sample_user.id


# ---------------------------------------------------------------------------
# Modal 1: Adjust
# ---------------------------------------------------------------------------


def test_adjust_route_accepts_no_member_at_a_shared_place(client, db, place, admin_auth_headers, sample_user):
    resp = client.post(
        "/api/v1/admin/bottles/adjustment",
        json={"addressId": place["a2"].id, "adjustment": 3, "notes": "recount"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    entry = BottleLedger.query.get(resp.get_json()["data"]["id"])
    # The write landed on the PLACE...
    assert entry.address_group_id == place["group"].id
    assert float(BottleTrackingService.get_place_balance(place["a1"].id)) == 3.0
    # ...and the audit row carries the representative member, not the sent one.
    assert entry.user_id == sample_user.id


def test_adjust_route_accepts_no_member_at_a_solo_place(client, db, user_address, admin_auth_headers, sample_user):
    resp = client.post(
        "/api/v1/admin/bottles/adjustment",
        json={"addressId": user_address.id, "adjustment": 2, "notes": "recount"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    entry = BottleLedger.query.get(resp.get_json()["data"]["id"])
    assert entry.address_group_id is None
    assert entry.user_id == sample_user.id
    assert float(BottleTrackingService.get_place_balance(user_address.id)) == 2.0


# ---------------------------------------------------------------------------
# Modal 2: Initial balance
# ---------------------------------------------------------------------------


def test_initial_balance_route_accepts_no_member_at_a_shared_place(
    client, db, place, admin_auth_headers, sample_user
):
    resp = client.post(
        "/api/v1/admin/bottles/initial-balance",
        json={"addressId": place["a2"].id, "quantity": 6},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    entry = BottleLedger.query.get(resp.get_json()["data"]["id"])
    assert entry.address_group_id == place["group"].id
    assert entry.user_id == sample_user.id
    assert float(BottleTrackingService.get_place_balance(place["a1"].id)) == 6.0


def test_initial_balance_route_accepts_no_member_at_a_solo_place(
    client, db, user_address, admin_auth_headers, sample_user
):
    resp = client.post(
        "/api/v1/admin/bottles/initial-balance",
        json={"addressId": user_address.id, "quantity": 4},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    entry = BottleLedger.query.get(resp.get_json()["data"]["id"])
    assert entry.address_group_id is None
    assert entry.user_id == sample_user.id
    assert float(BottleTrackingService.get_place_balance(user_address.id)) == 4.0


# ---------------------------------------------------------------------------
# Modal 3: Fine
# ---------------------------------------------------------------------------


def test_fine_route_accepts_no_member_at_a_shared_place(client, db, place, admin_auth_headers, sample_user):
    resp = client.post(
        "/api/v1/admin/bottles/fines",
        json={"addressId": place["a2"].id, "quantity": 1, "fineAmount": 20000},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    fine = BottleFine.query.get(resp.get_json()["data"]["id"])
    # Frozen at the PLACE, attributed to the representative member.
    assert fine.address_group_id == place["group"].id
    assert fine.user_id == sample_user.id


def test_fine_route_accepts_no_member_at_a_solo_place(client, db, user_address, admin_auth_headers, sample_user):
    resp = client.post(
        "/api/v1/admin/bottles/fines",
        json={"addressId": user_address.id, "quantity": 1, "fineAmount": 15000},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    fine = BottleFine.query.get(resp.get_json()["data"]["id"])
    assert fine.address_group_id is None
    assert fine.user_id == sample_user.id


# ---------------------------------------------------------------------------
# What must NOT change
# ---------------------------------------------------------------------------


def test_explicit_member_is_still_honoured(client, db, place, admin_auth_headers, second_sample_user):
    """Optional, not forbidden: a caller that still names a member gets that member."""
    resp = client.post(
        "/api/v1/admin/bottles/adjustment",
        json={"userId": second_sample_user.id, "addressId": place["a2"].id,
              "adjustment": 1, "notes": "explicit"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    entry = BottleLedger.query.get(resp.get_json()["data"]["id"])
    assert entry.user_id == second_sample_user.id


def test_explicit_stranger_is_still_rejected(app, db, place, admin_user, second_sample_user):
    """`_assert_user_in_scope` must survive: dropping a required field must not
    also drop the guard that stops a write being booked to a stranger."""
    from business_app.models.user import User
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserType

    stranger = User(
        email="task7-stranger@example.com", phone="+998901234591",
        password_hash=hash_password("TestPassword123!"), first_name="Random",
        last_name="Stranger", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(stranger)
    db.session.commit()

    with pytest.raises(ValidationError) as exc:
        BottleTrackingService().issue_fine(
            user_id=stranger.id, address_id=place["a1"].id, quantity=Decimal("1"),
            fine_amount=Decimal("10000"), actor_user_id=admin_user.id,
        )
    assert exc.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
