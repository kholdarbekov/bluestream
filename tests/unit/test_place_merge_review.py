"""Spec §7.4 — the admin reviews the merged ledger before committing a join.

No historical `quantity` is ever rewritten: an excluded entry is neutralised by
an APPENDED reversing ADMIN_ADJUSTMENT, and an overridden balance by one more.

CONSERVATION, stated honestly. A plain join moves bottles and conserves them
exactly. An exclusion and a `resulting_balance` override are AUTHORITATIVE
ADMIN CORRECTIONS: they are *meant* to change what the system believes the
place holds, because the admin has counted the crates and the ledger has not.
What makes that legitimate rather than a silent mint is that every unit of
change is CARRIED BY an appended ledger entry whose `quantity` IS the delta.
So the invariant asserted here is the auditable form of conservation:

    Σ balances AFTER − Σ balances BEFORE == Σ quantities of the entries the
                                            correction appended

Assert it as that PAIR — over EVERY `BottleBalance` row, not one place, since a
one-sided assertion passes for a bug that also zeroed the other side — and any
movement outside the ledger fails it.

Everything drives the real service write paths (`admin_adjust_balance`,
`create_place_group`, `add_addresses_to_group`, `remove_address_from_group`).
No hand-built `BottleBalance` rows.
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
from business_app.utils.exceptions import ValidationError
from shared.enums import BottleLedgerEventType, UserRole, UserStatus, UserType


# --------------------------------------------------------------------------- #
# Helpers — mirror Task 3's (`tests/unit/test_place_join_rescoping.py`).
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
    u1 = _user(db, "merge-a@example.com", "+998900000501")
    u2 = _user(db, "merge-b@example.com", "+998900000502")
    admin = _user(db, "merge-admin@example.com", "+998900000509")
    svc = CustomerLinkService()
    return svc, admin, u1, _addr(db, u1.id), u2, _addr(db, u2.id)


def _all_bottles():
    """Every bottle the system materialises, across EVERY scope.

    The conservation pair is asserted against this, never against one place: a
    correction that landed the reviewed place on the right number while quietly
    zeroing another scope's row would satisfy a one-sided assertion.
    """
    return sum((b.balance for b in BottleBalance.query.all()), Decimal("0.00"))


def _coupled_quantities():
    """Every BALANCE-COUPLED merge-review quantity: exclusions and the override.

    THE CONSERVATION PIN IS ASSERTED AGAINST THIS, AND `merge_backfill` IS A
    NAMED, DELIBERATE EXCLUSION FROM IT. Do not "restore" it to cover every
    `merge_%` key and conclude the backfill mints bottles — it does not move a
    balance at all.

    A backfill records an OPENING BALANCE THE LEDGER NEVER RECORDED: the place
    already held those bottles, the stored figure already said so, and only the
    ledger was silent. No crate arrives, so no balance moves; a balance-coupled
    write there would mint the drift a second time. It is the single sanctioned
    use of `BottleTrackingService._create_ledger_backfill_entry`, the only
    balance-decoupled writer in the codebase.

    So the invariant splits in two, and `_backfill_quantities` below carries the
    other half — both are asserted, neither is dropped:

        Σ balances after − before == Σ COUPLED quantities
        Σ ledger   after − before == Σ COUPLED + Σ BACKFILL quantities
    """
    return sum(
        (
            e.quantity
            for e in BottleLedger.query.filter(
                BottleLedger.idempotency_key.like("merge_exclude:%")
                | BottleLedger.idempotency_key.like("merge_correction:%")
            ).all()
        ),
        Decimal("0.00"),
    )


def _backfill_quantities():
    """The balance-DECOUPLED half: ledger-only, never a balance movement."""
    return sum(
        (
            e.quantity
            for e in BottleLedger.query.filter(
                BottleLedger.idempotency_key.like("merge_backfill:%")
            ).all()
        ),
        Decimal("0.00"),
    )


def _all_ledger():
    """Every ledger quantity in the system, across every scope."""
    return sum((e.quantity for e in BottleLedger.query.all()), Decimal("0.00"))


def _drift(db, address, qty):
    """Reproduce the production divergence this feature exists to REPAIR.

    A stored `bottle_balances` figure the ledger does not explain. In
    production it came from manual pre-grouping adjustments: the join CARRIES
    each joiner's stored figure rather than re-deriving it (spec §7.2 —
    rebuilding from ledger sums would zero any place seeded before the ledger),
    and the owner has confirmed the divergence is expected and known.

    The ROW is still created by the real write path (`_seed`); only its figure
    is moved without a matching entry, exactly as that history did. No
    hand-built `BottleBalance`.
    """
    row = BottleTrackingService.get_place_balance_row(address.id)
    assert row is not None, "_drift needs a place that has already moved a bottle"
    row.balance = (row.balance or Decimal("0.00")) + Decimal(qty)
    db.session.commit()


def _ledger_sum(scope):
    return sum(
        (e.quantity for e in BottleLedger.query.filter(*scope.ledger_filter()).all()),
        Decimal("0.00"),
    )


# --------------------------------------------------------------------------- #
# The preview
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_preview_merges_chronologically_and_totals_before_exclusions(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")

    preview = BottleTrackingService.build_merge_preview([addr_a.id, addr_b.id])

    assert preview["computed_balance"] == Decimal("7.00")
    assert preview["excluded_total"] == Decimal("0.00")
    assert preview["resulting_balance"] == Decimal("7.00")
    ids = [e.id for e in preview["entries"]]
    assert ids == sorted(ids)                       # (occurred_at, id) ordering
    running = Decimal("0.00")
    for entry in preview["entries"]:
        running += entry.quantity
        assert entry.preview_balance_after == running


@pytest.mark.unit
def test_preview_never_mutates_the_stored_balance_after(db):
    """`preview_balance_after` is a TRANSIENT attribute. Writing the merged
    running total onto the live `balance_after` column would let a READ rewrite
    the history of a merge the admin then cancels."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    stored = {e.id: e.balance_after for e in BottleLedger.query.all()}

    BottleTrackingService.build_merge_preview([addr_a.id, addr_b.id])
    db.session.flush()

    assert {e.id: e.balance_after for e in BottleLedger.query.all()} == stored
    # ...and each address still reads its OWN place, un-merged.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("3.00")


