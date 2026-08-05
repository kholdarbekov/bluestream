"""Spec §7.3 — a place group cannot be a one-member group, and a grouped
address cannot be deleted.

`create_place_group` requires >= 2 addresses, so a removal that would leave
exactly one member dissolves the place in the SAME transaction: the survivor
inherits the whole remaining balance plus its own history, and the group's
`bottle_balances` row — which no address could reach any more — is deleted.

The `AddressGroup` row itself is deliberately KEPT. `bottle_ledger.address_group_id`
is a foreign key, and the departed members' entries stay stamped with it; deleting
the group would either orphan that FK or force those entries to `NULL`, which
under §3.1's predicate would drop the PLACE's history into a DEPARTED address's
own scope and mint bottles onto it. A memberless group row is inert — nothing
resolves to it — so it is retained as the anchor for that history.

CONSERVATION is the invariant this file exists to protect: the dissolve MOVES
bottles, it never mints or destroys them. Every conservation assertion here is a
PAIR (total before == total after), because asserting only the post-state would
pass for a bug that also wrecked the other side.

Everything drives the real service write paths — `admin_adjust_balance`,
`create_place_group`, `remove_address_from_group`, and the three HTTP/service
delete paths — and asserts the running `BottleLedger.balance_after` snapshots.
No hand-built `BottleBalance` rows.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.user import User, UserAddress
from business_app.services.auth_service import AuthService
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, UserRole, UserStatus, UserType


# --------------------------------------------------------------------------- #
# Helpers — mirror Task 1/2/3's (`tests/unit/test_place_group_ungroup_split.py`,
# `tests/unit/test_place_join_rescoping.py`).
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


def _force_one_member_place(db, address_id, group_id):
    """Point ONE address at a group WITHOUT the service, carrying its bottles.

    No service path builds a one-member place any more: `create_place_group`
    requires >= 2, a removal that would leave one member DISSOLVES in the same
    transaction, and a memberless group is refused as a join target
    (`PLACE_GROUP_DISSOLVED`). The shape survives in data written before that
    refusal, and `_dissolve_if_last_member`'s ZERO-REMAINING arm exists for it —
    which is why `release_group_history_to_address` passes
    `allow_memberless=True`. Building it by hand keeps that arm covered.
    """
    db.session.query(UserAddress).filter(UserAddress.id == address_id).update(
        {UserAddress.address_group_id: group_id}, synchronize_session=False
    )
    db.session.query(BottleLedger).filter(
        BottleLedger.address_id == address_id, BottleLedger.address_group_id.is_(None)
    ).update({BottleLedger.address_group_id: group_id}, synchronize_session=False)
    own = BottleBalance.query.filter_by(
        address_id=address_id, address_group_id=None
    ).one_or_none()
    carried = Decimal(str(own.balance or 0)) if own is not None else Decimal("0.00")
    if own is not None:
        db.session.delete(own)
    db.session.flush()
    db.session.add(BottleBalance(address_group_id=group_id, balance=carried))
    db.session.commit()
    db.session.expire_all()


def _seed(db, address, user, qty):
    """Put `qty` bottles at the address's PLACE through the real write path."""
    BottleTrackingService().admin_adjust_balance(
        user_id=user.id, address_id=address.id, adjustment=Decimal(qty),
        actor_user_id=user.id, notes="seed",
    )
    db.session.commit()


def _place(address_id):
    return BottleTrackingService.get_place_balance(address_id)


def _all_bottles():
    """Every bottle the system materialises, across every scope.

    The conservation PAIR is asserted against this, not against one place: a
    dissolve that moved the survivor's figure correctly while quietly zeroing
    the group row would satisfy a one-sided assertion and destroy bottles.
    """
    return sum((b.balance for b in BottleBalance.query.all()), Decimal("0.00"))


def _two_ungrouped_customers(db):
    """Two DISTINCT (unlinked) customers, one ungrouped address each."""
    u1 = _user(db, "diss-a@example.com", "+998900000401")
    u2 = _user(db, "diss-b@example.com", "+998900000402")
    admin = _user(db, "diss-admin@example.com", "+998900000409")
    svc = CustomerLinkService()
    return svc, admin, u1, _addr(db, u1.id), u2, _addr(db, u2.id)


