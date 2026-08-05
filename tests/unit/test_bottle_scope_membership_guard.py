"""Spec 4.3: `issue_fine`'s dropped `balance.user_id == user_id` assertion is
REPLACED, not deleted — it becomes "the fined user must resolve to the same
scope as the address" (owner of the address, or owner of a member address of
its place group). The same substitution applies to `record_standalone_collection`,
which previously took `customer_id`/`address_id` straight from request JSON with
no ownership check at all.

Error code: BOTTLE_SCOPE_MEMBERSHIP_REQUIRED (spec section 13).
"""
from decimal import Decimal

import pytest

from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.exceptions import ValidationError


def test_coworker_at_same_place_can_be_fined_at_others_address(app, db, place, second_sample_user):
    """The old per-pair check wrongly forbade this; the place-scoped check must allow it:
    second_sample_user owns a2, but a1 and a2 are the SAME place, so fining
    second_sample_user against a1 is legitimate."""
    fine = BottleTrackingService().issue_fine(
        user_id=second_sample_user.id, address_id=place["a1"].id,
        quantity=Decimal("1"), fine_amount=Decimal("10000"),
        actor_user_id=second_sample_user.id,
    )
    db.session.flush()
    assert fine.user_id == second_sample_user.id
    assert fine.address_id == place["a1"].id


def test_stranger_is_rejected_with_scope_membership_required(app, db, place, sample_user):
    """A user who owns no address in this place must not be fine-able there."""
    from business_app.models.user import User
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserType

    stranger = User(
        email="stranger@example.com",
        phone="+998901234599",
        password_hash=hash_password("TestPassword123!"),
        first_name="Random",
        last_name="Stranger",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(stranger)
    db.session.commit()

    with pytest.raises(ValidationError) as exc_info:
        BottleTrackingService().issue_fine(
            user_id=stranger.id, address_id=place["a1"].id,
            quantity=Decimal("1"), fine_amount=Decimal("10000"),
            actor_user_id=sample_user.id,
        )
    assert exc_info.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"


def test_ungrouped_address_only_owner_allowed(app, db, user_address, sample_user, second_sample_user):
    """On an ungrouped address there is no place to share — only the address's
    own owner may be fined there."""
    # The owner: allowed.
    fine = BottleTrackingService().issue_fine(
        user_id=sample_user.id, address_id=user_address.id,
        quantity=Decimal("1"), fine_amount=Decimal("10000"), actor_user_id=sample_user.id,
    )
    db.session.flush()
    assert fine.user_id == sample_user.id

    # A different user with no address at this (ungrouped) place: rejected.
    with pytest.raises(ValidationError) as exc_info:
        BottleTrackingService().issue_fine(
            user_id=second_sample_user.id, address_id=user_address.id,
            quantity=Decimal("1"), fine_amount=Decimal("10000"), actor_user_id=sample_user.id,
        )
    assert exc_info.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"


def test_standalone_collection_rejects_mismatched_user(app, db, user_address, second_sample_user):
    """record_standalone_collection must apply the same scope-membership guard —
    it previously took customer_id/address_id from request JSON unchecked."""
    with pytest.raises(ValidationError) as exc_info:
        BottleTrackingService().record_standalone_collection(
            user_id=second_sample_user.id, address_id=user_address.id,
            quantity=Decimal("1"), actor_user_id=second_sample_user.id,
        )
    assert exc_info.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"


def test_standalone_collection_allows_coworker_at_same_place(app, db, place, second_sample_user):
    """Mirrors the fine-side coworker case: collection against a1 attributed to
    the coworker who owns a2 (same place) must be allowed."""
    entry = BottleTrackingService().record_standalone_collection(
        user_id=second_sample_user.id, address_id=place["a1"].id,
        quantity=Decimal("1"), actor_user_id=second_sample_user.id,
    )
    db.session.flush()
    assert entry is not None
