"""Cluster-fungible prepaid credit + the two-phase payment/event lock discipline.

Plan 2b Task 5. Spec:
docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md §5.3.

Two invariants dominate this file:

1. **Credit is CLUSTER-only.** One real person's accounts share one wallet, so a
   linked sibling's unapplied over-collection may settle/reserve against another
   member's COD debt. A place group is NOT a person — coworkers share bottles and
   the place's COD debt, never each other's credit.
2. **One lock order everywhere.** Every path that locks ``payments`` rows must
   acquire them in ONE query ordered by ``payments.id`` ASC, and every path that
   locks ``cash_collection_events`` in ONE query ordered by ``id`` ASC, with the
   allocation/consumption ordering re-applied in memory afterwards. Mixed lock
   orders deadlock, and a deadlock here is worse than a 500: ``_allocate_to_payment``
   may already have enqueued ``send_payment_confirmation_task`` (which performs no
   status re-check) before blocking, so the abort can tell a customer their payment
   was confirmed while it rolled back.
"""
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event, text

from business_app.models.payment import CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.exceptions import ValidationError
from shared.enums import CashCollectionSource, OrderStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


def _seed_credit(db, user, amount, admin):
    """Park `amount` as unapplied credit on `user` via an over-collection."""
    order, _ = delivered_cod_order(db, user, total=Decimal("1000.00"))
    return CashCollectionService().post_collection(
        customer_id=user.id,
        amount=Decimal("1000.00") + amount,
        source="standalone_meeting",
        order_id=order.id,
        recorded_by_user_id=admin.id,
        notes="over-collection",
    )


def _outstanding(db, payment):
    db.session.refresh(payment)
    return Decimal(str(payment.outstanding_amount))


def _reserved(db, payment):
    db.session.refresh(payment)
    return Decimal(str((payment.provider_data or {}).get("cod_prepayment_reserved_amount", 0)))


def _assert_conserved(db, event):
    """SUM(live allocations) + unapplied == event.amount (global constraint)."""
    db.session.refresh(event)
    allocated = sum(
        (Decimal(str(a.allocated_amount)) for a in event.allocations if a.reversed_at is None),
        Decimal("0.00"),
    )
    assert allocated + Decimal(str(event.unapplied_amount)) == Decimal(str(event.amount))