def _three_member_place(db):
    """A three-member place, so ONE removal does not dissolve it."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    u3 = _user(db, "diss-c@example.com", "+998900000403")
    addr_c = _addr(db, u3.id)
    svc.create_place_group([addr_a.id, addr_b.id, addr_c.id],
                           acting_admin_id=admin.id, reason="office")
    return svc, admin, u1, addr_a, u2, addr_b, addr_c, u3


# --------------------------------------------------------------------------- #
# §7.3 — dissolve on the last member
# --------------------------------------------------------------------------- #

@pytest.mark.integration
def test_removing_the_second_to_last_member_dissolves_the_place(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    assert _place(addr_b.id) == Decimal("7.00")

    result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    assert result["dissolved"] is True
    db.session.refresh(addr_b)
    assert addr_b.address_group_id is None
    # The survivor inherits the WHOLE remaining balance, on its OWN row.
    assert _place(addr_b.id) == Decimal("7.00")
    row = BottleTrackingService.get_place_balance_row(addr_b.id)
    assert row.address_id == addr_b.id and row.address_group_id is None
    # And the unreachable group row is gone (spec §7.3's orphan class).
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
    assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []


@pytest.mark.integration
def test_a_three_member_place_is_not_dissolved_by_one_removal(db):
    """The fence is "would be left with exactly one member", not "any removal"."""
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "4")

    result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    assert result["dissolved"] is False
    db.session.refresh(addr_b)
    db.session.refresh(addr_c)
    assert addr_b.address_group_id is not None
    assert addr_b.address_group_id == addr_c.address_group_id
    assert _place(addr_b.id) == Decimal("4.00")
    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("place_dissolve:%")).count() == 0


@pytest.mark.integration
def test_the_survivor_inherits_a_departed_members_bottles_without_stealing_their_ledger(db):
    """The sharp case. addr_a left earlier taking nothing, so its entries stay
    stamped with the group. Dissolving to addr_b must NOT re-home those entries
    into addr_a's own scope — that would mint bottles onto a departed address."""
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "4")       # the member who will leave first
    _seed(db, addr_b, u2, "3")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    assert _place(addr_a.id) == Decimal("0.00")

    svc.remove_address_from_group(addr_c.id, acting_admin_id=admin.id, reason="left too")

    # addr_b, the survivor, holds everything — on its OWN, ungrouped scope.
    db.session.refresh(addr_b)
    assert addr_b.address_group_id is None
    assert BottleTrackingService.get_place_balance_row(addr_b.id).address_id == addr_b.id
    assert _place(addr_b.id) == Decimal("7.00")
    # addr_a is STILL zero — its old entries did not follow it out.
    assert _place(addr_a.id) == Decimal("0.00")
    BottleTrackingService().reconcile_balance(addr_a.id)
    assert _place(addr_a.id) == Decimal("0.00")


@pytest.mark.integration
def test_dissolve_conserves_the_total_and_pairs_its_adjustment(db):
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    before = _place(addr_b.id)
    assert before == Decimal("7.00")
    total_before = _all_bottles()

    svc.remove_address_from_group(addr_c.id, acting_admin_id=admin.id, reason="left too")

    # The PAIR: the survivor's figure is unchanged AND nothing was minted or
    # destroyed anywhere else.
    assert _place(addr_b.id) == before
    assert _all_bottles() == total_before
    pair = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("place_dissolve:%")).all()
    assert len(pair) == 2
    assert sum((e.quantity for e in pair), Decimal("0.00")) == Decimal("0.00")