@pytest.mark.unit
def test_preview_subtracts_the_exclusions_it_is_given(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id

    preview = BottleTrackingService.build_merge_preview(
        [addr_a.id, addr_b.id], excluded_ledger_entry_ids=[drop])

    assert preview["computed_balance"] == Decimal("7.00")
    assert preview["excluded_total"] == Decimal("4.00")
    assert preview["resulting_balance"] == Decimal("3.00")


@pytest.mark.unit
def test_preview_of_an_existing_group_includes_the_groups_own_entries(db):
    """Joining an EXISTING group previews the group's ledger too — otherwise the
    admin decides against the joiner's history alone and the `resulting_balance`
    override would be measured against a figure that is not the place's."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="office")
    addr_c = _addr(db, u1.id)
    _seed(db, addr_c, u1, "2")

    preview = BottleTrackingService.build_merge_preview([addr_c.id], group_id=group.id)

    assert preview["computed_balance"] == Decimal("9.00")
    assert preview["resulting_balance"] == Decimal("9.00")


@pytest.mark.unit
def test_preview_raises_for_a_missing_address_or_group(db):
    """Skipping an unknown id would hand the admin a confident preview of a
    DIFFERENT merge — and the `resulting_balance` override would then be
    measured against it. Raising is also what makes the admin route a 404
    rather than the 500 its bare `except` would produce (spec §13)."""
    from business_app.utils.exceptions import NotFoundError

    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")

    with pytest.raises(NotFoundError):
        BottleTrackingService.build_merge_preview([addr_a.id, 999999])
    with pytest.raises(NotFoundError):
        BottleTrackingService.build_merge_preview([addr_a.id, addr_b.id], group_id=999999)


@pytest.mark.unit
def test_preview_of_a_rejoin_cannot_pull_the_former_groups_rows(db):
    """The §7.2 selector, in the preview: a departed address's entries stay
    stamped with its FORMER group, and `address_id = a` alone would show the
    admin — and then merge — a whole other place's history."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "5")
    addr_q = _addr(db, u2.id)                       # a quiet third member
    g1 = svc.create_place_group([addr_a.id, addr_b.id, addr_q.id],
                                acting_admin_id=admin.id, reason="office 1")
    old_ids = {e.id for e in BottleLedger.query.filter_by(address_group_id=g1.id).all()}
    assert old_ids
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")
    _seed(db, addr_a, u1, "2")                      # a fresh, address-scoped entry

    addr_c = _addr(db, u2.id)
    preview = BottleTrackingService.build_merge_preview([addr_a.id, addr_c.id])

    assert {e.id for e in preview["entries"]} & old_ids == set()
    assert preview["computed_balance"] == Decimal("2.00")


# --------------------------------------------------------------------------- #
# Exclusions
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_an_exclusion_writes_a_reversing_adjustment_and_never_rewrites_history(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one()
    original_quantity, drop_id = drop.quantity, drop.id

    group = svc.create_place_group(
        [addr_a.id, addr_b.id], acting_admin_id=admin.id,
        reason="the 4 was a data-entry error", excluded_ledger_entry_ids=[drop_id],
    )

    assert db.session.get(BottleLedger, drop_id).quantity == original_quantity   # untouched
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    reversal = BottleLedger.query.filter_by(
        idempotency_key=f"merge_exclude:{group.id}:{event.id}:{drop_id}").one()
    assert reversal.quantity == -original_quantity
    assert reversal.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
    assert reversal.address_group_id == group.id
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("3.00")


@pytest.mark.unit
def test_an_exclusion_changes_the_total_by_exactly_what_it_appended(db):
    """The auditable-conservation pair. The exclusion is SUPPOSED to remove 4
    bottles from what the system believes it holds — and it may do so only
    through the reversing entry it appended, never by touching a balance
    directly."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id
    total_before = _all_bottles()
    assert total_before == Decimal("7.00")

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                           reason="miscount", excluded_ledger_entry_ids=[drop])

    assert _coupled_quantities() == Decimal("-4.00")
    assert _all_bottles() - total_before == _coupled_quantities()
    # ...and the place's figure is exactly its own ledger sum: nothing moved
    # outside the append-only record.
    scope = BottleScope.for_group(BottleTrackingService.resolve_scope(addr_a.id).group_id)
    ledger_sum = sum(
        (e.quantity for e in BottleLedger.query.filter(*scope.ledger_filter()).all()),
        Decimal("0.00"),
    )
    assert BottleTrackingService.get_place_balance(addr_a.id) == ledger_sum


@pytest.mark.unit
def test_the_preview_itself_rejects_an_ineligible_exclusion(db):
    """The decision aid and the committer must not disagree about the SAME
    input. Silently ignoring a stray id let the preview render a confident
    `resulting_balance` for an exclusion the commit would then refuse."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    addr_c = _addr(db, u2.id)
    _seed(db, addr_c, u2, "9")
    stranger = BottleLedger.query.filter_by(address_id=addr_c.id).one().id

    with pytest.raises(ValidationError) as exc:
        BottleTrackingService.build_merge_preview(
            [addr_a.id, addr_b.id], excluded_ledger_entry_ids=[stranger])
    assert exc.value.error_code == "MERGE_EXCLUSION_NOT_ELIGIBLE"


@pytest.mark.unit
def test_an_oversized_merge_cannot_be_corrected_even_if_the_route_is_bypassed(db):
    """The cap stops a correction being COMMITTED against a merge the admin
    could never display. Enforcing it only at the preview route left the
    mutating call open to a client that skipped the preview.

    A plain join of the same addresses is deliberately still allowed — the cap
    is about correcting what you cannot see, not about group size."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")

    original = BottleTrackingService.MERGE_PREVIEW_MAX_ENTRIES
    BottleTrackingService.MERGE_PREVIEW_MAX_ENTRIES = 1
    try:
        with pytest.raises(ValidationError) as exc:
            svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted", resulting_balance=Decimal("5"))
        assert "above the 1" in str(exc.value)
        db.session.rollback()

        # ...and the plain join still goes through.
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    finally:
        BottleTrackingService.MERGE_PREVIEW_MAX_ENTRIES = original

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("7.00")


@pytest.mark.unit
def test_an_entry_outside_this_merge_is_rejected(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    addr_c = _addr(db, u2.id)
    _seed(db, addr_c, u2, "9")
    stranger = BottleLedger.query.filter_by(address_id=addr_c.id).one().id

    with pytest.raises(ValidationError) as exc:
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                               reason="r", excluded_ledger_entry_ids=[stranger])
    assert exc.value.error_code == "MERGE_EXCLUSION_NOT_ELIGIBLE"


@pytest.mark.unit
def test_a_rejected_merge_writes_nothing_at_all(db):
    """The guards run BEFORE the group row, the membership pointers and the
    audit event are written. A rejection that had already flushed an
    `AddressGroup` would leave it for the NEXT commit on this session to adopt."""
    from business_app.models.customer_link import AddressGroup

    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    addr_c = _addr(db, u2.id)
    _seed(db, addr_c, u2, "9")
    stranger = BottleLedger.query.filter_by(address_id=addr_c.id).one().id
    total_before = _all_bottles()

    with pytest.raises(ValidationError):
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                               reason="r", excluded_ledger_entry_ids=[stranger])
    db.session.rollback()

    assert AddressGroup.query.count() == 0
    assert CustomerLinkEvent.query.count() == 0
    assert db.session.get(UserAddress, addr_a.id).address_group_id is None
    assert _all_bottles() == total_before


@pytest.mark.unit
def test_an_already_excluded_entry_cannot_be_reversed_twice(db):
    """Episode scoping stops a retry swallowing a distinct episode, but it must
    NOT let a second episode reverse an entry the first one already neutralised.

    THE PLACE MUST STILL BE LIVE for this guard to be the one under test. A
    THIRD member is seeded so the removal below does not trigger §7.3's dissolve
    — a dissolved group is now refused as a join target by name
    (`PLACE_GROUP_DISSOLVED`, see the test right below), which would mask this
    assertion entirely.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    addr_c = _addr(db, u2.id)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id
    group = svc.create_place_group([addr_a.id, addr_b.id, addr_c.id], acting_admin_id=admin.id,
                                   reason="r", excluded_ledger_entry_ids=[drop])
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    with pytest.raises(ValidationError) as exc:
        svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id,
                                   reason="re-add", excluded_ledger_entry_ids=[drop])
    assert exc.value.error_code == "MERGE_EXCLUSION_NOT_ELIGIBLE"


@pytest.mark.unit
def test_a_DISSOLVED_group_is_refused_as_a_join_target(db):
    """A group id must denote exactly ONE tenancy.

    §7.1/§7.3 deliberately leave a departed member's ledger rows stamped with
    the group they left, and `bottle_ledger.address_group_id` is a foreign key,
    so a dissolved group keeps its row and its whole history for ever.
    Re-populating that id makes `get_place_ledger` — which filters on
    `address_group_id` alone — hand the new members a STRANGER's delivery
    history and residual balance.

    Refused under rung 0 (`address_groups` FOR NO KEY UPDATE), so unlike an
    unlocked existence check this is not a TOCTOU.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="r")
    svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

    with pytest.raises(ValidationError) as exc:
        svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id, reason="re-add")
    assert exc.value.error_code == "PLACE_GROUP_DISSOLVED"


# --------------------------------------------------------------------------- #
# The resulting-balance override
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_the_override_is_measured_against_the_post_exclusion_figure(db):
    """Exclusions first, THEN the override. Measuring the override against
    `computed_balance` instead would double-count the exclusion."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id

    group = svc.create_place_group(
        [addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="counted them",
        excluded_ledger_entry_ids=[drop], resulting_balance=Decimal("5"),
    )

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    correction = BottleLedger.query.filter_by(
        idempotency_key=f"merge_correction:{group.id}:{event.id}").one()
    assert correction.quantity == Decimal("2.00")     # 5 - (7 - 4), NOT 5 - 7


@pytest.mark.unit
def test_the_override_can_only_move_bottles_through_an_appended_entry(db):
    """THE guard on the most dangerous thing in this plan.

    An override lets an admin STATE a number instead of deriving one, so it can
    legitimately raise or lower the total. What stops it being a silent mint is
    that the difference is not applied to the balance directly: the service
    computes `delta = stated − post-exclusion figure` and appends ONE
    ADMIN_ADJUSTMENT of exactly `delta`, and the balance moves only as that
    entry's side effect. Asserted as the pair — the change in the GLOBAL total
    equals the quantity of what was appended, and the place's figure equals its
    own ledger sum.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    total_before = _all_bottles()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 10 crates", resulting_balance=Decimal("10"))

    correction = BottleLedger.query.filter_by(
        idempotency_key=f"merge_correction:{group.id}:"
        f"{CustomerLinkEvent.query.filter_by(event_type='create_place_group').one().id}"
    ).one()
    assert correction.quantity == Decimal("3.00")           # 10 - 7
    assert _all_bottles() - total_before == correction.quantity
    assert _all_bottles() - total_before == _coupled_quantities()

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == _ledger_sum(scope) == Decimal("10.00")

    # On a CLEAN place — stored figure and ledger sum already agree — NO
    # backfill row is written and nothing about the pre-fix behaviour changes.
    # This is the "skip it when there is no drift" pin.
    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_backfill:%")).count() == 0


@pytest.mark.unit
def test_an_override_downwards_destroys_nothing_silently_either(db):
    """The mirror case. Stating a LOWER number must also travel through the
    ledger, so the bottles it removes are recoverable from the audit trail."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    total_before = _all_bottles()

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                           reason="only 1 crate on site", resulting_balance=Decimal("1"))

    assert _coupled_quantities() == Decimal("-6.00")
    assert _all_bottles() - total_before == Decimal("-6.00")
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("1.00")


@pytest.mark.unit
def test_an_override_equal_to_the_figure_appends_nothing(db):
    """A zero delta must not litter the ledger with 0-quantity noise, and must
    leave the plain-join conservation EXACT."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    total_before = _all_bottles()

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                           reason="confirmed", resulting_balance=Decimal("7"))

    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_correction:%")).count() == 0
    assert _all_bottles() == total_before


@pytest.mark.unit
def test_a_join_without_a_merge_review_conserves_exactly(db):
    """The control. With no exclusion and no override nothing is appended, so
    the auditable pair collapses back to strict conservation."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    total_before = _all_bottles()

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    assert _coupled_quantities() == Decimal("0.00")
    assert _all_bottles() == total_before


@pytest.mark.unit
def test_the_corrections_are_recomputed_into_the_running_snapshots(db):
    """The adjustments land AFTER the absorb, so the `balance_after` pass has to
    run last. A stale snapshot reads right in the summary and wrong in history."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted", excluded_ledger_entry_ids=[drop],
                                   resulting_balance=Decimal("5"))

    scope = BottleScope.for_group(group.id)
    ordered = (BottleLedger.query.filter(*scope.ledger_filter())
               .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all())
    running = Decimal("0.00")
    for entry in ordered:
        running += entry.quantity
        assert entry.balance_after == running
    assert ordered[-1].balance_after == BottleTrackingService.get_place_balance(addr_a.id)


@pytest.mark.unit
def test_the_corrections_are_recorded_on_the_join_event(db):
    """Both are stamped with the join `CustomerLinkEvent` (spec §7.4's last
    line), so the group's audit trail can explain the figure."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                           reason="counted", excluded_ledger_entry_ids=[drop],
                           resulting_balance=Decimal("5"))

    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert event.event_metadata["excluded_ledger_entry_ids"] == [drop]
    assert event.event_metadata["resulting_balance"] == "5"
    # Task 3's key survives alongside the new ones.
    assert "rescoped_ledger_entry_ids" in event.event_metadata
    reversal = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_exclude:%")).one()
    assert reversal.entry_metadata["acting_admin_id"] == admin.id
    assert reversal.entry_metadata["excluded_ledger_entry_id"] == drop
    assert reversal.entry_metadata["reason"] == "counted"


