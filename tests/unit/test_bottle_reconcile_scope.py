from decimal import Decimal

from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from shared.enums import BottleLedgerEventType


def test_reconcile_uses_the_place_ledger(app, db, place, sample_user, second_sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("1"))
    db.session.flush()

    row = BottleBalance.query.filter_by(address_group_id=place["group"].id).one()
    row.balance = Decimal("999")   # corrupt it
    db.session.flush()

    result = svc.reconcile_balance(place["a1"].id)
    assert result["recalculated_balance"] == 7.0
    assert result["corrected"] is True
    assert result["address_group_id"] == place["group"].id


def test_sweep_reports_negative_places(app, db, place, sample_user):
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT, quantity=Decimal("-3"), notes="x",
    )
    db.session.flush()
    report = reconcile_customer_link_invariants()
    assert report["negative_place_balances"]
    assert "stranded_negative_pairs" not in report


def test_sweep_reports_orphaned_place_balances(app, db, place, sample_user):
    """A group balance whose members have all left is unreachable by every
    address-keyed read, so the sweep must be driven from bottle_balances."""
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"),
    )
    db.session.flush()
    place["a1"].address_group_id = None
    place["a2"].address_group_id = None
    db.session.flush()

    report = reconcile_customer_link_invariants()
    assert place["group"].id in report["orphaned_place_balances"]


def test_sweep_reports_stranded_address_balances(app, db, place, sample_user):
    """The INVERSE of the orphan case: an address-KEYED row whose address has
    since joined a place group (spec §7.2).

    Every place-scoped read resolves that address to the group, so the row is
    unreachable and its bottles are invisible without being deleted. Group join
    now re-scopes the balance (`absorb_address_into_group`), so the check is a
    BACKSTOP against a direct DB edit, an old restore, or a future write path
    that bypasses it — the row below is manufactured directly for that reason.
    Neither the negative nor the orphan check can see it, which is why it needs
    a key of its own.
    """
    stranded = BottleBalance(address_id=place["a1"].id, balance=Decimal("4"))
    db.session.add(stranded)
    db.session.flush()

    # It really is unreachable: the place-scoped read resolves past it.
    assert BottleTrackingService.get_place_balance(place["a1"].id) == Decimal("0")

    report = reconcile_customer_link_invariants()
    assert stranded.id in report["stranded_address_balances"]
    # Not confused with either neighbouring check.
    assert report["negative_place_balances"] == []
    assert report["orphaned_place_balances"] == []


def test_sweep_does_not_flag_an_ungrouped_addresss_own_balance(app, db, user_address, sample_user):
    """False-positive control, and the one that matters most: an address-keyed
    row on an UNGROUPED address is the ordinary, correct shape for the majority
    of production rows. It is that address's own place and must never be
    flagged."""
    db.session.add(BottleBalance(address_id=user_address.id, balance=Decimal("5")))
    db.session.flush()

    assert BottleTrackingService.get_place_balance(user_address.id) == Decimal("5")
    assert reconcile_customer_link_invariants()["stranded_address_balances"] == []
