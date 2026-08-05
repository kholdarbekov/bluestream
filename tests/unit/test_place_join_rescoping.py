"""Spec §7.2 — joining a place re-scopes the joiner's prior history into it.

Before this, `create_place_group` re-pointed `addresses.address_group_id` and
touched no bottle row: both members' balances stayed on address-keyed rows that
no place-scoped read can reach, and the new group scope read 0. The nightly
`stranded_address_balances` check exists to find exactly that wreckage.

The re-scoping is a MOVE, never a mint: the joiner's own-scope ledger entries
are re-stamped with the group and its own-scope `bottle_balances` row is folded
into the place's single row. Conservation is asserted as a PAIR (the sum of the
place figures before == the sum after), because asserting only the post-state
would pass for a bug that also destroyed the other side.

Everything here drives the real service write paths — `admin_adjust_balance`,
`create_place_group`, `add_addresses_to_group`, `remove_address_from_group` —
and asserts the running `BottleLedger.balance_after` snapshots. No hand-built
`BottleBalance` rows: building the row by hand is exactly the habit that let the
re-key ship green.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import CustomerLinkEvent
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType


# --------------------------------------------------------------------------- #
# Helpers — mirror Task 1's (`tests/unit/test_place_group_ungroup_split.py`).
# --------------------------------------------------------------------------- #

def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    db.session.add(a)
    db.session.commit()
    return a


def _seed(db, address, user, qty):
    """Put `qty` bottles at the address's PLACE through the real write path."""
    BottleTrackingService().admin_adjust_balance(
        user_id=user.id, address_id=address.id, adjustment=Decimal(qty),
        actor_user_id=user.id, notes="seed",
    )
    db.session.commit()


def _two_ungrouped_customers(db):
    """Two DISTINCT (unlinked) customers, one ungrouped address each."""
    u1 = _user(db, "join-a@example.com", "+998900000201")
    u2 = _user(db, "join-b@example.com", "+998900000202")
    admin = _user(db, "join-admin@example.com", "+998900000209")
    svc = CustomerLinkService()
    return svc, admin, u1, _addr(db, u1.id), u2, _addr(db, u2.id)


def _place(address_id):
    return BottleTrackingService.get_place_balance(address_id)


def _quiet_member(db, owner):
    """A third member address that never moves a bottle.

    §7.3 dissolves a place the moment a removal would leave exactly ONE member.
    The remove -> re-add -> remove scenarios below are about §7.1/§7.2 with the
    place still standing, so they need a member that outlives the departure.
    It holds nothing, so no figure in those tests changes.
    """
    return _addr(db, owner.id)


@pytest.mark.unit
def test_grouping_two_funded_addresses_yields_their_sum_at_once(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="same office")

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("7.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")
    # ONE row for the place; both address-keyed rows are gone.
    assert BottleBalance.query.count() == 1
    row = BottleBalance.query.one()
    assert row.address_group_id == group.id and row.address_id is None


@pytest.mark.unit
def test_grouping_conserves_the_two_places_totals(db):
    """The invariant as a PAIR. Assert only the post-state (7) and a bug that
    ALSO minted somewhere else would sail through."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    before = _place(addr_a.id) + _place(addr_b.id)
    ledger_before = sum((e.quantity for e in BottleLedger.query.all()), Decimal("0.00"))
    balances_before = sum((b.balance for b in BottleBalance.query.all()), Decimal("0.00"))

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    # The place is ONE now, so "the sum of the distinct places" is read once.
    after = _place(addr_a.id)
    assert before == after
    assert sum((e.quantity for e in BottleLedger.query.all()), Decimal("0.00")) == ledger_before
    assert sum((b.balance for b in BottleBalance.query.all()), Decimal("0.00")) == balances_before


@pytest.mark.unit
def test_history_is_restamped_and_no_immutable_fact_is_touched(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    before = {
        e.id: (e.event_type, e.quantity, e.user_id, e.address_id,
               e.occurred_at, e.order_id, e.delivery_id, e.idempotency_key)
        for e in BottleLedger.query.all()
    }
    assert before

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="same office")

    for entry in BottleLedger.query.all():
        assert entry.address_group_id == group.id          # the ONE key rewritten
        assert (entry.event_type, entry.quantity, entry.user_id, entry.address_id,
                entry.occurred_at, entry.order_id, entry.delivery_id,
                entry.idempotency_key) == before[entry.id]


@pytest.mark.unit
def test_a_rejoin_cannot_capture_the_former_groups_rows(db):
    """The selector is `address_id = a AND address_group_id IS NULL`. A bare
    `address_id = a` would drag the OLD place's history into the new one."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "5")
    g1 = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office 1")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    # addr_a's OLD entries are still stamped g1 and stay there (spec §7.1).
    old_ids = {e.id for e in BottleLedger.query.filter_by(address_group_id=g1.id).all()}
    assert old_ids

    _seed(db, addr_a, u1, "2")            # a fresh, address-scoped entry
    addr_c = _addr(db, u2.id)
    g2 = svc.create_place_group([addr_a.id, addr_c.id], acting_admin_id=admin.id, reason="office 2")

    for entry_id in old_ids:
        assert db.session.get(BottleLedger, entry_id).address_group_id == g1.id
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("2.00")
    # g1 kept its 5 — the rejoin neither drained nor duplicated the old place.
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")
    assert g2.id != g1.id