# --------------------------------------------------------------------------- #
# Convergence on a DRIFTED place — the stored figure and the ledger sum
# disagree, which is the state this feature exists to repair (spec §7.2 carry).
#
# The shapes below are the REAL ones on the dev database, not invented ones:
#   * address 24 (user 68, "Home"): stored 20.00, ZERO ledger rows, drift 20.
#     Manually adjusted, never grouped — the shape the owner says broke.
#   * group 9 (addresses 44+45): stored 7.00, ledger 6+5-4 = 7.00, drift 0.
#     Clean, and must come through a merge review byte-for-byte unchanged.
# --------------------------------------------------------------------------- #

def _address_24_shape(db):
    """Stored 20.00 with a ledger that sums to 0.00 and has no rows for it.

    The +20 is seeded through the real write path and then REMOVED from the
    ledger, which is the only way to reproduce "stored 20, zero ledger rows"
    without hand-building a `BottleBalance`. The balance row itself is the one
    `admin_adjust_balance` created.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "20")
    BottleLedger.query.filter_by(address_id=addr_a.id).delete(synchronize_session=False)
    db.session.commit()
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("20.00")
    assert BottleLedger.query.count() == 0
    return svc, admin, u1, addr_a, u2, addr_b


def _group_9_shape(db):
    """Stored 7.00 and a ledger of +6, +5, -4 that sums to 7.00. Drift 0."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "6")
    _seed(db, addr_b, u2, "5")
    _seed(db, addr_b, u2, "-4")
    assert _all_bottles() == Decimal("7.00")
    assert _all_ledger() == Decimal("7.00")
    return svc, admin, u1, addr_a, u2, addr_b