@pytest.mark.integration
def test_the_dissolve_pair_is_attributed_scoped_and_keyed(db):
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    group_id = addr_b.address_group_id
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    svc.remove_address_from_group(addr_c.id, acting_admin_id=admin.id, reason="last one out")

    event = (CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group")
             .order_by(CustomerLinkEvent.id.desc()).first())
    out = BottleLedger.query.filter_by(
        idempotency_key=f"place_dissolve:{group_id}:{event.id}:out").one()
    inn = BottleLedger.query.filter_by(
        idempotency_key=f"place_dissolve:{group_id}:{event.id}:in").one()
    assert out.quantity == Decimal("-4.00")     # addr_a's departed 4
    assert inn.quantity == Decimal("4.00")
    assert out.quantity + inn.quantity == Decimal("0.00")
    assert out.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
    assert inn.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
    # Attribution: both halves name the SURVIVOR and its owner, not the leaver.
    assert out.address_id == addr_b.id and inn.address_id == addr_b.id
    assert out.user_id == u2.id and inn.user_id == u2.id
    assert out.actor_user_id == admin.id and inn.actor_user_id == admin.id
    # SCOPE: the out half stays in the dissolved place, the in half lands on the
    # survivor's own scope.
    assert out.address_group_id == group_id
    assert inn.address_group_id is None
    assert out.entry_metadata["source"] == "place_dissolve"
    assert out.entry_metadata["place_group_id"] == group_id
    assert out.entry_metadata["reason"] == "last one out"
    assert out.entry_metadata["acting_admin_id"] == admin.id


@pytest.mark.integration
def test_the_survivors_running_snapshots_walk_its_new_timeline(db):
    """`balance_after` on the survivor's own scope: 3 -> 7. A dissolve that moved
    the rows but left stale snapshots would read right in the summary and wrong
    in the customer's history view."""
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    svc.remove_address_from_group(addr_c.id, acting_admin_id=admin.id, reason="left too")

    scope = BottleScope.for_address(addr_b.id)
    ordered = (BottleLedger.query.filter(*scope.ledger_filter())
               .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all())
    assert [(e.quantity, e.balance_after) for e in ordered] == [
        (Decimal("3.00"), Decimal("3.00")),
        (Decimal("4.00"), Decimal("7.00")),
    ]
    assert ordered[-1].balance_after == _place(addr_b.id)


@pytest.mark.integration
def test_a_clean_two_member_dissolve_writes_no_adjustment(db):
    """No departed members => the survivor's own entries ARE the place, and the
    gap is zero, so the common case posts nothing."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_b, u2, "3")
    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("place_dissolve:%")).count() == 0
    # It still dissolved: the survivor's own re-stamped history IS the place.
    db.session.refresh(addr_b)
    assert addr_b.address_group_id is None
    row = BottleTrackingService.get_place_balance_row(addr_b.id)
    assert row.address_id == addr_b.id and row.address_group_id is None
    assert _place(addr_b.id) == Decimal("3.00")
    assert _place(addr_a.id) == Decimal("0.00")


@pytest.mark.integration
def test_dissolve_conserves_a_place_figure_its_ledger_cannot_explain(db):
    """The drift case, and the reason the dissolve does NOT rebuild either scope
    from its ledger.

    `absorb_address_into_group` documents this class explicitly: a place whose
    figure was seeded before the ledger existed has a balance row and no
    entries. Rebuilding the group scope from ledger sums (and reading the gap
    off the rebuilt figure) would silently DESTROY every such bottle. The
    dissolve therefore moves the figure the group's row actually holds.
    """
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "7")
    group_id = addr_b.address_group_id
    # Model the pre-ledger seeded figure: keep the row, drop the entries that
    # explain it. Nothing hand-builds a BottleBalance — the row below is the one
    # `create_place_group` + `admin_adjust_balance` really wrote.
    BottleLedger.query.filter_by(address_group_id=group_id).delete()
    db.session.commit()
    assert _place(addr_b.id) == Decimal("7.00")
    total_before = _all_bottles()
    assert total_before == Decimal("7.00")

    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    svc.remove_address_from_group(addr_c.id, acting_admin_id=admin.id, reason="left too")

    # The PAIR. The seven survive the dissolve and land on the last member's
    # OWN row — a ledger-derived rebuild would have zeroed both scopes here.
    assert _all_bottles() == total_before
    db.session.refresh(addr_b)
    assert addr_b.address_group_id is None
    row = BottleTrackingService.get_place_balance_row(addr_b.id)
    assert row.address_id == addr_b.id and row.balance == Decimal("7.00")
    assert _place(addr_b.id) == Decimal("7.00")
    assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0


@pytest.mark.integration
def test_bottles_can_leave_with_the_departing_member_and_still_dissolve(db):
    """§7.1's split and §7.3's dissolve in ONE call: the split writes while the
    place still has two members, then the dissolve moves what is left."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    total_before = _all_bottles()
    assert total_before == Decimal("7.00")

    result = svc.remove_address_from_group(
        addr_a.id, acting_admin_id=admin.id, reason="took two crates", bottles_leaving=2)

    assert result["bottles_leaving"] == Decimal("2.00")
    assert result["dissolved"] is True
    assert _place(addr_a.id) == Decimal("2.00")
    assert _place(addr_b.id) == Decimal("5.00")
    assert _all_bottles() == total_before          # the pair


