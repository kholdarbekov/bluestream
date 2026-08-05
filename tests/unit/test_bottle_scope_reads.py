# tests/unit/test_bottle_scope_reads.py
from decimal import Decimal

from business_app.models.bottle import BottleLedger
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType


def _seed(db, place, sample_user, second_sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.RETURN_ON_DELIVERY, quantity=Decimal("-4"))
    db.session.flush()


def test_both_member_addresses_report_the_same_place_balance(
    app, db, place, sample_user, second_sample_user
):
    _seed(db, place, sample_user, second_sample_user)
    svc = BottleTrackingService()
    assert svc.get_place_balance(place["a1"].id) == Decimal("7")
    assert svc.get_place_balance(place["a2"].id) == Decimal("7")


def test_place_ledger_shows_every_members_entries(
    app, db, place, sample_user, second_sample_user
):
    _seed(db, place, sample_user, second_sample_user)
    result = BottleTrackingService.get_place_ledger(place["a2"].id)
    assert result["total"] == 3
    assert {e.user_id for e in result["items"]} == {sample_user.id, second_sample_user.id}


def test_departed_address_does_not_reabsorb_place_history(
    app, db, place, sample_user, second_sample_user
):
    """The `address_group_id IS NULL` arm of the ungrouped predicate (spec 3.1).
    Without it, an address that leaves pulls the whole place ledger with it."""
    _seed(db, place, sample_user, second_sample_user)
    place["a1"].address_group_id = None
    db.session.flush()

    result = BottleTrackingService.get_place_ledger(place["a1"].id)
    assert result["total"] == 0
    assert BottleTrackingService().get_place_balance(place["a1"].id) == Decimal("0")
    # the place keeps everything
    assert BottleTrackingService().get_place_balance(place["a2"].id) == Decimal("7")


def test_cluster_ledger_dedupes_two_addresses_in_one_place(
    app, db, place, sample_user, second_sample_user
):
    """One person owning two addresses at one place must not double-count."""
    place["a2"].user_id = sample_user.id
    db.session.flush()
    _seed(db, place, sample_user, sample_user)

    result = BottleTrackingService.get_cluster_ledger(sample_user.id)
    assert result["total"] == 3      # not 6
    assert len({e.id for e in result["items"]}) == result["total"]


def test_union_helpers_are_gone():
    assert not hasattr(BottleTrackingService, "get_group_union_balance")
    assert not hasattr(BottleTrackingService, "get_address_ledger")