@pytest.mark.unit
def test_address_24_shape_lands_both_figures_on_the_stated_number(db):
    """stored 20 / ledger 0, admin states 12 -> BOTH figures land on 12.

    Two designs failed here before. Measuring the delta against the ledger and
    landing it on the carried figure gave 32 (`20 + (12 - 0)`). Absorbing the
    drift as a COUPLED `-20` gave the right balance and a ledger of -8, which
    asserts twenty bottles left the place on a day nothing left — and the admin
    panel's Reconcile button would then set the balance to -8 and destroy the
    admin's number.

    The backfill is a LEDGER FACT, not a bottle movement: +20 with no balance
    movement, lifting the ledger onto the figure the place already held.
    """
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)
    balances_before, ledger_before = _all_bottles(), _all_ledger()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 12 crates", resulting_balance=Decimal("12"))

    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    backfill = BottleLedger.query.filter_by(
        idempotency_key=f"merge_backfill:{group.id}:{event.id}").one()
    correction = BottleLedger.query.filter_by(
        idempotency_key=f"merge_correction:{group.id}:{event.id}").one()
    assert backfill.quantity == Decimal("20.00")      # stored 20 - ledger 0, POSITIVE
    assert correction.quantity == Decimal("-8.00")    # stated 12 - stored 20

    scope = BottleScope.for_group(group.id)
    # BOTH figures, on 12. This equality was impossible under the coupled
    # design and is the strongest single guard on the feature.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("12.00")
    assert _ledger_sum(scope) == Decimal("12.00")

    # The renegotiated pair. Balances move by the COUPLED quantities only; the
    # ledger moves by backfill + coupled. Both halves pinned, neither dropped.
    assert _all_bottles() - balances_before == _coupled_quantities() == Decimal("-8.00")
    assert _all_ledger() - ledger_before == _backfill_quantities() + _coupled_quantities()
    assert _backfill_quantities() == Decimal("20.00")