@pytest.mark.integration
def test_emptying_a_repopulated_one_member_group_still_dissolves(db, client, admin_auth_headers):
    """The ZERO-remaining arm of §7.3, and the HTTP door into it, now closed.

    UPDATED, in two halves that must be read together.

    (1) The door is SHUT. A dissolved group keeps its (memberless)
    `AddressGroup` row, and `add_addresses_to_group` used to accept it, so a
    place could be repopulated to EXACTLY ONE member over HTTP. That is now
    refused by name (`PLACE_GROUP_DISSOLVED`) — asserted below — because
    re-tenanting a group id hands the new members a stranger's ledger through
    `get_place_ledger`.

    (2) The ARM still has to work, so it is still tested. One-member places
    exist in data written before the refusal, and `release_group_history_to_address`
    passes `allow_memberless=True` precisely for this arm — without it an
    ordinary last-member removal 500s. The membership is therefore pointed by
    hand and the removal is driven over HTTP exactly as before.

    The rule applied is the SAME one, not a second one: the last member out
    takes the place's history. With nobody remaining, the DEPARTING address IS
    the last member, and "the bottles stay with the place" is meaningless when
    the place has nobody left to reach them.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    assert UserAddress.query.filter_by(address_group_id=group.id).count() == 0

    u3 = _user(db, "diss-d@example.com", "+998900000404")
    addr_d = _addr(db, u3.id)
    _seed(db, addr_d, u3, "5")
    total_before = _all_bottles()
    assert total_before == Decimal("9.00")          # addr_b's 4 + addr_d's 5

    # (1) Repopulating the dead group over HTTP is REFUSED by name.
    added = client.post(
        f"/api/v1/admin/place-groups/{group.id}/addresses",
        json={"addressIds": [addr_d.id], "reason": "moved in"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 400, added.get_json()
    payload = added.get_json()
    assert (payload.get("data") or {}).get("error_code") == "PLACE_GROUP_DISSOLVED", payload
    db.session.rollback()

    # (2) ...so the one-member place is built by hand — see the docstring.
    _force_one_member_place(db, addr_d.id, group.id)
    assert UserAddress.query.filter_by(address_group_id=group.id).count() == 1
    assert _place(addr_d.id) == Decimal("5.00")

    # ...then empty it again, over HTTP.
    removed = client.delete(
        f"/api/v1/admin/place-groups/{group.id}/addresses/{addr_d.id}",
        json={"reason": "moved out"},
        headers=admin_auth_headers,
    )

    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()["data"]["dissolved"] is True
    db.session.refresh(addr_d)
    assert addr_d.address_group_id is None
    # The five went WITH the last member out, on its own row.
    assert _place(addr_d.id) == Decimal("5.00")
    row = BottleTrackingService.get_place_balance_row(addr_d.id)
    assert row.address_id == addr_d.id and row.address_group_id is None
    # The PAIR: nothing minted, nothing stranded, anywhere.
    assert _all_bottles() == total_before
    # No unreachable group row, and the anchor row itself is still kept.
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
    assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []
    assert AddressGroup.query.get(group.id) is not None


@pytest.mark.integration
def test_emptying_a_one_member_group_conserves_alongside_a_split(db):
    """The zero-remaining arm with §7.1's split in the same call.

    The split writes a `:out` half attributed to the departing address into the
    group scope; the dissolve's re-stamp selector then picks that very row up.
    Conservation must survive that overlap — the address ends holding everything
    the place held, and the two split halves simply net to zero inside its own
    scope, because in retrospect nothing ever left.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    u3 = _user(db, "diss-e@example.com", "+998900000405")
    addr_e = _addr(db, u3.id)
    _seed(db, addr_e, u3, "6")
    total_before = _all_bottles()
    # A dissolved group is no longer a join target; the one-member place this
    # arm needs is built by hand instead. See
    # `test_emptying_a_repopulated_one_member_group_still_dissolves`.
    with pytest.raises(ValidationError) as exc:
        svc.add_addresses_to_group(
            group.id, [addr_e.id], acting_admin_id=admin.id, reason="moved in"
        )
    assert exc.value.error_code == "PLACE_GROUP_DISSOLVED"
    db.session.rollback()
    _force_one_member_place(db, addr_e.id, group.id)

    result = svc.remove_address_from_group(
        addr_e.id, acting_admin_id=admin.id, reason="took two", bottles_leaving=2)

    assert result["dissolved"] is True
    assert _place(addr_e.id) == Decimal("6.00")     # the split had nowhere to split TO
    assert _all_bottles() == total_before           # the pair
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
    assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []


