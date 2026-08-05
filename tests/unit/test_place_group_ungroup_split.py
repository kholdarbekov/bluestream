"""Removing an address from a place group (spec §7.1) — the DEFAULT case.

The bottles stay with the place. The departing address starts a fresh scope at
0. There is no donor, no transfer and no clamp: `bottle_balances` has one row
per PLACE since migration a3e7d1f9c204, so a "negative pair inside a
non-negative union" is not representable and the netting mechanism it existed
to settle is deleted (spec §8).

The DELIBERATE alternative — the admin says some bottles leave WITH the address
— is `TestBottlesLeavingSplit` at the bottom of this file. It never changes the
default: `bottles_leaving` is opt-in and defaults to 0.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleLedger
from business_app.models.customer_link import CustomerLinkEvent
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserStatus,
    UserType,
)


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    db.session.add(a); db.session.commit()
    return a


def _seed_place(db, address, user, qty):
    """Put `qty` bottles at the address's PLACE through the real write path.

    Deliberately NOT a hand-inserted `BottleBalance`: the row is keyed by place,
    and only the service knows which place an address resolves to. Building the
    row by hand is exactly the habit that let the re-key ship green.
    """
    BottleTrackingService().admin_adjust_balance(
        user_id=user.id, address_id=address.id, adjustment=Decimal(qty),
        actor_user_id=user.id, notes="seed",
    )
    db.session.commit()


def _grouped_two_customers(db):
    """Two DISTINCT (unlinked) customers, one place group over their addresses.

    THREE member addresses, not two: §7.3 dissolves a place the moment a removal
    would leave it with exactly ONE member, and every test below is about §7.1's
    removal semantics with the place still standing. The third address is a
    second desk of the same coworker, never moves a bottle, and so changes no
    figure here — it only keeps the place alive past the first removal. §7.3's
    dissolve has its own file,
    ``tests/integration/test_place_dissolve_and_delete_fence.py``.
    """
    u1 = _user(db, "a@example.com", "+998900000001")
    u2 = _user(db, "b@example.com", "+998900000002")
    admin = _user(db, "admin@example.com", "+998900000009")
    svc = CustomerLinkService()
    addr_a, addr_b, addr_quiet = _addr(db, u1.id), _addr(db, u2.id), _addr(db, u2.id)
    group = svc.create_place_group([addr_a.id, addr_b.id, addr_quiet.id],
                                   acting_admin_id=admin.id, reason="office", label="office")
    return svc, admin, u1, addr_a, u2, addr_b, group


@pytest.mark.unit
class TestRemoveAddressFromGroup:
    def test_removal_leaves_the_bottles_with_the_place(self, db):
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")

        result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                               reason="left the office")

        db.session.refresh(addr_a)
        assert addr_a.address_group_id is None
        # Exact shape, so a surprise key still fails here. `bottles_leaving` is
        # the §7.1 opt-in split and its DEFAULT is what this test is about: zero.
        # `dissolved` is §7.3's flag; the place still has two members, so False.
        assert result == {"group_id": group.id, "bottles_leaving": Decimal("0.00"), "dissolved": False}
        assert "netting" not in result
        # The place keeps all seven; the departed address opens at zero.
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("0.00")

    def test_departing_address_starts_at_zero(self, db):
        svc, admin, u1, addr_a, u2, addr_b, _group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "5")
        # Pin the pre-condition: without it a no-op seed would leave the place at
        # 0 and the post-conditions below would pass while proving nothing.
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="moved")

        # ...and the departing address opens at 0 even though the place held 5.
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("0.00")
        assert BottleTrackingService.get_place_balance_row(addr_a.id) is None
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")

    def test_removal_posts_no_adjustments_by_default(self, db):
        svc, admin, u1, addr_a, _u2, addr_b, _group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "5")
        # Pin the pre-condition: a no-op seed would make "no NEW adjustments"
        # trivially true, so assert the place really is holding 5 first.
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")
        before = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count()
        assert before == 1  # the seed itself

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="moved")

        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == before

    def test_remove_readd_remove_writes_no_adjustments_by_default(self, db):
        """Groups are long-lived; remove -> re-add -> remove is routine. Both
        episodes must audit, and neither may move a bottle unasked."""
        svc, admin, u1, addr_a, _u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "9")

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="first")
        svc.add_addresses_to_group(group.id, [addr_a.id], acting_admin_id=admin.id, reason="re-add")
        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="second")

        events = CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").all()
        assert len(events) == 2
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 1  # the seed only
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("9.00")

    def test_requires_reason_and_grouped_address(self, db):
        # Carried over from the retired file unchanged: it asserts only the
        # three fence codes, none of which this plan changes.
        svc, admin, u1, addr_a, _u2, _addr_b, _group = _grouped_two_customers(db)
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="  ")
        assert exc.value.error_code == "PLACE_GROUP_REASON_REQUIRED"

        ungrouped = _addr(db, u1.id)
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(ungrouped.id, acting_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "PLACE_GROUP_NOT_FOUND"

        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(999999, acting_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "CUSTOMER_LINK_ADDRESS_NOT_FOUND"

    def test_ungroup_leaves_reservations_untouched(self, db):
        """Spec §5.7: place scope creates reservations only via the cluster-keyed
        ring-3 sweep, so ungroup has nothing to release. Guards against a future
        "release the group's reservations here" edit silently reversing money."""
        svc, admin, u1, addr_a, u2, addr_b, _group = _grouped_two_customers(db)
        cash = CashCollectionService()
        db.session.add(
            CashCollectionEvent(
                customer_id=u1.id, collector_user_id=admin.id, recorded_by_user_id=admin.id,
                amount=Decimal("50000.00"), currency="UZS", source="standalone_meeting",
                occurred_at=datetime.now(UTC), unapplied_amount=Decimal("50000.00"),
            )
        )
        order = Order(
            user_id=u1.id, order_number="ORD-UNGROUP-RES", status=OrderStatus.CONFIRMED,
            subtotal=Decimal("50000.00"), delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("50000.00"), payment_method=PaymentMethod.CASH,
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.flush()
        payment = cash.ensure_cod_payment_for_order(order)
        db.session.flush()
        reserved = cash.reserve_customer_prepaid_credit_for_payment(payment, actor_user_id=admin.id)
        db.session.commit()
        assert reserved == Decimal("50000.00")

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="left")

        allocation = CashCollectionAllocation.query.filter_by(
            payment_id=payment.id, allocation_mode="prepaid_reservation").one()
        assert allocation.reversed_at is None
        assert allocation.allocated_amount == Decimal("50000.00")


@pytest.mark.unit
class TestBottlesLeavingSplit:
    """Spec §7.1's deliberate alternative to the default: some of the place's
    bottles may leave WITH the departing address, when the admin says so.

    The default above stays exactly as it is — `bottles_leaving` is opt-in, and
    every assertion here is about the path where it is supplied.

    Bottles are CONSERVED in every arm: the place loses exactly what the address
    gains (two paired ADMIN_ADJUSTMENT rows summing to zero), and a rejected
    request moves nothing at all.
    """

    def test_split_moves_exactly_what_the_admin_entered(self, db):
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")

        result = svc.remove_address_from_group(
            addr_a.id, acting_admin_id=admin.id, reason="took two crates",
            bottles_leaving=Decimal("2"),
        )

        assert result["group_id"] == group.id
        assert result["bottles_leaving"] == Decimal("2.00")
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("2.00")

    def test_the_split_conserves_the_place_total(self, db):
        """The invariant, asserted as a PAIR rather than one side of it:
        whatever leaves is exactly what the place loses. Assert only
        `place_after == 5` and a bug that ALSO minted 2 at the address (or
        destroyed them) would sail through."""
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        place_before = BottleTrackingService.get_place_balance(addr_b.id)
        assert place_before == Decimal("7.00")

        result = svc.remove_address_from_group(
            addr_a.id, acting_admin_id=admin.id, reason="split", bottles_leaving=2)

        place_after = BottleTrackingService.get_place_balance(addr_b.id)
        departed = BottleTrackingService.get_place_balance(addr_a.id)
        # The pair. Nothing minted, nothing destroyed.
        assert place_before == place_after + result["bottles_leaving"]
        assert departed == result["bottles_leaving"]
        assert place_after + departed == place_before

    def test_the_two_halves_are_one_paired_conserving_move(self, db):
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        event_count_before = CustomerLinkEvent.query.count()

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                      reason="split", bottles_leaving=2)

        # Exactly ONE new audit event — the split is part of the removal episode,
        # not a second one.
        assert CustomerLinkEvent.query.count() == event_count_before + 1
        event = (CustomerLinkEvent.query
                 .filter_by(event_type="remove_from_place_group").one())
        out = BottleLedger.query.filter_by(
            idempotency_key=f"place_leave:{group.id}:{event.id}:{addr_a.id}:out").one()
        inn = BottleLedger.query.filter_by(
            idempotency_key=f"place_leave:{group.id}:{event.id}:{addr_a.id}:in").one()
        assert out.quantity == Decimal("-2.00")
        assert inn.quantity == Decimal("2.00")
        assert out.quantity + inn.quantity == Decimal("0.00")   # conserved
        # The running snapshots, not just the deltas: the place walks 7 -> 5 and
        # the departing address opens at 2.
        assert out.balance_after == Decimal("5.00")
        assert inn.balance_after == Decimal("2.00")
        assert out.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
        assert inn.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
        # Attribution: both halves name the departing address and its owner.
        assert out.address_id == addr_a.id and inn.address_id == addr_a.id
        assert out.user_id == u1.id and inn.user_id == u1.id
        assert out.actor_user_id == admin.id and inn.actor_user_id == admin.id
        # SCOPE: the out half stays in the place, the in half opens the address.
        assert out.address_group_id == group.id
        assert inn.address_group_id is None
        assert out.entry_metadata["source"] == "place_leave"
        assert out.entry_metadata["acting_admin_id"] == admin.id
        assert out.entry_metadata["reason"] == "split"
        assert out.entry_metadata["place_group_id"] == group.id

    def test_zero_writes_nothing(self, db):
        svc, admin, u1, addr_a, _u2, addr_b, _group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        before = BottleLedger.query.count()
        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                      reason="nothing left with them", bottles_leaving=0)
        assert BottleLedger.query.count() == before
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")
        assert BottleTrackingService.get_place_balance_row(addr_a.id) is None

    @pytest.mark.parametrize("leaving", [-1, 8])
    def test_negative_or_above_the_place_total_is_rejected(self, db, leaving):
        svc, admin, u1, addr_a, _u2, _addr_b, _group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                          reason="r", bottles_leaving=leaving)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"

    def test_a_rejected_split_moves_nothing_and_removes_nothing(self, db):
        """Conservation on the REJECTION path. The whole point of retiring
        netting was to stop silently absorbing impossible numbers, so a refused
        request must leave the place, the ledger, the audit trail AND the
        membership pointer exactly as they were.

        DO NOT add a `db.session.rollback()` before the assertions below. Its
        ABSENCE is the pin: `bottles_leaving` is validated BEFORE the
        `CustomerLinkEvent` is created and flushed, so a refused split never
        puts a phantom "address removed" row into the session at all. Validate
        after the flush instead and the event is flushed-but-uncommitted, which
        the `CustomerLinkEvent.query.count()` below sees via autoflush — and
        which the NEXT commit on this session would silently persist. A rollback
        here would discard that phantom and make the whole property untestable,
        exactly as the HTTP layer's `_rollback_db_session()` already masks it.
        """
        svc, admin, u1, addr_a, _u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        ledger_before = BottleLedger.query.count()
        events_before = CustomerLinkEvent.query.count()

        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                          reason="too many", bottles_leaving=99)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"

        # No rollback — see the docstring.
        assert CustomerLinkEvent.query.count() == events_before
        assert addr_a.address_group_id == group.id          # still a member
        assert BottleLedger.query.count() == ledger_before  # no half-written move
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("7.00")

        # And the session is still clean enough to be used: the caller may well
        # retry with a legal number, which must not inherit anything.
        result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                               reason="retry", bottles_leaving=3)
        assert result["bottles_leaving"] == Decimal("3.00")
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 1   # ONE episode, not two
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("4.00")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("3.00")

    @pytest.mark.parametrize("leaving", ["two", "", [], float("nan"), float("inf"), True])
    def test_a_non_numeric_bottles_leaving_is_rejected(self, db, leaving):
        """It arrives off an HTTP body, so it can be anything. NaN and Infinity
        are in here because Python's own JSON parser accepts both literals, and
        an unguarded NaN would sail past `leaving < 0` and `leaving > cap`
        (every NaN comparison is False) straight into the ledger."""
        svc, admin, u1, addr_a, _u2, addr_b, _group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "7")
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                          reason="r", bottles_leaving=leaving)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"
        # Nothing moved on the way out. (No rollback here either — see
        # test_a_rejected_split_moves_nothing_and_removes_nothing.)
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 1  # the seed

    def test_a_negative_place_forces_zero(self, db):
        """The cap sits BELOW the default when the place is over-returned, so
        'clamp to the cap' would produce a negative transfer. Reject instead."""
        svc, admin, u1, addr_a, _u2, addr_b, _group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "-3")
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                          reason="r", bottles_leaving=1)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"
        # ... and 0 still succeeds: the default must never be rejected by the cap.
        result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="r")
        assert result["bottles_leaving"] == Decimal("0.00")
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("-3.00")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("0.00")
        # No rollback above, so the refused attempt would still be sitting in
        # this session if it had written anything: ONE episode, not two.
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 1

    def test_an_empty_place_rejects_a_non_zero_split(self, db):
        """The third rejection arm at its boundary: a place holding exactly 0."""
        svc, admin, u1, addr_a, _u2, addr_b, _group = _grouped_two_customers(db)
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                          reason="r", bottles_leaving=1)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"
        assert svc.remove_address_from_group(
            addr_a.id, acting_admin_id=admin.id, reason="r")["bottles_leaving"] == Decimal("0.00")
        # No rollback above: ONE episode, not two.
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 1

    def test_prefill_is_this_address_own_entries_clamped_to_the_place(self, db):
        """addr_a took 4, addr_b took 9 => the place holds 13 and addr_a's own
        attributed sum is 4. The suggestion is 4, not 13 and not 0."""
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "4")
        _seed_place(db, addr_b, u2, "9")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("13.00")
        assert BottleTrackingService.suggested_bottles_leaving(
            group.id, addr_a.id) == Decimal("4.00")

    def test_prefill_never_exceeds_the_place_or_goes_negative(self, db):
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "9")
        _seed_place(db, addr_b, u2, "-6")     # coworker over-returned
        # addr_a's own sum is 9 but the place only holds 3.
        assert BottleTrackingService.suggested_bottles_leaving(
            group.id, addr_a.id) == Decimal("3.00")
        # And an address whose own sum is negative suggests 0, never a negative.
        assert BottleTrackingService.suggested_bottles_leaving(
            group.id, addr_b.id) == Decimal("0.00")

    def test_the_prefill_is_always_an_acceptable_bottles_leaving(self, db):
        """The suggestion and the validator must agree, or the dialog would
        pre-fill a value its own OK button rejects."""
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "9")
        _seed_place(db, addr_b, u2, "-6")
        suggestion = BottleTrackingService.suggested_bottles_leaving(group.id, addr_a.id)

        result = svc.remove_address_from_group(
            addr_a.id, acting_admin_id=admin.id, reason="took the lot",
            bottles_leaving=suggestion,
        )
        assert result["bottles_leaving"] == Decimal("3.00")
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("0.00")
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("3.00")

    def test_the_departed_address_keeps_only_its_split_not_the_place_history(self, db):
        """Spec §7.1: history stays with the place. The departing address's new
        scope opens at exactly `bottles_leaving` and its ledger holds only the
        `:in` half — the place's own rows are not dragged along."""
        svc, admin, u1, addr_a, u2, addr_b, group = _grouped_two_customers(db)
        _seed_place(db, addr_a, u1, "6")

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                      reason="split", bottles_leaving=4)

        db.session.refresh(addr_a)
        assert addr_a.address_group_id is None
        own_scope_rows = BottleLedger.query.filter(
            BottleLedger.address_id == addr_a.id,
            BottleLedger.address_group_id.is_(None),
        ).all()
        assert [r.quantity for r in own_scope_rows] == [Decimal("4.00")]
        assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")
        assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("2.00")