@pytest.mark.unit
def test_group_9_shape_is_untouched_by_the_backfill(db):
    """stored 7 / ledger 7, admin states 10 -> ONE coupled +3, NO backfill row.

    A place whose ledger already explains its figure must come through exactly
    as it did before the backfill existed.
    """
    svc, admin, u1, addr_a, u2, addr_b = _group_9_shape(db)
    balances_before, ledger_before = _all_bottles(), _all_ledger()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 10", resulting_balance=Decimal("10"))

    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_backfill:%")).count() == 0
    assert _backfill_quantities() == Decimal("0.00")
    correction = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_correction:%")).one()
    assert correction.quantity == Decimal("3.00")     # stated 10 - stored 7

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("10.00")
    assert _ledger_sum(scope) == Decimal("10.00")
    assert _all_bottles() - balances_before == _coupled_quantities() == Decimal("3.00")
    assert _all_ledger() - ledger_before == Decimal("3.00")


@pytest.mark.unit
def test_the_backfill_records_everything_needed_to_reconstruct_it(db):
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 12 crates", resulting_balance=Decimal("12"))

    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    backfill = BottleLedger.query.filter_by(
        idempotency_key=f"merge_backfill:{group.id}:{event.id}").one()
    assert backfill.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
    assert backfill.address_group_id == group.id
    assert backfill.entry_metadata["source"] == "merge_backfill"
    assert backfill.entry_metadata["stored_before"] == "20.00"
    assert backfill.entry_metadata["ledger_sum_before"] == "0.00"
    assert backfill.entry_metadata["stated_resulting_balance"] == "12"
    assert backfill.entry_metadata["acting_admin_id"] == admin.id
    assert backfill.entry_metadata["reason"] == "counted 12 crates"


@pytest.mark.unit
def test_the_backfill_moves_no_balance_at_all(db):
    """The decoupling, isolated. A review whose ONLY effect is the backfill
    (`resulting_balance` equal to what the place already holds) must move the
    ledger and leave every balance exactly where it was."""
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)
    balances_before, ledger_before = _all_bottles(), _all_ledger()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="confirmed 20", resulting_balance=Decimal("20"))

    assert _all_bottles() == balances_before             # NOTHING moved
    assert _all_ledger() - ledger_before == Decimal("20.00")
    assert _coupled_quantities() == Decimal("0.00")      # the delta was 0, so no correction
    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_correction:%")).count() == 0
    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == _ledger_sum(scope) == Decimal("20.00")