@pytest.mark.integration
def test_the_memberless_address_group_row_is_kept(db):
    """Spec §7.3 says the group "is then deleted"; that is not implementable.

    `bottle_ledger.address_group_id` is an FK and the departed members' entries
    still carry it. Dropping the row would orphan that FK, and NULLing those
    entries would put the PLACE's history inside a departed address's own scope
    under §3.1's predicate — minting bottles onto an address that left with
    nothing. The memberless row is inert (no address resolves to it) and is kept
    as the anchor of that history.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    assert AddressGroup.query.get(group.id) is not None
    assert UserAddress.query.filter_by(address_group_id=group.id).count() == 0
    # addr_a's history is still reachable through that anchor.
    assert BottleLedger.query.filter_by(address_group_id=group.id).count() >= 1
    # ...and no balance row remains, so the nightly sweep stays quiet.
    assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []
    assert reconcile_customer_link_invariants()["stranded_address_balances"] == []


@pytest.mark.integration
def test_the_dissolve_stamps_the_FORWARDING_POINTER_onto_the_kept_group_row(db):
    """The kept row is not just an anchor — it is a SIGNPOST.

    Keeping a memberless `AddressGroup` (above) preserves the FK the departed
    members' entries still carry, but on its own it preserves no way to ACT on
    them: a fine or a delivery frozen to that group names a place with no members
    and no `bottle_balances` row, and every write against it was refused.

    `dissolved_onto_address_id` is what turns the anchor into a destination. It
    is written in the SAME transaction that deletes the group's balance row —
    "the place's figure moved HERE" and "the place has no figure any more" are
    one fact, and a reader that saw the second without the first would refuse a
    write that is perfectly bookable.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    assert AddressGroup.query.get(group.id).dissolved_onto_address_id is None, (
        "a LIVE place must never carry a forwarding pointer"
    )

    result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    assert result["dissolved"] is True
    db.session.expire_all()

    # The pointer names the SURVIVING address — never the departed one, and
    # never the group itself.
    group_row = AddressGroup.query.get(group.id)
    assert group_row.dissolved_onto_address_id == addr_b.id
    # ...and it is the address the figure actually landed on.
    assert _place(addr_b.id) == Decimal("4.00")
    # The two facts really are one transaction: the balance row is gone.
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0


