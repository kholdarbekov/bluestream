"""The two remaining place-scope consumers: the admin customer map and the
account-merge re-parenting.

The map is ONE aggregate query over N pins, and a grouped place's balance row
carries `address_id IS NULL` — so no single equijoin reaches both the grouped
and the ungrouped shape. The merge is the other half: `bottle_ledger.user_id`
and `bottle_fines.user_id` are still NOT NULL FKs to `users`, so a merge that
does not re-parent them aborts the terminal `db.session.delete(secondary_user)`
on Postgres and dangles in the FK-off SQLite suite.
"""

from decimal import Decimal

import pytest

from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleFineStatus, BottleLedgerEventType


@pytest.mark.unit
def test_both_pins_at_one_place_show_the_place_balance(
    app, db, place, sample_user, second_sample_user, seeded_orders_for_map
):
    """Two coworkers' pins at ONE physical place read the SAME pooled balance.

    The +7 is recorded against a1 only. Before the place re-key each pin showed
    its own (user, address) slice — 7 and 0. Both now show the place's 7.
    """
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("7"),
    )
    db.session.flush()

    from business_app.services.customer_map_service import CustomerMapService

    all_pins = CustomerMapService.get_customer_map_pins()
    # The two conditional outer joins must not FAN OUT: one pin per address,
    # or `address_count` (len of the per-user row list) silently doubles.
    assert [p["address_id"] for p in all_pins].count(place["a1"].id) == 1
    assert len(all_pins) == 2
    assert {p["address_count"] for p in all_pins} == {1}

    pins = {p["address_id"]: p for p in all_pins}
    assert float(pins[place["a1"].id]["bottle_balance"]) == 7.0
    assert float(pins[place["a2"].id]["bottle_balance"]) == 7.0


@pytest.mark.unit
def test_grouped_address_with_stale_solo_row_agrees_with_the_service(
    app, db, place, sample_user, second_sample_user, seeded_orders_for_map
):
    """A GROUPED address that still holds a stale address-keyed balance row must
    not read that row on the map.

    This is the spec §7.2 STRAND state. The write path that used to mint it is
    fixed — `create_place_group` and `add_addresses_to_group` now re-scope an
    existing balance onto the group they join, via
    `BottleTrackingService.absorb_address_into_group` — so the row is
    manufactured directly below. It stays testable because a direct DB edit or a
    restore from a pre-re-scoping dump can still present one, and the map must
    never contradict `get_place_balance` when it does.

    `resolve_scope` decides grouped-vs-ungrouped from the ADDRESS. Gating the
    map's solo join only on the BALANCE ROW's group being NULL lets the two
    disagree: the map served a1=4.0 / a2=0.0 while `get_place_balance` said 0.0
    for both — two pins at one physical place, the exact 6-and-1 split of spec
    §1.1 that this plan exists to remove.

    The stranded 4 is deliberately INVISIBLE here rather than shown on one pin:
    it belongs to no place until an admin re-groups the address (which now runs
    `absorb_address_into_group`) or a merge review folds it in. It is not lost
    silently — the
    nightly sweep reports it under `stranded_address_balances`
    (business_app/tasks/customer_link_tasks.py), pinned by
    tests/unit/test_customer_link_reconciliation.py.
    """
    a1, a2 = place["a1"], place["a2"]
    # The strand: an address-keyed row on an address that is now grouped.
    db.session.add(BottleBalance(address_id=a1.id, balance=Decimal("4.00")))
    db.session.flush()

    from business_app.services.customer_map_service import CustomerMapService

    pins = {p["address_id"]: p for p in CustomerMapService.get_customer_map_pins()}

    # The map is a read model over the SAME scope resolution as the service; it
    # may never contradict it.
    for addr in (a1, a2):
        assert float(pins[addr.id]["bottle_balance"]) == float(
            BottleTrackingService.get_place_balance(addr.id)
        )
    # ...and the two members of one place must agree with each other.
    assert pins[a1.id]["bottle_balance"] == pins[a2.id]["bottle_balance"]
    assert float(pins[a1.id]["bottle_balance"]) == 0.0


