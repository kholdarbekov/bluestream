# tests/unit/test_bottle_scope_writes.py
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType


# NOTE: `second_sample_user` and `place` are NOT defined here — add both to
# tests/conftest.py exactly as given in the plan's "Shared Test Fixtures"
# section, since Tasks 4-9 and 11 all use them.


def test_two_members_share_one_balance_row(app, db, place, sample_user, second_sample_user):
    """The whole point: coworkers at one place are ONE pool, not 6 and 1."""
    svc = BottleTrackingService()
    svc._create_ledger_entry(
        user_id=second_sample_user.id, address_id=place["a2"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"),
    )
    svc._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"),
    )
    svc._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.RETURN_ON_DELIVERY, quantity=Decimal("-4"),
    )
    db.session.flush()

    rows = BottleBalance.query.filter(BottleBalance.address_group_id == place["group"].id).all()
    assert len(rows) == 1
    assert rows[0].balance == Decimal("7")
    assert rows[0].address_id is None
    # and no per-address rows leaked
    assert BottleBalance.query.filter(BottleBalance.address_id.isnot(None)).count() == 0


def test_balance_after_is_the_places_running_total(app, db, place, sample_user, second_sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    db.session.flush()
    entries = BottleLedger.query.order_by(BottleLedger.id).all()
    assert [e.balance_after for e in entries] == [Decimal("6"), Decimal("11")]


def test_ledger_keeps_attribution(app, db, place, sample_user):
    svc = BottleTrackingService()
    entry = svc._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"),
    )
    db.session.flush()
    assert entry.user_id == sample_user.id      # who
    assert entry.address_id == place["a1"].id     # which door
    assert entry.address_group_id == place["group"].id  # which pool


def test_explicit_scope_overrides_the_resolver(app, db, place, sample_user):
    """Plan C writes both halves of a transfer for one address_id.

    The `resolve_scope_for_write` call is NOT scaffolding: an explicit `scope=`
    means "this specific place", which `get_or_create_balance` cannot self-serve,
    so it asserts the caller already holds the ladder's rung-1 lock on the
    `addresses` row the entry is attributed to. Real callers
    (`_split_bottles_out_of_place`, `release_group_history_to_address`,
    `_apply_merge_review`, `OrderEditService._cascade_bottle`) all hold it by the
    time they get here; a test poking the private helper must too. That
    assertion is the ONLY part of the ladder visible on SQLite, where
    `with_for_update()` compiles to nothing.
    """
    svc = BottleTrackingService()
    svc.resolve_scope_for_write(place["a1"].id)
    svc._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT, quantity=Decimal("3"),
        scope=BottleScope.for_address(place["a1"].id),
        notes="explicit address scope",
    )
    db.session.flush()
    row = BottleBalance.query.filter(
        BottleBalance.address_id == place["a1"].id,
        BottleBalance.address_group_id.is_(None),
    ).one()
    assert row.balance == Decimal("3")


def test_ungrouped_address_behaves_as_before(app, db, user_address, sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=sample_user.id, address_id=user_address.id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"))
    db.session.flush()
    row = BottleBalance.query.one()
    assert row.address_id == user_address.id
    assert row.address_group_id is None
    assert row.balance == Decimal("2")