@pytest.mark.integration
def test_a_removal_that_does_NOT_dissolve_leaves_the_pointer_NULL(db):
    """The control. A pointer on a LIVE place would forward writes off it.

    Removing one member of a three-member place leaves the place alive, so its
    frozen references are still perfectly resolvable and must keep resolving to
    the group itself. Stamping a pointer here would send every subsequent frozen
    write to one member's own scope while the place kept trading — splitting the
    two halves of one handover across two ledgers, which is exactly what freezing
    exists to prevent.
    """
    svc, admin, u1, addr_a, u2, addr_b, addr_c, _u3 = _three_member_place(db)
    _seed(db, addr_a, u1, "9")
    group = AddressGroup.query.filter(AddressGroup.id.isnot(None)).one()

    result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    assert result["dissolved"] is False
    db.session.expire_all()

    assert AddressGroup.query.get(group.id).dissolved_onto_address_id is None
    assert UserAddress.query.filter_by(address_group_id=group.id).count() == 2
    # Nowhere in the database is a pointer set by a non-dissolve.
    assert AddressGroup.query.filter(
        AddressGroup.dissolved_onto_address_id.isnot(None)
    ).count() == 0


@pytest.mark.integration
def test_the_pointer_is_WRITE_ONCE_because_a_dissolved_place_cannot_be_rejoined(db):
    """No chaining, no rewriting, no second contradictory destination.

    The column is never updated after the dissolve, and the reason is structural
    rather than a discipline anyone has to keep: a group dissolves exactly once,
    because `add_addresses_to_group` refuses a memberless group as a join target
    (`PLACE_GROUP_DISSOLVED`). Without that refusal the group could be
    re-populated and dissolved again, and the second dissolve would silently
    overwrite the destination the first one's still-frozen references depend on.

    It also never has to CHAIN. The pointer names an ADDRESS, and the address's
    LIVE scope is re-resolved at read time — so a survivor that has since joined
    a new place forwards to that place with the pointer untouched.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    db.session.expire_all()
    assert AddressGroup.query.get(group.id).dissolved_onto_address_id == addr_b.id

    # The only path that could produce a SECOND dissolve is refused by name.
    with pytest.raises(ValidationError) as exc:
        svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id, reason="back")
    assert exc.value.error_code == "PLACE_GROUP_DISSOLVED"
    db.session.rollback()
    db.session.expire_all()
    assert AddressGroup.query.get(group.id).dissolved_onto_address_id == addr_b.id

    # And the NO-CHAIN property: the survivor joins a NEW place, and the old
    # pointer still names the address — the new place is found by re-resolving
    # it, not by rewriting anything.
    u3 = _user(db, "diss-d@example.com", "+998900000404")
    addr_d = _addr(db, u3.id)
    new_group = svc.create_place_group(
        [addr_b.id, addr_d.id], acting_admin_id=admin.id, reason="moved in together"
    )
    db.session.expire_all()
    assert AddressGroup.query.get(group.id).dissolved_onto_address_id == addr_b.id
    target = BottleTrackingService.resolve_frozen_scope_for_write(addr_a.id, group.id)
    assert target.forwarded is True
    assert target.unreachable is False
    assert target.scope == BottleScope.for_group(new_group.id), (
        "the pointer names an ADDRESS; its LIVE scope must be re-resolved, so a "
        "survivor that joined a new place forwards to THAT place"
    )
    assert target.address_id == addr_b.id


@pytest.mark.integration
def test_the_dissolve_is_recorded_on_the_removal_episode(db):
    """ONE audit episode, marked as the dissolve. `reason` is String(500) and the
    marker must survive an over-long admin prose."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    events_before = CustomerLinkEvent.query.count()

    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="x" * 600)

    assert CustomerLinkEvent.query.count() == events_before + 1
    event = (CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group")
             .order_by(CustomerLinkEvent.id.desc()).first())
    assert len(event.reason) <= 500
    assert event.reason.endswith(" | place dissolved onto its last member")
    assert event.reason.startswith(f"[group {group.id}] ")
    assert event.event_metadata["dissolved_onto_address_id"] == addr_b.id


# --------------------------------------------------------------------------- #
# §7.3 — the address-deletion fence, on ALL THREE delete entry points
# --------------------------------------------------------------------------- #