@pytest.mark.unit
def test_grouped_place_row_wins_over_a_stale_solo_row(
    app, db, place, sample_user, second_sample_user, seeded_orders_for_map
):
    """Both a group row AND a stale address-keyed row present: the PLACE wins.

    Guards the COALESCE precedence independently of the solo arm's gating — if a
    future edit reorders the two arms, the grouped pin would silently fall back
    to the strand.
    """
    a1, a2 = place["a1"], place["a2"]
    db.session.add(BottleBalance(address_group_id=place["group"].id, balance=Decimal("9.00")))
    db.session.add(BottleBalance(address_id=a1.id, balance=Decimal("4.00")))
    db.session.flush()

    from business_app.services.customer_map_service import CustomerMapService

    pins = {p["address_id"]: p for p in CustomerMapService.get_customer_map_pins()}
    assert float(pins[a1.id]["bottle_balance"]) == 9.0
    assert float(pins[a2.id]["bottle_balance"]) == 9.0


@pytest.mark.unit
def test_ungrouped_pin_still_reads_its_own_address_balance(
    app, db, sample_user, second_sample_user, user_address, seeded_orders_for_map
):
    """The `solo_balance` arm of the COALESCE: an UNGROUPED address keeps its own
    row, and a grouped neighbour's pool must not leak into it.

    Without the `address_group_id IS NULL` clause on the solo join this would
    still pass, so the negative half matters: `second_sample_user` has no
    address at all here and must not acquire a balance from anywhere.
    """
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=user_address.id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"),
    )
    db.session.flush()

    from business_app.services.customer_map_service import CustomerMapService

    pins = {p["address_id"]: p for p in CustomerMapService.get_customer_map_pins()}
    assert float(pins[user_address.id]["bottle_balance"]) == 3.0
    # second_sample_user owns no geocoded address -> contributes no pin at all.
    assert all(p["user_id"] != second_sample_user.id for p in pins.values())


@pytest.mark.unit
def test_pin_with_no_balance_row_reads_zero_not_none(
    app, db, sample_user, second_sample_user, user_address, seeded_orders_for_map
):
    """The COALESCE's final arm: a place that has never moved a bottle reads 0.00."""
    from business_app.services.customer_map_service import CustomerMapService

    pin = next(
        p for p in CustomerMapService.get_customer_map_pins() if p["address_id"] == user_address.id
    )
    assert Decimal(str(pin["bottle_balance"])) == Decimal("0.00")


@pytest.mark.unit
def test_merge_reparents_ledger_attribution(app, db, sample_user, second_sample_user, user_address):
    """Decision 4's named ledger rests on BottleLedger.user_id — if the merge
    leaves it pointing at a deleted account, member_name renders as null."""
    BottleTrackingService()._create_ledger_entry(
        user_id=second_sample_user.id, address_id=user_address.id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"),
    )
    db.session.flush()

    from business_app.services.cross_platform_sync_service import CrossPlatformSyncService

    CrossPlatformSyncService()._transfer_user_references(sample_user.id, second_sample_user.id)
    db.session.flush()

    assert BottleLedger.query.filter_by(user_id=second_sample_user.id).count() == 0
    assert BottleLedger.query.filter_by(user_id=sample_user.id).count() == 1


@pytest.mark.unit
def test_merge_reparents_fine_attribution(app, db, sample_user, second_sample_user, user_address):
    """`bottle_fines.user_id` is the other NOT NULL FK to `users` the merge must
    carry — an unpaid fine that survives on a deleted account is money lost."""
    fine = BottleFine(
        user_id=second_sample_user.id,
        address_id=user_address.id,
        quantity=Decimal("2"),
        fine_amount=Decimal("10000.00"),
        status=BottleFineStatus.PENDING,
        issued_by=sample_user.id,
    )
    db.session.add(fine)
    db.session.flush()

    from business_app.services.cross_platform_sync_service import CrossPlatformSyncService

    CrossPlatformSyncService()._transfer_user_references(sample_user.id, second_sample_user.id)
    db.session.flush()

    assert BottleFine.query.filter_by(user_id=second_sample_user.id).count() == 0
    assert BottleFine.query.filter_by(user_id=sample_user.id).count() == 1