@pytest.mark.unit
def test_exclusions_alone_land_on_stored_minus_excluded(db):
    """Falls out of the backfill, with NO special case.

    The backfill lifts the ledger onto the stored figure without moving the
    balance; the exclusions then move both equally. So an exclusions-only
    review lands on `stored - excluded` — 12 - 4 = 8 — and the two figures
    agree there.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    _drift(db, addr_a, "5")                        # stored 12, ledger 7
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id
    preview = BottleTrackingService.build_merge_preview(
        [addr_a.id, addr_b.id], excluded_ledger_entry_ids=[drop])
    assert preview["stored_balance"] == Decimal("12.00")
    assert preview["projected_place_balance"] == Decimal("8.00")   # 12 - 4
    balances_before, ledger_before = _all_bottles(), _all_ledger()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="the 4 never happened", excluded_ledger_entry_ids=[drop])

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("8.00")
    assert _ledger_sum(scope) == Decimal("8.00")
    assert _all_bottles() - balances_before == _coupled_quantities() == Decimal("-4.00")
    assert _all_ledger() - ledger_before == _backfill_quantities() + _coupled_quantities()


@pytest.mark.unit
def test_a_negative_drift_converges_too(db):
    """The OTHER direction: the ledger recorded MORE than the place holds.

    stored 2 / ledger 7, so the backfill is NEGATIVE (-5) — it retires a
    surplus the ledger over-recorded rather than adding an opening balance it
    never had. Everything else is identical, and it must converge just as
    hard: this is why the entry's `notes` are sign-neutral and why nothing in
    `_create_ledger_backfill_entry` may assume a positive quantity.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    _drift(db, addr_a, "-5")                       # stored 2, ledger 7
    preview = BottleTrackingService.build_merge_preview([addr_a.id, addr_b.id])
    assert preview["computed_balance"] == Decimal("7.00")
    assert preview["stored_balance"] == Decimal("2.00")
    assert preview["drift"] == Decimal("-5.00")
    balances_before, ledger_before = _all_bottles(), _all_ledger()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 5", resulting_balance=Decimal("5"))

    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    backfill = BottleLedger.query.filter_by(
        idempotency_key=f"merge_backfill:{group.id}:{event.id}").one()
    correction = BottleLedger.query.filter_by(
        idempotency_key=f"merge_correction:{group.id}:{event.id}").one()
    assert backfill.quantity == Decimal("-5.00")   # stored 2 - ledger 7, NEGATIVE
    assert correction.quantity == Decimal("3.00")  # stated 5 - stored 2

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")
    assert _ledger_sum(scope) == Decimal("5.00")
    assert _all_bottles() - balances_before == _coupled_quantities() == Decimal("3.00")
    assert _all_ledger() - ledger_before == _backfill_quantities() + _coupled_quantities()

    # The audit line must read correctly in THIS direction too — a note that
    # says "opening balance the ledger never recorded" is false for a surplus.
    assert "opening balance" not in (backfill.notes or "").lower()


@pytest.mark.unit
def test_the_decoupled_writer_refuses_a_key_outside_its_namespace(db):
    """The constant is ENFORCED, not documentation.

    A decoupled entry must be identifiable by its key: `_coupled_quantities`
    tells coupled from decoupled by prefix, so an unkeyed or foreign-keyed
    decoupled write would be counted as COUPLED and pass the conservation pin
    it violates.
    """
    from business_app.services.bottle_scope import BottleScope as _Scope

    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    bottles = BottleTrackingService()
    scope = _Scope.for_address(addr_a.id)
    shared = dict(scope=scope, user_id=u1.id, address_id=addr_a.id,
                  quantity=Decimal("1"), actor_user_id=admin.id, notes="n")

    for bad_key in (None, "", "delivery:123", "merge_correction:1:2"):
        with pytest.raises(ValidationError) as exc:
            bottles._create_ledger_backfill_entry(idempotency_key=bad_key, **shared)
        assert exc.value.error_code == "BOTTLE_DECOUPLED_KEY_REQUIRED", bad_key

    # ...and a key inside the namespace is accepted, so the guard is not simply
    # rejecting everything.
    entry = bottles._create_ledger_backfill_entry(idempotency_key="merge_backfill:1:2", **shared)
    assert entry.quantity == Decimal("1")
    # The whole point: no balance moved.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")


@pytest.mark.unit
def test_the_combined_exclusion_and_override_path_lands_on_stated(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    _drift(db, addr_a, "5")                        # stored 12, ledger 7
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id
    balances_before, ledger_before = _all_bottles(), _all_ledger()

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 6", excluded_ledger_entry_ids=[drop],
                                   resulting_balance=Decimal("6"))

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("6.00")
    assert _ledger_sum(scope) == Decimal("6.00")
    # stated 6 - (stored 12 - excluded 4) = -2, NOT 6 - (7 - 4) = +3.
    correction = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_correction:%")).one()
    assert correction.quantity == Decimal("-2.00")
    assert _all_bottles() - balances_before == _coupled_quantities()
    assert _all_ledger() - ledger_before == _backfill_quantities() + _coupled_quantities()


@pytest.mark.unit
def test_a_second_review_restating_the_same_number_still_yields_it(db):
    """Convergence across EPISODES. No sequence of previews converged before:
    each commit moved the place by the drift again. Stating N must yield N
    every time, on a place that has already been corrected once — and the
    second review finds NO drift left to backfill."""
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)
    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted 12", resulting_balance=Decimal("12"))
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("12.00")
    backfills_after_first = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_backfill:%")).count()
    assert backfills_after_first == 1

    addr_c = _addr(db, u2.id)
    svc.add_addresses_to_group(group.id, [addr_c.id], acting_admin_id=admin.id,
                               reason="still 12", resulting_balance=Decimal("12"))

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("12.00")
    assert BottleTrackingService.get_place_balance(addr_c.id) == Decimal("12.00")
    assert _ledger_sum(scope) == Decimal("12.00")
    # The first review already converged the two figures, so the second finds
    # no drift and writes no second backfill.
    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_backfill:%")).count() == 1


@pytest.mark.unit
@pytest.mark.parametrize("stated", [Decimal("12"), Decimal("0"), Decimal("-3")])
def test_a_reviewed_merge_leaves_the_place_balance_equal_to_its_ledger_sum(db, stated):
    """THE guard. After any reviewed merge the two figures agree, so the admin
    panel's Reconcile button (`api/admin_bottles.py`) becomes a no-op on the
    result instead of a destroyer of it. Impossible under the balance-coupled
    design, where the two differed by the drift by construction.

    `-3` is deliberate: a place CAN be over-returned (spec §1.2/§16 leaves the
    return quantity unbounded), so the equality must hold there too.
    """
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                                   reason="counted", resulting_balance=stated)

    scope = BottleScope.for_group(group.id)
    assert BottleTrackingService.get_place_balance(addr_a.id) == _ledger_sum(scope) == stated


