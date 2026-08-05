from decimal import Decimal

import pytest

from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.exceptions import ValidationError
from shared.enums import BottleLedgerEventType


def test_second_member_cannot_seed_the_same_place(app, db, place, sample_user, second_sample_user):
    """Two coworkers each calling set_initial_balance would double-credit the office."""
    svc = BottleTrackingService()
    svc.set_initial_balance(user_id=sample_user.id, address_id=place["a1"].id,
                            quantity=Decimal("5"), actor_user_id=sample_user.id)
    with pytest.raises(ValidationError) as exc:
        svc.set_initial_balance(user_id=second_sample_user.id, address_id=place["a2"].id,
                                quantity=Decimal("6"), actor_user_id=sample_user.id)
    assert exc.value.error_code == "BOTTLE_INITIAL_BALANCE_EXISTS"


def test_guard_fires_after_any_movement_not_just_initial(app, db, place, sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))
    db.session.flush()
    with pytest.raises(ValidationError):
        svc.set_initial_balance(user_id=sample_user.id, address_id=place["a1"].id,
                                quantity=Decimal("9"), actor_user_id=sample_user.id)


def test_the_initial_balance_entry_carries_NO_idempotency_key(app, db, place, sample_user):
    """The key is GONE, deliberately, and its absence is the fix.

    `uq_bottle_ledger_idempotency` is UNIQUE on the KEY ALONE, so
    `_create_ledger_entry`'s duplicate lookup is `filter_by(idempotency_key=...)`
    with NO scope predicate. A scope-keyed `initial:place:{G}` left behind by a
    dissolved place — or an `initial:addr:{A}` that survived A's join re-stamp —
    therefore matched a call for an entirely different place, and
    `set_initial_balance` silently no-opped and returned 200 echoing ANOTHER
    CUSTOMER'S ledger row.

    Adding a scope predicate to that lookup is NOT the alternative: the UNIQUE
    index would then turn the silent no-op into an IntegrityError 500. The key
    was vestigial — the method's own guard is structural ("this place has no
    history yet"), and it now runs under rung 1 (`addresses` FOR SHARE) and the
    balance row's FOR UPDATE, so concurrency cannot defeat it either.
    """
    svc = BottleTrackingService()
    entry = svc.set_initial_balance(user_id=sample_user.id, address_id=place["a1"].id,
                                    quantity=Decimal("5"), actor_user_id=sample_user.id)
    assert entry.idempotency_key is None


def test_the_structural_guard_replaces_the_key_for_an_ungrouped_address(
    app, db, user_address, sample_user
):
    """Same for an ungrouped place: no key, and the guard still bites."""
    svc = BottleTrackingService()
    entry = svc.set_initial_balance(user_id=sample_user.id, address_id=user_address.id,
                                    quantity=Decimal("2"), actor_user_id=sample_user.id)
    assert entry.idempotency_key is None

    with pytest.raises(ValidationError) as exc:
        svc.set_initial_balance(user_id=sample_user.id, address_id=user_address.id,
                                quantity=Decimal("7"), actor_user_id=sample_user.id)
    assert exc.value.error_code == "BOTTLE_INITIAL_BALANCE_EXISTS"