@pytest.mark.unit
class TestClusterPrepaidCredit:
    def test_cluster_balance_sums_all_members(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        _seed_credit(db, u1, Decimal("3000.00"), admin)
        _seed_credit(db, u2, Decimal("2000.00"), admin)
        svc = CashCollectionService()
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("3000.00")
        link_users(db, [u1, u2])
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("5000.00")
        assert svc.get_customer_prepaid_balance(u2.id) == Decimal("5000.00")

    def test_sibling_credit_applies_to_other_members_debt(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        event = _seed_credit(db, u1, Decimal("5000.00"), admin)
        _, payment = delivered_cod_order(db, u2, total=Decimal("4000.00"))
        svc = CashCollectionService()
        svc.apply_customer_prepaid_credit_to_payment(payment)
        db.session.refresh(payment)
        assert Decimal(str(payment.outstanding_amount)) == Decimal("0.00")
        assert svc.get_customer_prepaid_balance(u2.id) == Decimal("1000.00")
        # Conservation survives a cross-account application, and the dual audit
        # stamps record who paid for whom.
        _assert_conserved(db, event)
        cross = [a for a in event.allocations if a.beneficiary_user_id == u2.id]
        assert [Decimal(str(a.allocated_amount)) for a in cross] == [Decimal("4000.00")]
        assert {a.source_customer_id for a in cross} == {u1.id}

    def test_sweep_reserves_against_sibling_pending_orders(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _, pending_payment = delivered_cod_order(
            db, u2, total=Decimal("4000.00"), status=OrderStatus.CONFIRMED
        )
        _seed_credit(db, u1, Decimal("5000.00"), admin)
        db.session.refresh(pending_payment)
        reserved = (pending_payment.provider_data or {}).get(
            "cod_prepayment_reserved_amount", 0
        )
        assert Decimal(str(reserved)) == Decimal("4000.00")

    def test_place_group_alone_never_pools_credit(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        _seed_credit(db, u1, Decimal("5000.00"), admin)
        svc = CashCollectionService()
        # Coworkers are NOT a cluster: credit stays personal.
        assert svc.get_customer_prepaid_balance(u2.id) == Decimal("0.00")

    def test_pct_preview_spill_covers_cluster(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        target_order, _ = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        _, sibling_payment = delivered_cod_order(db, u2, total=Decimal("3000.00"))
        plan = CashCollectionService().preview_personal_card_transfer(
            order_id=target_order.id, amount=Decimal("8000.00")
        )
        assert plan.applied_to_order == Decimal("5000.00")
        assert plan.applied_to_other_debts == Decimal("3000.00")
        assert plan.remaining_as_credit == Decimal("0.00")
        assert plan.spill_allocations[0]["order_id"] == sibling_payment.order_id

    def test_prepayment_history_covers_cluster(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _seed_credit(db, u1, Decimal("3000.00"), admin)
        payload = CashCollectionService().get_customer_prepayment_history(u2.id)
        assert payload["available_prepayment_balance"] == 3000.0
        assert sorted(payload["cluster_member_ids"]) == sorted([u1.id, u2.id])
        assert any(e["customer_id"] == u1.id for e in payload["events"])

    def test_prepayment_list_collapses_cluster_to_one_row(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _seed_credit(db, u1, Decimal("3000.00"), admin)
        _seed_credit(db, u2, Decimal("2000.00"), admin)
        items = CashCollectionService().list_customers_with_prepayment_balance()
        cluster_rows = [
            i for i in items if set(i.get("member_user_ids", [])) == {u1.id, u2.id}
        ]
        assert len(cluster_rows) == 1
        assert cluster_rows[0]["available_prepayment_balance"] == 5000.0


@pytest.mark.unit
class TestCreditIsClusterOnlyNeverPlacePooled:
    """Hard invariant: a place group shares bottles and the place's COD debt —
    never its members' credit."""

    def test_place_scoped_collection_cannot_reach_a_coworkers_credit(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)

        # The coworker u1 parks 5000 of credit and has a pending order at the place.
        _seed_credit(db, u1, Decimal("5000.00"), admin)
        _, coworker_pending = delivered_cod_order(
            db, u1, address=a1, total=Decimal("7000.00"), status=OrderStatus.CONFIRMED
        )
        # u2 owes 4000 delivered to the shared place.
        debt_order, debt_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("4000.00")
        )

        svc = CashCollectionService()

        # 1. Direct credit application on the coworker's debt sees nothing.
        svc.apply_customer_prepaid_credit_to_payment(debt_payment)
        assert _outstanding(db, debt_payment) == Decimal("4000.00")
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("5000.00")

        # 2. A PLACE-scoped collection settles the place's debt from its OWN
        #    cash only; the residual stays the poster's credit and the ring-3
        #    sweep (cluster-only) never reserves against a coworker's order.
        event = svc.post_collection(
            customer_id=u2.id,
            amount=Decimal("6000.00"),
            source="standalone_meeting",
            order_id=debt_order.id,
            recorded_by_user_id=admin.id,
            notes="cash handed over at the office door",
        )
        assert event.scope_type == "place"
        assert _outstanding(db, debt_payment) == Decimal("0.00")
        assert Decimal(str(event.unapplied_amount)) == Decimal("2000.00")
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("5000.00")
        assert svc.get_customer_prepaid_balance(u2.id) == Decimal("2000.00")
        assert _reserved(db, coworker_pending) == Decimal("0.00")
        _assert_conserved(db, event)

    def test_reservation_sweep_never_reaches_a_coworkers_pending_order(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        _, coworker_pending = delivered_cod_order(
            db, u1, address=a1, total=Decimal("9000.00"), status=OrderStatus.CONFIRMED
        )
        _seed_credit(db, u2, Decimal("3000.00"), admin)
        assert _reserved(db, coworker_pending) == Decimal("0.00")
        assert CashCollectionService().get_customer_prepaid_balance(u2.id) == Decimal("3000.00")


@pytest.mark.unit
class TestConservationAndGuards:
    def test_both_over_allocation_guards_still_raise(self, db):
        u1, admin = make_user(db), make_user(db)
        _, payment = delivered_cod_order(db, u1, total=Decimal("1000.00"))
        svc = CashCollectionService()
        event = CashCollectionEvent(
            customer_id=u1.id,
            recorded_by_user_id=admin.id,
            amount=Decimal("100.00"),
            currency="UZS",
            source=CashCollectionSource.ADMIN_ADJUSTMENT,
            occurred_at=datetime.now(UTC),
            unapplied_amount=Decimal("100.00"),
            notes="guard probe",
        )
        db.session.add(event)
        db.session.commit()

        with pytest.raises(ValidationError, match="exceeds unapplied event balance"):
            svc._allocate_to_payment(
                event=event,
                payment=payment,
                amount=Decimal("200.00"),
                allocation_order=1,
                allocation_mode="manual",
            )

        event.unapplied_amount = Decimal("5000.00")
        event.amount = Decimal("5000.00")
        db.session.commit()
        with pytest.raises(ValidationError, match="exceeds payment outstanding balance"):
            svc._allocate_to_payment(
                event=event,
                payment=payment,
                amount=Decimal("2000.00"),
                allocation_order=1,
                allocation_mode="manual",
            )


@pytest.mark.unit
class TestUnifiedIdOrderedLocking:
    """Directives 1 & 2: every payments lock is one id-ordered batch, and the
    PERSONAL_CARD_TRANSFER target is inside that batch rather than pre-locked."""

    @staticmethod
    def _spy(monkeypatch, calls):
        original_lock = CashCollectionService._lock_payments_by_ids
        original_alloc = CashCollectionService._allocate_to_payment

        def lock_spy(self, payment_ids):
            ids = sorted(int(pid) for pid in payment_ids)
            calls.append(("lock", ids))
            return original_lock(self, payment_ids)

        def alloc_spy(self, **kwargs):
            calls.append(("allocate", kwargs["payment"].id))
            return original_alloc(self, **kwargs)

        monkeypatch.setattr(CashCollectionService, "_lock_payments_by_ids", lock_spy)
        monkeypatch.setattr(CashCollectionService, "_allocate_to_payment", alloc_spy)

    def test_batch_lock_orders_by_payment_id(self, db):
        u1 = make_user(db)
        _, p1 = delivered_cod_order(db, u1, total=Decimal("1000.00"))
        _, p2 = delivered_cod_order(db, u1, total=Decimal("2000.00"))
        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        p1_id, p2_id = p1.id, p2.id  # touch before recording (avoids refresh noise)
        sa_event.listen(db.engine, "before_cursor_execute", _record)
        try:
            CashCollectionService()._lock_payments_by_ids([p2_id, p1_id])
        finally:
            sa_event.remove(db.engine, "before_cursor_execute", _record)

        # ONE batch select over the whole id set, ordered by payments.id — never
        # a per-row walk and never another ordering.
        batch = [s for s in statements if "FROM payments" in s and "payments.id IN" in s]
        assert len(batch) == 1, statements
        assert "ORDER BY payments.id ASC" in batch[0]
        assert not [s for s in statements if "FROM payments" in s and "payments.id IN" not in s], statements

    def test_payment_batch_lock_refreshes_identity_mapped_rows(self, db):
        """The payments lock must REFRESH what it returns (``populate_existing``).

        Unlike the events lock, this query cannot carry business predicates — the
        batch deliberately includes the current order's payment, which need not be
        DELIVERED — so ``FOR UPDATE`` re-qualification cannot drop a row that
        stopped qualifying while we were blocked. The only remaining protection is
        that the locked rows carry LIVE values, and a locking ``SELECT`` does not
        refresh column attributes of a row already in the session identity map:
        the identity map wins and the fetched values are discarded.

        Without the refresh this is a lost update, not a cosmetic staleness: a
        staff delivery loads ``delivery.order.payment`` at outstanding=3000, a
        concurrent collection settles it in full and commits, and on unblocking
        ``live_outstanding``, the candidate filter and ``_allocate_to_payment``'s
        over-allocation guard all read the SAME stale 3000 and all agree — then
        ``amount_collected = stale(0) + 3000`` clobbers the committed write.

        The concurrent committed write is simulated with direct SQL (the ORM
        instance keeps its stale attributes, exactly as it would across a real
        ``FOR UPDATE`` block).
        """
        u1 = make_user(db)
        _, payment = delivered_cod_order(db, u1, total=Decimal("3000.00"))
        payment_id = payment.id
        # Load the row's values into the identity map, then leave them stale.
        assert Decimal(str(payment.outstanding_amount)) == Decimal("3000.00")
        assert Decimal(str(payment.amount_collected)) == Decimal("0.00")

        db.session.execute(
            text(
                "UPDATE payments SET amount_collected = 3000, outstanding_amount = 0 "
                "WHERE id = :pid"
            ),
            {"pid": payment_id},
        )

        locked = CashCollectionService()._lock_payments_by_ids([payment_id])
        # Same instance (identity map), refreshed values — that is the whole point.
        assert locked[payment_id] is payment
        assert Decimal(str(locked[payment_id].outstanding_amount)) == Decimal("0.00")
        assert Decimal(str(locked[payment_id].amount_collected)) == Decimal("3000.00")

    def test_credit_event_batch_lock_refreshes_identity_mapped_rows(self, db):
        """The events lock must ALSO refresh (``populate_existing``).

        Its SQL predicates correctly DROP a row that stopped qualifying, but a row
        that still qualifies (``voided_at IS NULL``, ``unapplied_amount > 0``) can
        come back with a stale-HIGH ``unapplied_amount`` — the identity map keeps
        the pre-block value — and then slip past ``_allocate_to_payment``'s
        "exceeds unapplied event balance" guard, spending credit a committed
        concurrent allocation already consumed.
        """
        u1, admin = make_user(db), make_user(db)
        event = _seed_credit(db, u1, Decimal("5000.00"), admin)
        event_id = event.id
        assert Decimal(str(event.unapplied_amount)) == Decimal("5000.00")

        # A concurrent transaction spends 4000 of the credit and commits. The row
        # still qualifies, so the predicates cannot save us here.
        db.session.execute(
            text("UPDATE cash_collection_events SET unapplied_amount = 1000 WHERE id = :eid"),
            {"eid": event_id},
        )

        locked = CashCollectionService()._lock_credit_events_by_ids([event_id])
        assert locked[event_id] is event
        assert Decimal(str(locked[event_id].unapplied_amount)) == Decimal("1000.00")

    def test_personal_path_locks_the_whole_candidate_set_in_one_batch(self, db, monkeypatch):
        u1, admin = make_user(db), make_user(db)
        t0 = datetime.now(UTC) - timedelta(days=3)
        _older_order, older_payment = delivered_cod_order(
            db, u1, total=Decimal("2000.00"), created_at=t0
        )
        newer_order, newer_payment = delivered_cod_order(
            db, u1, total=Decimal("3000.00"), created_at=t0 + timedelta(days=1)
        )
        calls = []
        self._spy(monkeypatch, calls)

        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("5000.00"),
            source="standalone_meeting",
            order_id=newer_order.id,
            recorded_by_user_id=admin.id,
            notes="personal cash",
        )

        locks = [ids for kind, ids in calls if kind == "lock"]
        assert locks, calls
        # The personal path must acquire BOTH debts in a single batch — the same
        # helper (and therefore the same id order) the cluster/place path uses.
        assert {older_payment.id, newer_payment.id} <= set(locks[0])
        # Oldest-first allocation ordering still applies, in memory.
        allocated = [pid for kind, pid in calls if kind == "allocate"]
        assert allocated == [older_payment.id, newer_payment.id]

    def test_cluster_path_uses_the_same_batch_locker(self, db, monkeypatch):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=3)
        _sib_order, sibling_payment = delivered_cod_order(
            db, u2, total=Decimal("2000.00"), created_at=t0
        )
        own_order, own_payment = delivered_cod_order(
            db, u1, total=Decimal("3000.00"), created_at=t0 + timedelta(days=1)
        )
        calls = []
        self._spy(monkeypatch, calls)

        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("5000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="cluster cash",
        )

        locks = [ids for kind, ids in calls if kind == "lock"]
        assert locks, calls
        assert {sibling_payment.id, own_payment.id} <= set(locks[0])

    def test_pct_locks_the_full_batch_before_the_target_first_allocation(self, db, monkeypatch):
        u1, admin = make_user(db), make_user(db)
        t0 = datetime.now(UTC) - timedelta(days=3)
        _other_order, other_payment = delivered_cod_order(
            db, u1, total=Decimal("3000.00"), created_at=t0
        )
        target_order, target_payment = delivered_cod_order(
            db, u1, total=Decimal("5000.00"), created_at=t0 + timedelta(days=1)
        )
        calls = []
        self._spy(monkeypatch, calls)

        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("9000.00"),
            source="personal_card_transfer",
            order_id=target_order.id,
            recorded_by_user_id=admin.id,
            notes="customer transferred to the owner's card",
        )

        assert calls[0][0] == "lock", calls
        # The target is acquired inside the SAME id-ordered batch as the spill
        # candidates — never pre-locked out of order ahead of them.
        assert {target_payment.id, other_payment.id} <= set(calls[0][1])
        first_allocation = next(i for i, (kind, _) in enumerate(calls) if kind == "allocate")
        assert first_allocation > 0
        assert calls[first_allocation][1] == target_payment.id

    def test_pct_that_only_settles_its_target_still_batch_locks_first(self, db, monkeypatch):
        """`_allocate_scoped` never runs here, so the batch lock is the ONLY
        thing standing between the target-first allocation and a lost update."""
        u1, admin = make_user(db), make_user(db)
        target_order, target_payment = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        calls = []
        self._spy(monkeypatch, calls)

        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("5000.00"),
            source="personal_card_transfer",
            order_id=target_order.id,
            recorded_by_user_id=admin.id,
            notes="exact card transfer",
        )

        assert calls[0][0] == "lock", calls
        assert target_payment.id in calls[0][1]
        assert [pid for kind, pid in calls if kind == "allocate"] == [target_payment.id]

    def test_credit_events_are_locked_in_one_id_ordered_batch(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _seed_credit(db, u1, Decimal("3000.00"), admin)
        _seed_credit(db, u2, Decimal("2000.00"), admin)
        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        sa_event.listen(db.engine, "before_cursor_execute", _record)
        try:
            events = CashCollectionService()._locked_cluster_credit_events(u2.id)
        finally:
            sa_event.remove(db.engine, "before_cursor_execute", _record)

        # Both members' credit events are visible...
        assert {e.customer_id for e in events} == {u1.id, u2.id}
        # ...consumption order is oldest-event-first (derived in SQL, never a
        # Python sort over a tz-mixed DateTime column)...
        assert [e.customer_id for e in events] == [u1.id, u2.id]
        # ...and every locking read of the table is ordered by id ASC.
        locking = [
            s
            for s in statements
            if "FROM cash_collection_events" in s and "ORDER BY cash_collection_events.id" in s
        ]
        assert locking, statements

    def test_credit_event_lock_query_carries_the_business_predicates(self, db):
        """The predicates must live on the LOCKING query, not only on phase 1.

        Under READ COMMITTED a ``FOR UPDATE`` that blocks on a row another
        transaction is updating re-evaluates *its own* WHERE against the newly
        committed row version and drops the row if it no longer qualifies. That
        re-qualification is the concurrency guard: locking by id ALONE always
        re-qualifies, so the blocked reader unblocks holding a just-voided event
        whose credit ``reverse_collection_event`` has restored — and neither
        consuming loop re-checks ``voided_at``. A true cross-transaction race is
        not reproducible on SQLite, so pin the emitted SQL.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _seed_credit(db, u1, Decimal("3000.00"), admin)
        _seed_credit(db, u2, Decimal("2000.00"), admin)
        statements = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        sa_event.listen(db.engine, "before_cursor_execute", _record)
        try:
            CashCollectionService()._locked_cluster_credit_events(u2.id)
        finally:
            sa_event.remove(db.engine, "before_cursor_execute", _record)

        locking = [
            s
            for s in statements
            if "FROM cash_collection_events" in s and "ORDER BY cash_collection_events.id" in s
        ]
        assert len(locking) == 1, statements
        assert "cash_collection_events.voided_at IS NULL" in locking[0], locking[0]
        assert "cash_collection_events.unapplied_amount >" in locking[0], locking[0]

    def test_locking_query_drops_an_event_voided_after_candidate_selection(self, db):
        """Behavioural mirror of the race: the phase-1 id set was resolved while
        the event still qualified; by the time the locking query runs it is
        voided, and its restored credit must NOT come back with it."""
        u1, admin = make_user(db), make_user(db)
        event = _seed_credit(db, u1, Decimal("5000.00"), admin)
        candidate_ids = [event.id]  # phase-1 snapshot, taken while it qualified

        CashCollectionService().reverse_collection_event(
            event.id, reversed_by_user_id=admin.id, reason="admin voided the collection"
        )
        db.session.refresh(event)
        # The void restores the full credit onto the row — which is exactly why
        # an id-only locking query would hand it back as spendable.
        assert Decimal(str(event.unapplied_amount)) == Decimal(str(event.amount))

        assert CashCollectionService()._lock_credit_events_by_ids(candidate_ids) == {}

    def test_voided_event_credit_is_never_consumed_when_the_void_lands_mid_flight(
        self, db, monkeypatch
    ):
        """End-to-end: no allocation path may spend a voided event's credit, even
        when the void lands AFTER phase 1 selected the event as a candidate.

        Voiding up front does not discriminate: phase 1's own predicates already
        drop the row, so such a test passes even with the locking query's
        predicates deleted. The void must land BETWEEN candidate selection and the
        locking query — the window ``FOR UPDATE`` re-qualification actually guards,
        and the one where ``reverse_collection_event``'s
        ``unapplied_amount = event.amount`` restore would otherwise hand the credit
        back as spendable (neither consuming loop re-checks ``voided_at``).
        """
        u1, admin = make_user(db), make_user(db)
        event = _seed_credit(db, u1, Decimal("5000.00"), admin)
        event_id = event.id
        _, payment = delivered_cod_order(db, u1, total=Decimal("4000.00"))
        svc = CashCollectionService()
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("5000.00")

        original_lock = CashCollectionService._lock_credit_events_by_ids
        state = {"voided": False}

        def void_then_lock(self, candidate_ids, *, must_hold_event_ids=()):
            candidate_ids = list(candidate_ids)
            if not state["voided"]:
                state["voided"] = True
                # Phase 1 picked it up while it still qualified...
                assert event_id in candidate_ids, candidate_ids
                # ...and the admin's void commits before our locking query runs.
                CashCollectionService().reverse_collection_event(
                    event_id,
                    reversed_by_user_id=admin.id,
                    reason="admin voided the collection",
                )
            return original_lock(self, candidate_ids, must_hold_event_ids=must_hold_event_ids)

        monkeypatch.setattr(CashCollectionService, "_lock_credit_events_by_ids", void_then_lock)
        svc.apply_customer_prepaid_credit_to_payment(payment)
        monkeypatch.undo()

        assert state["voided"], "the locking helper was never reached"
        assert _outstanding(db, payment) == Decimal("4000.00")
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("0.00")
        assert svc._locked_cluster_credit_events(u1.id) == []
        assert svc.reserve_customer_prepaid_credit_for_payment(payment) == Decimal("0.00")

    def test_adjust_event_amount_batch_locks_the_cluster_before_voiding(self, db, monkeypatch):
        """``adjust_event_amount`` must take the cluster's id-ordered event batch
        — the target INCLUDED — before it voids anything.

        A bare single-row pre-lock is not made safe by voiding first: that only
        stops OUR transaction re-requesting the row. A concurrent post cannot see
        the uncommitted void, so its batch still contains and blocks on it —
        T1 holds E5 and wants {E3}; T2 holds E3 and blocks on E5.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        # Sibling credit event first, so it carries the LOWER id — the target
        # must not be grabbed ahead of it.
        sibling_event = _seed_credit(db, u2, Decimal("2000.00"), admin)
        # A fully-allocated target: the spendable arm cannot contain it, so only
        # the explicit must-hold arm puts it in the same id-ordered batch.
        target_order, _ = delivered_cod_order(db, u1, total=Decimal("1000.00"))
        target_event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("1000.00"),
            source="standalone_meeting",
            order_id=target_order.id,
            recorded_by_user_id=admin.id,
            notes="exact cash",
        )
        assert Decimal(str(target_event.unapplied_amount)) == Decimal("0.00")
        assert sibling_event.id < target_event.id

        calls = []
        original_lock = CashCollectionService._lock_credit_events_by_ids
        original_reverse = CashCollectionService.reverse_collection_event

        def lock_spy(self, candidate_ids, *, must_hold_event_ids=()):
            ids = {int(i) for i in candidate_ids} | {int(i) for i in must_hold_event_ids}
            calls.append(("lock", sorted(ids)))
            return original_lock(self, candidate_ids, must_hold_event_ids=must_hold_event_ids)

        def reverse_spy(self, event_id, **kwargs):
            calls.append(("void", int(event_id)))
            return original_reverse(self, event_id, **kwargs)

        monkeypatch.setattr(CashCollectionService, "_lock_credit_events_by_ids", lock_spy)
        monkeypatch.setattr(CashCollectionService, "reverse_collection_event", reverse_spy)

        CashCollectionService().adjust_event_amount(
            target_event.id,
            new_amount=Decimal("500.00"),
            adjusted_by_user_id=admin.id,
            reason="driver miscounted",
            commit=False,
        )

        assert calls[0][0] == "lock", calls
        assert {sibling_event.id, target_event.id} <= set(calls[0][1]), calls
        void_index = next(i for i, (kind, _) in enumerate(calls) if kind == "void")
        assert void_index > 0, calls


@pytest.mark.unit
class TestGroceryCreditNeverPools:
    """Spec 5.8 layer 3, third fence: the credit pool itself.

    ``resolve_allocation_scope`` and ``post_collection`` both force a grocery
    customer to personal scope; the credit primitives must do the same or an
    already-linked individual converted to a grocery entity would start pooling
    contract-mirrored money with a personal wallet.
    """

    def test_grocery_customer_cannot_reach_a_linked_siblings_credit(self, db):
        grocery, sibling, admin = make_user(db, grocery=True), make_user(db), make_user(db)
        link_users(db, [grocery, sibling])
        _seed_credit(db, sibling, Decimal("5000.00"), admin)
        svc = CashCollectionService()

        assert svc.get_customer_prepaid_balance(grocery.id) == Decimal("0.00")
        assert svc._locked_cluster_credit_events(grocery.id) == []

        _, grocery_payment = delivered_cod_order(db, grocery, total=Decimal("4000.00"))
        svc.apply_customer_prepaid_credit_to_payment(grocery_payment)
        assert _outstanding(db, grocery_payment) == Decimal("4000.00")
        # The sibling's wallet is untouched.
        assert svc.get_customer_prepaid_balance(sibling.id) == Decimal("5000.00")

    def test_individual_cannot_reach_a_linked_grocery_siblings_credit(self, db):
        grocery, sibling, admin = make_user(db, grocery=True), make_user(db), make_user(db)
        link_users(db, [grocery, sibling])
        _seed_credit(db, grocery, Decimal("5000.00"), admin)
        svc = CashCollectionService()

        assert svc.get_customer_prepaid_balance(sibling.id) == Decimal("0.00")
        assert svc._locked_cluster_credit_events(sibling.id) == []

        _, sibling_payment = delivered_cod_order(db, sibling, total=Decimal("4000.00"))
        svc.apply_customer_prepaid_credit_to_payment(sibling_payment)
        assert _outstanding(db, sibling_payment) == Decimal("4000.00")
        assert svc.get_customer_prepaid_balance(grocery.id) == Decimal("5000.00")

    def test_prepayment_history_ledger_matches_its_own_balance_field(self, db):
        """The history payload must describe ONE pool.

        ``available_prepayment_balance`` is ``get_customer_prepaid_balance``, which
        is grocery-guarded. Resolving the ledger from the raw cluster instead would
        list a grocery member's contract-mirrored events (and sum them into the
        lifetime aggregates) beside a balance that deliberately excludes them —
        an admin reading 5000 of collected cash under a 0 balance.
        """
        grocery, sibling, admin = make_user(db, grocery=True), make_user(db), make_user(db)
        link_users(db, [grocery, sibling])
        _seed_credit(db, grocery, Decimal("5000.00"), admin)
        _seed_credit(db, sibling, Decimal("2000.00"), admin)

        payload = CashCollectionService().get_customer_prepayment_history(sibling.id)

        assert payload["available_prepayment_balance"] == 2000.0
        assert grocery.id not in payload["cluster_member_ids"]
        assert [e["customer_id"] for e in payload["events"]] == [sibling.id]
        # Lifetime totals come from the same pool: only the sibling's 1000-order
        # over-collection of 3000, never the grocery member's.
        assert payload["lifetime_collected"] == 3000.0
        assert payload["lifetime_applied"] == 1000.0


@pytest.mark.unit
class TestSweepGateMatchesTheSpentPool:
    """Directive 4: ``auto_reserve_against_pending_payments`` reserves the PAYMENT
    OWNER's cluster credit, so its gates must be evaluated on that same pool —
    not on the poster's."""

    def test_gate_does_not_early_return_when_only_another_owner_has_credit(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        _seed_credit(db, u2, Decimal("5000.00"), admin)
        _, pending_payment = delivered_cod_order(
            db, u2, total=Decimal("4000.00"), status=OrderStatus.CONFIRMED
        )
        reserved = CashCollectionService().auto_reserve_against_pending_payments(
            u1.id, cluster_user_ids=[u1.id, u2.id]
        )
        assert reserved == Decimal("4000.00")
        assert _reserved(db, pending_payment) == Decimal("4000.00")

    def test_exhausted_owner_does_not_stop_the_sweep(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        t0 = datetime.now(UTC) - timedelta(days=3)
        _seed_credit(db, u1, Decimal("1000.00"), admin)
        _seed_credit(db, u2, Decimal("5000.00"), admin)
        _, first_pending = delivered_cod_order(
            db, u1, total=Decimal("1000.00"), status=OrderStatus.CONFIRMED, created_at=t0
        )
        _, second_pending = delivered_cod_order(
            db,
            u2,
            total=Decimal("4000.00"),
            status=OrderStatus.CONFIRMED,
            created_at=t0 + timedelta(days=1),
        )
        # Posted by u1, whose 1000 is fully consumed by their OWN pending order
        # first. The old gate re-read the POSTER's balance each iteration and
        # broke out at 0 — stranding u2's pending order even though u2's own
        # 5000 was sitting right there.
        reserved = CashCollectionService().auto_reserve_against_pending_payments(
            u1.id, cluster_user_ids=[u1.id, u2.id]
        )
        assert _reserved(db, first_pending) == Decimal("1000.00")
        assert _reserved(db, second_pending) == Decimal("4000.00")
        assert reserved == Decimal("5000.00")