@pytest.mark.unit
def test_the_preview_exposes_the_stored_figure_the_drift_and_the_projection(db):
    """The admin has to SEE what they are correcting, and the preview has to
    predict its own outcome — otherwise the number they type is a guess."""
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)

    plain = BottleTrackingService.build_merge_preview([addr_a.id, addr_b.id])
    assert plain["computed_balance"] == Decimal("0.00")
    assert plain["stored_balance"] == Decimal("20.00")
    assert plain["drift"] == Decimal("20.00")
    # Nothing excluded => the place will hold what it already holds.
    assert plain["projected_place_balance"] == Decimal("20.00")


@pytest.mark.unit
def test_the_projection_is_what_a_plain_join_actually_produces(db):
    """The `projected_place_balance` for a no-correction merge must not be
    aspirational: commit it and the place holds exactly that."""
    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)
    projected = BottleTrackingService.build_merge_preview(
        [addr_a.id, addr_b.id])["projected_place_balance"]

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")

    assert BottleTrackingService.get_place_balance(addr_a.id) == projected == Decimal("20.00")
    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_%")).count() == 0


# --------------------------------------------------------------------------- #
# Attribution: a PLACE-level correction is not a member's activity
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_a_place_level_correction_does_not_inflate_a_members_departure_prefill(db):
    """`suggested_bottles_leaving` sums the member's OWN attributed entries. A
    place-level `merge_correction` carries a member's (user_id, address_id)
    only because the columns are NOT NULL — counting it would pre-fill that one
    coworker's departure with bottles that belong to the whole place, and an
    admin accepting the default would split them onto the wrong person."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    addr_q = _addr(db, u2.id)                       # keeps the place alive on removal

    group = svc.create_place_group([addr_a.id, addr_b.id, addr_q.id], acting_admin_id=admin.id,
                                   reason="counted 10", resulting_balance=Decimal("10"))
    correction = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_correction:%")).one()
    assert correction.address_id == addr_a.id       # the borrowed stamp

    # addr_a's OWN activity is still 4 — the +3 correction is the place's.
    assert BottleTrackingService.suggested_bottles_leaving(group.id, addr_a.id) == Decimal("4.00")
    assert BottleTrackingService.suggested_bottles_leaving(group.id, addr_b.id) == Decimal("3.00")


@pytest.mark.unit
def test_an_exclusion_reversal_is_still_counted_in_the_prefill(db):
    """Narrowness guard for the fix above: `merge_exclude` must NOT be skipped.

    A reversal is attributed to the very entry it neutralises, so it cancels
    that address's own contribution. Skipping it would leave the excluded
    quantity in the pre-fill — the same class of quantity error in the other
    direction."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    addr_q = _addr(db, u2.id)
    drop = BottleLedger.query.filter_by(address_id=addr_a.id).one().id

    group = svc.create_place_group([addr_a.id, addr_b.id, addr_q.id], acting_admin_id=admin.id,
                                   reason="the 4 was an error", excluded_ledger_entry_ids=[drop])

    # 4 + (-4) = 0: addr_a's own contribution is gone, as the admin decided.
    assert BottleTrackingService.suggested_bottles_leaving(group.id, addr_a.id) == Decimal("0.00")


@pytest.mark.unit
def test_a_place_level_correction_is_not_shown_as_a_members_own_row(db):
    """The customer place ledger suppresses `notes`, so a borrowed attribution
    shows one coworker an unexplained +/-N flagged as theirs."""
    from business_app.serializers.bottle_serializers import serialize_customer_place_ledger_entry

    svc, admin, u1, addr_a, u2, addr_b = _address_24_shape(db)
    _seed(db, addr_b, u2, "3")          # a genuine, attributed member entry
    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                           reason="counted 12", resulting_balance=Decimal("12"))

    for key in ("merge_correction:%", "merge_backfill:%"):
        entry = BottleLedger.query.filter(BottleLedger.idempotency_key.like(key)).one()
        row = serialize_customer_place_ledger_entry(entry, viewer_user_id=entry.user_id)
        assert row["is_own"] is False, key
        assert row["member_name"] is None, key
        # Deliberately NO new key: the row shape is a whitelist pinned by
        # tests/unit/test_customer_place_ledger_gate.py, and with member_name
        # None + is_own False the bot already renders the line unattributed.
        assert "is_place_level" not in row, key

    # A genuine member entry is untouched: still named, still `is_own`.
    own = BottleLedger.query.filter(
        BottleLedger.idempotency_key.is_(None), BottleLedger.address_id == addr_b.id).first()
    assert own is not None
    own_row = serialize_customer_place_ledger_entry(own, viewer_user_id=own.user_id)
    assert own_row["is_own"] is True
    assert own_row["member_name"]


# --------------------------------------------------------------------------- #
# Staleness and the mandatory reason
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_a_stale_preview_is_rejected(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    stale = [e.id for e in BottleLedger.query.all()]
    _seed(db, addr_b, u2, "3")            # the ledger moved under the admin

    with pytest.raises(ValidationError) as exc:
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                               reason="r", resulting_balance=Decimal("2"),
                               preview_entry_ids=stale)
    assert exc.value.error_code == "MERGE_PREVIEW_STALE"


