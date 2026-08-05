"""Nightly link-layer invariant checks (Phase 2).

Cross-customer place groups are a SANCTIONED state
(docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md §3),
so the Phase-1 ``address_group_cross_customer`` key is gone. Every remaining key
gets a test that proves it FIRES and a clean-state control that proves it does
not false-positive.

Bottle balances are re-keyed to the PLACE (spec 2026-07-27 §3): one
``bottle_balances`` row per place, not one per ``(user, address)`` pair. That
retires two Phase-1 contracts entirely: ``negative_group_unions`` (an N+1 loop
over ``get_group_union_balance``) becomes ``negative_place_balances`` — a
single ``WHERE balance < 0`` query — and ``stranded_negative_pairs`` (a
negative pair inside a non-negative union) is deleted outright, because with
only one balance row per place that state can no longer occur.

``stranded_address_balances`` is the one key the re-key ADDED: an address-keyed
row whose address has since joined a place group (spec §7.2) is unreachable by
every place-scoped read. The write path that used to mint one is fixed — group
join now re-scopes the balance via
``BottleTrackingService.absorb_address_into_group`` — so the key survives as a
BACKSTOP for a direct DB edit, a restore from a pre-re-scoping dump, or a future
write path that bypasses that helper. The tests below manufacture the row
directly, which is exactly the shape the backstop has to keep catching.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.customer_link import CanonicalCustomer, AddressGroup, CustomerLinkEvent
from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, Payment
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from shared.enums import (
    BottleLedgerEventType, CashCollectionSource, EntitySubtype, OrderStatus, PaymentMethod,
    PaymentStatus, UserRole, UserStatus, UserType,
)
from business_app.utils.password_security import hash_password

_EMPTY_REPORT = {
    "negative_place_balances": [],
    "orphaned_canonical_pointers": [],
    "grocery_or_entity_members": [],
    "events_missing_scope_snapshot": [],
    "allocation_stamp_mismatches": [],
    "event_conservation_violations": [],
    "orphaned_place_balances": [],
    "stranded_address_balances": [],
    "stranded_fine_scopes": [],
    "invalid_scope_balances": [],
    "stamp_incoherent_ledger_entries": [],
    "duplicate_rescoped_ledger_entries": [],
    "group_check_errors": [],
}


def _user(db, email, phone, *, canonical_id=None, user_type=UserType.INDIVIDUAL,
          entity_subtype=None, company_name=None):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=user_type, role=UserRole.CUSTOMER,
             entity_subtype=entity_subtype, company_name=company_name,
             status=UserStatus.ACTIVE, is_verified=True, canonical_customer_id=canonical_id,
             created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id, group_id=None):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=41.31, longitude=69.28, address_group_id=group_id)
    db.session.add(a); db.session.commit()
    return a


_LEDGER_SEQ = [0]


def _ledger(db, user_id, address_id, *, group_id=None, quantity="1.00"):
    """One ``bottle_ledger`` row with an EXPLICIT scope stamp.

    Built directly rather than through ``BottleTrackingService`` on purpose:
    the stamp-coherence check exists to catch rows whatever wrote them, and
    routing through the service could only ever produce the shapes the service
    already gets right.
    """
    _LEDGER_SEQ[0] += 1
    row = BottleLedger(
        user_id=user_id,
        address_id=address_id,
        address_group_id=group_id,
        event_type=BottleLedgerEventType.DELIVERY,
        quantity=Decimal(quantity),
        balance_after=Decimal(quantity),
        occurred_at=datetime.now(UTC),
        idempotency_key=f"recon-test:{_LEDGER_SEQ[0]}",
        entry_metadata={},
    )
    db.session.add(row); db.session.commit()
    return row


_UNSET = object()


def _link_event(db, event_type, *, rescoped=None, dissolved=None, metadata=_UNSET):
    """A ``customer_link_events`` row carrying the custody keys the sweep replays."""
    if metadata is _UNSET:
        metadata = {}
        if rescoped is not None:
            metadata["rescoped_ledger_entry_ids"] = rescoped
        if dissolved is not None:
            metadata["dissolved_rescoped_ledger_entry_ids"] = dissolved
    row = CustomerLinkEvent(
        event_type=event_type,
        canonical_customer_id=None,
        acting_admin_id=None,
        member_user_ids=[],
        reason="",
        event_metadata=metadata,
    )
    db.session.add(row); db.session.commit()
    return row


def _event(db, customer_id, amount, unapplied, *, scope_type="personal", scope_snapshot=None):
    e = CashCollectionEvent(
        customer_id=customer_id, amount=Decimal(amount), currency="UZS",
        source=CashCollectionSource.ADMIN_ADJUSTMENT, occurred_at=datetime.now(UTC),
        unapplied_amount=Decimal(unapplied), scope_type=scope_type, scope_snapshot=scope_snapshot,
    )
    db.session.add(e); db.session.commit()
    return e


def _payment(db, user, order_number):
    order = Order(user_id=user.id, order_number=order_number, status=OrderStatus.DELIVERED,
                  subtotal=Decimal("10000.00"), delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                  total_amount=Decimal("10000.00"), payment_method=PaymentMethod.CASH,
                  created_at=datetime.now(UTC))
    db.session.add(order); db.session.flush()
    p = Payment(order_id=order.id, user_id=user.id, payment_method=PaymentMethod.CASH,
                amount=Decimal("10000.00"), currency="UZS", status=PaymentStatus.PENDING,
                payment_id=f"pay_{order_number}", outstanding_amount=Decimal("10000.00"),
                created_at=datetime.now(UTC))
    db.session.add(p); db.session.commit()
    return p


@pytest.mark.unit
class TestReconciliation:
    def test_clean_state_reports_empty_everything(self, db):
        _user(db, "a@example.com", "+998900000001")
        assert reconcile_customer_link_invariants() == _EMPTY_REPORT

    def test_cross_customer_group_is_sanctioned(self, db):
        """Phase 2: a place group spanning two canonical customers is a LEGAL,
        sanctioned state — no report key exists for it any more."""
        c1 = CanonicalCustomer(); c2 = CanonicalCustomer()
        db.session.add_all([c1, c2]); db.session.commit()
        u1 = _user(db, "a@example.com", "+998900000001", canonical_id=c1.id)
        u2 = _user(db, "b@example.com", "+998900000002", canonical_id=c2.id)
        group = AddressGroup(canonical_customer_id=None, label="office")
        db.session.add(group); db.session.commit()
        _addr(db, u1.id, group_id=group.id)
        _addr(db, u2.id, group_id=group.id)

        report = reconcile_customer_link_invariants()

        assert "address_group_cross_customer" not in report
        assert report == _EMPTY_REPORT

    def test_detects_negative_place_balance_and_orphaned_pointer(self, db):
        u_orphan = _user(db, "o@example.com", "+998900000004", canonical_id=999999)
        u = _user(db, "c@example.com", "+998900000003")
        group = AddressGroup(canonical_customer_id=None, label="home")
        db.session.add(group); db.session.commit()
        _addr(db, u.id, group_id=group.id)
        # One balance row per PLACE: the group, not the (user, address) pair.
        balance = BottleBalance(address_group_id=group.id, balance=Decimal("-3"))
        db.session.add(balance)
        db.session.commit()

        report = reconcile_customer_link_invariants()

        assert balance.id in report["negative_place_balances"]
        assert u_orphan.id in report["orphaned_canonical_pointers"]

    def test_detects_stranded_address_balance_on_a_now_grouped_address(self, db):
        """``stranded_address_balances`` fires for an address-keyed row whose
        address has since joined a place group (spec §7.2), and does NOT fire
        for an ungrouped address's own row — the ordinary production shape."""
        u = _user(db, "s@example.com", "+998900000031")
        group = AddressGroup(canonical_customer_id=None, label="office")
        db.session.add(group); db.session.commit()
        grouped_addr = _addr(db, u.id, group_id=group.id)
        solo_addr = _addr(db, u.id)

        stranded = BottleBalance(address_id=grouped_addr.id, balance=Decimal("4"))
        healthy = BottleBalance(address_id=solo_addr.id, balance=Decimal("6"))
        db.session.add_all([stranded, healthy])
        db.session.commit()

        report = reconcile_customer_link_invariants()

        assert report["stranded_address_balances"] == [stranded.id]
        assert healthy.id not in report["stranded_address_balances"]
        # Distinct from its inverse: the group is live, so nothing is orphaned.
        assert report["orphaned_place_balances"] == []

    # ---------------------------------------------------------------- #
    # ORACLE 2 — stamp coherence, with its known-bad control
    # ---------------------------------------------------------------- #

    def test_detects_a_ledger_row_left_UNSTAMPED_at_a_now_GROUPED_address(self, db):
        """KNOWN-BAD CONTROL for ``stamp_incoherent_ledger_entries``.

        The bad state is built directly, which is the point: the check has to
        keep catching it whatever produced it — a scope resolved before the
        lock, a direct DB edit, a restore from a dump, or a future write path.
        The healthy row in the same fixture is what stops this passing for a
        check that simply returns every ledger id it can see.
        """
        u = _user(db, "l@example.com", "+998900000041")
        group = AddressGroup(canonical_customer_id=None, label="office")
        db.session.add(group); db.session.commit()
        grouped_addr = _addr(db, u.id, group_id=group.id)
        solo_addr = _addr(db, u.id)

        incoherent = _ledger(db, u.id, grouped_addr.id, group_id=None)
        coherent = _ledger(db, u.id, grouped_addr.id, group_id=group.id)
        ungrouped = _ledger(db, u.id, solo_addr.id, group_id=None)

        report = reconcile_customer_link_invariants()

        assert report["stamp_incoherent_ledger_entries"] == [incoherent.id]
        assert coherent.id not in report["stamp_incoherent_ledger_entries"]
        assert ungrouped.id not in report["stamp_incoherent_ledger_entries"]
        # Nothing about the BALANCE side is wrong here — which is exactly why
        # the two reachability buckets cannot see this.
        assert report["orphaned_place_balances"] == []
        assert report["stranded_address_balances"] == []

    def test_does_NOT_flag_a_row_stamped_to_a_group_the_address_has_LEFT(self, db):
        """The SANCTIONED §7.1 split outcome must stay silent.

        A departing address keeps the history it made at the place; only the
        agreed ``bottles_leaving`` quantity travels with it. A check that fires
        on the normal outcome of a normal admin action gets muted, and then it
        is not a check.
        """
        u = _user(db, "d@example.com", "+998900000042")
        stayer = _user(db, "d2@example.com", "+998900000043")
        group = AddressGroup(canonical_customer_id=None, label="office")
        db.session.add(group); db.session.commit()
        _addr(db, stayer.id, group_id=group.id)
        departed = _addr(db, u.id)  # pointer already cleared by the split

        left_behind = _ledger(db, u.id, departed.id, group_id=group.id)

        report = reconcile_customer_link_invariants()

        assert left_behind.id not in report["stamp_incoherent_ledger_entries"]
        assert report["stamp_incoherent_ledger_entries"] == []

    # ---------------------------------------------------------------- #
    # ORACLE 3 — custody replay, with its known-bad control
    # ---------------------------------------------------------------- #

    def test_detects_TWO_episodes_claiming_CUSTODY_of_one_ledger_entry(self, db):
        """KNOWN-BAD CONTROL for ``duplicate_rescoped_ledger_entries``.

        Two joins absorbing one address: both audit events claim entry 4242,
        and no dissolve ever handed it back in between. Both places are
        internally consistent, so neither reachability nor stamp coherence can
        see it — this replay is the only oracle that can.
        """
        _link_event(db, "create_place_group", rescoped=[4242, 4243])
        _link_event(db, "add_to_place_group", rescoped=[4242])

        report = reconcile_customer_link_invariants()

        assert report["duplicate_rescoped_ledger_entries"] == [4242]
        assert 4243 not in report["duplicate_rescoped_ledger_entries"]

    def test_a_DISSOLVE_then_REGROUP_reclaims_the_same_entries_and_is_LEGAL(self, db):
        """The reason this is a custody replay and not a duplicate scan.

        The design's literal wording — "the union of ``rescoped_ledger_entry_ids``
        must contain no duplicates" — reports THIS sequence, which is an
        ordinary admin round trip that the randomised soak generates on its own.
        A dissolve hands the entries back to the survivor's own scope; the next
        group legitimately claims them again.
        """
        _link_event(db, "create_place_group", rescoped=[7001, 7002])
        _link_event(db, "remove_from_place_group", dissolved=[7001, 7002])
        _link_event(db, "create_place_group", rescoped=[7001, 7002])

        report = reconcile_customer_link_invariants()

        assert report["duplicate_rescoped_ledger_entries"] == []

    def test_a_reclaim_of_only_SOME_released_entries_still_flags_the_rest(self, db):
        """Order matters, and the replay respects it: 8001 was handed back and
        re-claimed (legal); 8002 was never released and is claimed twice."""
        _link_event(db, "create_place_group", rescoped=[8001, 8002])
        _link_event(db, "remove_from_place_group", dissolved=[8001])
        _link_event(db, "add_to_place_group", rescoped=[8001, 8002])

        report = reconcile_customer_link_invariants()

        assert report["duplicate_rescoped_ledger_entries"] == [8002]

    def test_events_with_no_metadata_at_all_are_skipped_not_crashed(self, db):
        """Every pre-existing row has ``event_metadata`` NULL (model comment).
        A sweep that raises on them reports nothing at all, forever."""
        _link_event(db, "link", metadata=None)
        _link_event(db, "set_primary", metadata={})
        _link_event(db, "create_place_group", rescoped=[9001])

        assert reconcile_customer_link_invariants()["duplicate_rescoped_ledger_entries"] == []

    def test_detects_grocery_member_in_cluster_and_entity_owner_in_group(self, db):
        c = CanonicalCustomer(); db.session.add(c); db.session.commit()
        grocery = _user(db, "g@example.com", "+998900000005", canonical_id=c.id,
                        user_type=UserType.ENTITY, entity_subtype=EntitySubtype.GROCERY_STORE,
                        company_name="Shop")
        entity = _user(db, "e@example.com", "+998900000006", user_type=UserType.ENTITY,
                       entity_subtype=EntitySubtype.WORKPLACE, company_name="Acme")
        u = _user(db, "a@example.com", "+998900000001")
        group = AddressGroup(canonical_customer_id=None)
        db.session.add(group); db.session.commit()
        _addr(db, entity.id, group_id=group.id)
        _addr(db, u.id, group_id=group.id)

        report = reconcile_customer_link_invariants()

        assert grocery.id in report["grocery_or_entity_members"]
        assert entity.id in report["grocery_or_entity_members"]
        assert u.id not in report["grocery_or_entity_members"]

    def test_detects_scoped_event_without_snapshot(self, db):
        """Both on-disk shapes of "no snapshot" must be caught: an explicit
        ``scope_snapshot=None`` is stored by SQLAlchemy's JSON type as the
        literal JSON ``null`` (NOT SQL NULL), so an ``IS NULL``-only check would
        silently miss the writer pattern the money engine is most likely to use.
        """
        u = _user(db, "a@example.com", "+998900000001")
        ok_personal = _event(db, u.id, "1000.00", "1000.00")
        ok_cluster = _event(db, u.id, "1000.00", "1000.00",
                            scope_type="cluster", scope_snapshot={"user_ids": [u.id]})
        bad_json_null = _event(db, u.id, "1000.00", "1000.00",
                               scope_type="place", scope_snapshot=None)
        bad_sql_null = CashCollectionEvent(  # column omitted entirely -> SQL NULL
            customer_id=u.id, amount=Decimal("1000.00"), currency="UZS",
            source=CashCollectionSource.ADMIN_ADJUSTMENT, occurred_at=datetime.now(UTC),
            unapplied_amount=Decimal("1000.00"), scope_type="cluster",
        )
        db.session.add(bad_sql_null); db.session.commit()

        report = reconcile_customer_link_invariants()

        assert sorted(report["events_missing_scope_snapshot"]) == sorted(
            [bad_json_null.id, bad_sql_null.id]
        )
        assert ok_personal.id not in report["events_missing_scope_snapshot"]
        assert ok_cluster.id not in report["events_missing_scope_snapshot"]

    def test_detects_dual_stamp_mismatch_post_migration_only(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        other = _user(db, "b@example.com", "+998900000002")
        event = _event(db, u.id, "10000.00", "0.00")
        payment = _payment(db, u, "ORD-STAMP")
        good = CashCollectionAllocation(
            cash_collection_event_id=event.id, payment_id=payment.id,
            allocated_amount=Decimal("5000.00"), allocation_order=1,
            source_customer_id=u.id, beneficiary_user_id=u.id,
        )
        bad = CashCollectionAllocation(
            cash_collection_event_id=event.id, payment_id=payment.id,
            allocated_amount=Decimal("5000.00"), allocation_order=2,
            source_customer_id=other.id, beneficiary_user_id=u.id,  # wrong source stamp
        )
        legacy = CashCollectionAllocation(  # pre-migration rows: both stamps NULL -> skipped
            cash_collection_event_id=event.id, payment_id=payment.id,
            allocated_amount=Decimal("0.00"), allocation_order=3,
        )
        db.session.add_all([good, bad, legacy]); db.session.commit()

        report = reconcile_customer_link_invariants()

        assert bad.id in report["allocation_stamp_mismatches"]
        assert good.id not in report["allocation_stamp_mismatches"]
        assert legacy.id not in report["allocation_stamp_mismatches"]

    def test_detects_conservation_violation(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        payment = _payment(db, u, "ORD-CONS")
        # amount 10000 == live alloc 6000 + unapplied 4000 -> OK
        ok = _event(db, u.id, "10000.00", "4000.00")
        db.session.add(CashCollectionAllocation(
            cash_collection_event_id=ok.id, payment_id=payment.id,
            allocated_amount=Decimal("6000.00"), allocation_order=1))
        # amount 10000 != live alloc 5000 + unapplied 4000 -> violation
        broken = _event(db, u.id, "10000.00", "4000.00")
        db.session.add(CashCollectionAllocation(
            cash_collection_event_id=broken.id, payment_id=payment.id,
            allocated_amount=Decimal("5000.00"), allocation_order=1))
        db.session.commit()

        report = reconcile_customer_link_invariants()

        assert broken.id in report["event_conservation_violations"]
        assert ok.id not in report["event_conservation_violations"]