@pytest.fixture
def manager_headers(app, admin_user):
    """`manager_or_higher_required` reads the role off the JWT CLAIMS, which the
    shared `admin_auth_headers` fixture does not mint — without it the admin
    delete route 403s before ever reaching the fence."""
    from flask_jwt_extended import create_access_token

    with app.app_context():
        token = create_access_token(identity=str(admin_user.id),
                                    additional_claims={"role": UserRole.ADMIN.value})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def grouped_address(db, sample_user):
    """`sample_user` owns one GROUPED address plus one spare ungrouped one.

    The spare exists so the "cannot delete the only address" guard (which fires
    first on two of the three paths) cannot be what a fence test observes.
    """
    coworker = _user(db, "fence-b@example.com", "+998900000411")
    admin = _user(db, "fence-admin@example.com", "+998900000419")
    grouped = _addr(db, sample_user.id)
    spare = _addr(db, sample_user.id)
    CustomerLinkService().create_place_group(
        [grouped.id, _addr(db, coworker.id).id], acting_admin_id=admin.id, reason="office")
    db.session.refresh(grouped)
    assert grouped.address_group_id is not None
    return grouped, spare


@pytest.mark.integration
def test_a_grouped_address_cannot_be_deleted_by_the_customer_route(db, client, auth_headers,
                                                                   grouped_address):
    """A grouped address has no balance row of its own, so the IntegrityError
    that used to stop this never fires for exactly the members who share a pool."""
    grouped, _spare = grouped_address

    response = client.delete(f"/api/v1/addresses/{grouped.id}", headers=auth_headers)

    assert response.status_code == 400
    assert response.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert UserAddress.query.get(grouped.id) is not None


@pytest.mark.integration
def test_a_grouped_address_cannot_be_deleted_by_the_admin_route(db, client, manager_headers,
                                                               sample_user, grouped_address):
    grouped, _spare = grouped_address

    response = client.delete(
        f"/api/v1/admin/users/{sample_user.id}/addresses/{grouped.id}", headers=manager_headers)

    assert response.status_code == 400
    assert response.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert UserAddress.query.get(grouped.id) is not None


@pytest.mark.integration
def test_a_grouped_address_cannot_be_deleted_through_auth_service(db, sample_user, grouped_address):
    grouped, _spare = grouped_address

    with pytest.raises(ValidationError) as exc:
        AuthService().delete_user_address(sample_user.id, grouped.id)

    assert exc.value.error_code == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert UserAddress.query.get(grouped.id) is not None


@pytest.mark.integration
def test_the_auth_route_forwards_the_fence_code(db, client, auth_headers, grouped_address):
    """The service raises; the route must surface the code, not a bare 500."""
    grouped, _spare = grouped_address

    response = client.delete(f"/api/v1/auth/addresses/{grouped.id}", headers=auth_headers)

    assert response.status_code == 400
    assert response.get_json()["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    assert UserAddress.query.get(grouped.id) is not None


@pytest.mark.integration
def test_an_ungrouped_address_deletes_as_before(db, client, auth_headers, manager_headers,
                                                sample_user, grouped_address):
    """The fence must be narrow: nothing else about deletion changes."""
    _grouped, spare = grouped_address
    extra = _addr(db, sample_user.id)

    response = client.delete(f"/api/v1/addresses/{spare.id}", headers=auth_headers)
    assert response.status_code == 200
    assert UserAddress.query.get(spare.id) is None

    admin_response = client.delete(
        f"/api/v1/admin/users/{sample_user.id}/addresses/{extra.id}", headers=manager_headers)
    assert admin_response.status_code == 200
    assert UserAddress.query.get(extra.id) is None


@pytest.mark.integration
def test_a_removed_address_becomes_deletable_again(db, client, auth_headers, grouped_address):
    """The fence is a redirect, not a wall: remove it from the place first —
    which routes through §7.1 and makes the bottle question explicit."""
    grouped, _spare = grouped_address
    admin = User.query.filter_by(email="fence-admin@example.com").one()

    CustomerLinkService().remove_address_from_group(
        grouped.id, acting_admin_id=admin.id, reason="moving out")

    response = client.delete(f"/api/v1/addresses/{grouped.id}", headers=auth_headers)
    assert response.status_code == 200
    assert UserAddress.query.get(grouped.id) is None