@pytest.mark.unit
def test_a_matching_preview_is_accepted(db):
    """The staleness guard must not be unfalsifiable — the SAME set passes."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    preview = BottleTrackingService.build_merge_preview([addr_a.id, addr_b.id])

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                           reason="counted", resulting_balance=Decimal("8"),
                           preview_entry_ids=list(reversed(preview["entry_ids"])))

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("8.00")


@pytest.mark.unit
@pytest.mark.parametrize("kwargs", [
    {"excluded_ledger_entry_ids": [1]},
    {"resulting_balance": Decimal("5")},
])
def test_a_correction_without_a_reason_is_rejected(db, kwargs):
    svc, admin, _u1, addr_a, _u2, addr_b = _two_ungrouped_customers(db)
    with pytest.raises(ValidationError) as exc:
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                               reason="   ", **kwargs)
    assert exc.value.error_code == "MERGE_REASON_REQUIRED"


@pytest.mark.unit
@pytest.mark.parametrize("stated", ["NaN", "Infinity", "-Infinity", "not-a-number"])
def test_a_non_finite_resulting_balance_is_refused_and_moves_nothing(db, stated):
    """NaN is REACHABLE, not theoretical: Python's own JSON parser accepts the
    `NaN` and `Infinity` literals, `Decimal("NaN")` constructs happily, and
    every comparison against NaN is False — so an unguarded NaN sails past
    `delta != 0` straight into `bottle_ledger.quantity`, and the place's balance
    becomes NaN forever. The same trap `_validated_bottles_leaving` documents
    for `bottles_leaving`.
    """
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    _seed(db, addr_b, u2, "3")
    total_before = _all_bottles()

    with pytest.raises(ValidationError):
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                               reason="counted", resulting_balance=Decimal(stated)
                               if stated != "not-a-number" else stated)
    db.session.rollback()

    assert _all_bottles() == total_before
    assert BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_correction:%")).count() == 0


@pytest.mark.unit
def test_a_non_integer_exclusion_id_is_a_rejection_not_a_crash(db):
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")

    with pytest.raises(ValidationError):
        svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id,
                               reason="r", excluded_ledger_entry_ids=["oops"])


@pytest.mark.unit
def test_a_plain_join_still_needs_no_extra_reason_handling(db):
    """Narrowness guard (cannot be red-first): MERGE_REASON_REQUIRED must fire
    ONLY when a correction is present. A blank reason on a plain join is the
    route's business, not the merge review's — widening it here would break
    `create_address_group`, which passes an empty reason today."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")

    group = svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="")

    assert group.id is not None
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")


# --------------------------------------------------------------------------- #
# The scope-row assertion (§13, BOTTLE_SCOPE_INVALID)
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_a_balance_row_with_two_scope_keys_is_rejected(db):
    """Defence in depth behind ck_bottle_balance_scope.

    NOT because the suite skips CHECKs — SQLite enforces them, only FOREIGN
    KEYS are off (see the NOTE on the nightly-sweep test below). The CHECK
    fires at FLUSH time as an opaque `IntegrityError` that every caller's bare
    `except` reports as "referenced by existing records"; this rejects the row
    earlier, by name, and covers any database predating the constraint.
    """
    bad = BottleBalance(address_group_id=1, address_id=2, balance=Decimal("1"))
    with pytest.raises(ValidationError) as exc:
        BottleTrackingService.assert_scope_row_valid(bad)
    assert exc.value.error_code == "BOTTLE_SCOPE_INVALID"


@pytest.mark.unit
def test_a_balance_row_with_no_scope_key_is_rejected(db):
    bad = BottleBalance(address_group_id=None, address_id=None, balance=Decimal("1"))
    with pytest.raises(ValidationError) as exc:
        BottleTrackingService.assert_scope_row_valid(bad)
    assert exc.value.error_code == "BOTTLE_SCOPE_INVALID"


@pytest.mark.unit
def test_a_well_formed_row_from_the_real_write_path_passes(db):
    """The assertion is on `get_or_create_balance`'s hot path, so it has to stay
    quiet for every legitimate row — grouped and ungrouped alike."""
    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    BottleTrackingService.assert_scope_row_valid(
        BottleTrackingService.get_place_balance_row(addr_a.id))

    svc.create_place_group([addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="office")
    BottleTrackingService.assert_scope_row_valid(
        BottleTrackingService.get_place_balance_row(addr_a.id))


@pytest.mark.unit
def test_the_nightly_sweep_reports_invalid_scope_rows(db):
    """The report key that finds rows the in-process assertion never saw.

    NOTE, against the brief's premise: SQLite DOES enforce CHECK constraints
    (`tests/unit/test_bottle_scope_schema.py::test_balance_scope_check_is_enforced`
    pins exactly that) — it is only FOREIGN KEYS that are off. So a violating
    row cannot simply be inserted here. That is precisely the state this sweep
    exists for: a database whose `ck_bottle_balance_scope` is absent, i.e. rows
    written before the constraint or by a migration that rebuilt the table
    without it. `PRAGMA ignore_check_constraints` reproduces that database, so
    the query is proven to FIND the row rather than merely to run.
    """
    from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants

    if db.engine.dialect.name != "sqlite":
        pytest.skip("CHECK suppression here is SQLite-specific")

    svc, admin, u1, addr_a, u2, addr_b = _two_ungrouped_customers(db)
    _seed(db, addr_a, u1, "4")
    assert reconcile_customer_link_invariants()["invalid_scope_balances"] == []

    db.session.execute(db.text("PRAGMA ignore_check_constraints = ON"))
    try:
        db.session.execute(
            BottleBalance.__table__.insert().values(
                address_group_id=None, address_id=None, balance=Decimal("2.00")
            )
        )
        db.session.flush()
    finally:
        db.session.execute(db.text("PRAGMA ignore_check_constraints = OFF"))
    bad_ids = [
        b.id for b in BottleBalance.query.all()
        if (b.address_group_id is None) == (b.address_id is None)
    ]
    assert bad_ids

    assert reconcile_customer_link_invariants()["invalid_scope_balances"] == sorted(bad_ids)