@pytest.mark.unit
def test_balance_after_is_recomputed_and_stable_across_reruns(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    db.session.refresh(addr_a)
    scope = BottleScope.for_group(addr_a.address_group_id)
    ordered = (BottleLedger.query.filter(*scope.ledger_filter())
               .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all())
    running = Decimal("0.00")
    for entry in ordered:
        running += entry.quantity
        assert entry.balance_after == running
    assert running == BottleTrackingService.get_place_balance(addr_a.id)

    # Rerunning it must be a no-op — `occurred_at` alone would not be stable,
    # because paired entries written in one transaction share a timestamp.
    snapshot = [(e.id, e.balance_after) for e in ordered]
    BottleTrackingService.recompute_balance_after(scope)
    db.session.flush()
    assert [(e.id, e.balance_after) for e in
            BottleLedger.query.filter(*scope.ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all()] == snapshot


@pytest.mark.unit
def test_add_addresses_to_group_absorbs_a_funded_joiner(db):
    """The same contract via the OTHER entry point."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    addr_c = _addr(db, u1.id)
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    _seed(db, addr_c, u1, "6")
    _seed(db, addr_a, u1, "1")
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("1.00")

    svc.add_addresses_to_group(group.id, [addr_c.id], acting_admin_id=admin.id, reason="new hire")

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("7.00")
    assert BottleTrackingService.get_place_balance(addr_c.id) == Decimal("7.00")


@pytest.mark.unit
def test_the_join_event_records_which_entries_moved(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    moved_before = {e.id for e in BottleLedger.query.all()}

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert set(event.event_metadata["rescoped_ledger_entry_ids"]) == moved_before


@pytest.mark.unit
def test_the_nightly_stranded_check_finds_nothing_after_a_join(db):
    """The check that exists because this task did not: it must go quiet now."""
    from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants

    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    assert reconcile_customer_link_invariants()["stranded_address_balances"] == []


# --------------------------------------------------------------------------- #
# The bug Task 2's review routed here: split -> re-add stranded the departed
# bottles. Task 2's `:in` half is the FIRST thing that ever gives a departing
# address a balance row of its own, which is what made this gap reachable.
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_readding_a_split_out_address_brings_its_bottles_back_to_the_place(db):
    """place 7 -> remove(bottles_leaving=3) -> place 4 + addr 3 -> re-add -> 7.

    Before the join-time absorb, the re-add left the 3 on addr_a's own
    address-keyed row while every place-scoped read resolved addr_a to the
    group: the place read 4 from BOTH addresses and the 3 were invisible
    operationally — in the admin panel and in the driver return prompt — for as
    long as addr_a stayed re-grouped.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    group = svc.create_place_group([addr_a.id, addr_b.id, _quiet_member(db, u2).id],
                                   acting_admin_id=admin.id, reason="office")
    _seed(db, addr_a, u1, "7")
    assert _place(addr_b.id) == Decimal("7.00")

    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                  reason="took a crate", bottles_leaving=Decimal("3"))
    # Task 2's conserving split: 4 stay at the place, 3 leave with the address.
    assert _place(addr_b.id) == Decimal("4.00")
    assert _place(addr_a.id) == Decimal("3.00")
    total_before = _place(addr_a.id) + _place(addr_b.id)
    assert total_before == Decimal("7.00")

    svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id, reason="came back")

    # The pair: nothing minted, nothing stranded. One place, holding all seven.
    assert _place(addr_a.id) == Decimal("7.00")
    assert _place(addr_b.id) == Decimal("7.00")
    assert _place(addr_a.id) == total_before
    # The 3 came back as HISTORY, not as a new adjustment: the `:in` half is now
    # stamped with the group, and no address-keyed row survives.
    inn = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("place_leave:%:in")).one()
    assert inn.address_group_id == group.id
    assert BottleBalance.query.filter(BottleBalance.address_id.isnot(None)).count() == 0
    assert BottleBalance.query.count() == 1


@pytest.mark.unit
def test_the_readded_places_running_snapshots_walk_the_merged_timeline(db):
    """`balance_after` after the re-add: 7 -> 4 -> 7, and the last snapshot is
    the place figure. A re-scope that moved rows but left stale snapshots would
    read correctly in the summary and wrongly in the history view."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    group = svc.create_place_group([addr_a.id, addr_b.id, _quiet_member(db, u2).id],
                                   acting_admin_id=admin.id, reason="office")
    _seed(db, addr_a, u1, "7")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                  reason="took a crate", bottles_leaving=Decimal("3"))
    svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id, reason="came back")

    scope = BottleScope.for_group(group.id)
    ordered = (BottleLedger.query.filter(*scope.ledger_filter())
               .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all())
    assert [(e.quantity, e.balance_after) for e in ordered] == [
        (Decimal("7.00"), Decimal("7.00")),
        (Decimal("-3.00"), Decimal("4.00")),
        (Decimal("3.00"), Decimal("7.00")),
    ]
    assert ordered[-1].balance_after == _place(addr_b.id)


@pytest.mark.unit
def test_a_second_removal_after_the_readd_still_sees_the_whole_place(db):
    """The empirical repro's last step: removing again used to make the 3
    reappear, proving they had merely been hidden. Now the place genuinely holds
    7, so the admin may take all 7 out — which the pre-fix code rejected as
    above the (wrongly-read) 4."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    group = svc.create_place_group([addr_a.id, addr_b.id, _quiet_member(db, u2).id],
                                   acting_admin_id=admin.id, reason="office")
    _seed(db, addr_a, u1, "7")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                  reason="took a crate", bottles_leaving=Decimal("3"))
    svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id, reason="came back")

    result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                           reason="left for good", bottles_leaving=Decimal("7"))

    assert result["bottles_leaving"] == Decimal("7.00")
    assert _place(addr_a.id) == Decimal("7.00")
    assert _place(addr_b.id) == Decimal("0.00")
