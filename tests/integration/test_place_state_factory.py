"""THE FACTORY'S OWN TEST. Every other stream trusts this file.

``place_state_factory`` is the answer to "the fixtures agreed by construction",
so it is the one piece of test infrastructure whose own correctness cannot be
taken on faith. If a scenario declares Alice is worth 45 000 and the rows it
wrote say 25 000, every test built on it is asserting fiction — confidently.

WHAT IS PROVED HERE, AND HOW IT CANNOT PASS VACUOUSLY
-----------------------------------------------------
Every expectation is re-derived from the DATABASE with plain SQLAlchemy — never
by calling ``CashCollectionService``, ``StaffService`` or ``collectible_cod_total``.
Three independent derivations of the same number therefore have to agree:

    the SPEC (what a preset declares)
      → the ORACLE (arithmetic over the spec, in the factory)
        → the ROWS (SQL over what was actually written, in this file)

A bug in the factory's writers breaks spec→rows. A bug in its oracle breaks
oracle→rows. Only a bug present in BOTH, identically, could hide — and they are
written in different languages (Python comprehensions vs SQL aggregates) over
different inputs (dataclasses vs tables).

``TestTheOracleIsFalsifiable`` is the guard on the guard: it corrupts a written
row and asserts the cross-check goes red. If that test ever passes while the
row is broken, the cross-checks below are blind and their green is worthless.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func

from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup, CanonicalCustomer
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, Payment
from business_app.models.user import User, UserAddress
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
from tests.integration.place_state_factory import (
    SCENARIOS,
    AddressSpec,
    BottleSpec,
    DebtSpec,
    PersonSpec,
    PlaceSpec,
    ScenarioNamespaceCollision,
    ScenarioSpec,
    build,
    build_scenario,
)

pytestmark = pytest.mark.integration

ALL_SCENARIOS = sorted(SCENARIOS)


# --------------------------------------------------------------------------- #
# ROW-LEVEL RE-DERIVATION — SQL only. No service layer is imported in this file.
# --------------------------------------------------------------------------- #


def _open_delivered_cod_for_users(db, user_ids):
    """Σ / count of open DELIVERED cash debt owned by these accounts.

    Never ``join(User)`` — ``payments`` has two FKs to ``users`` and an
    unqualified join silently picks the wrong one.
    """
    if not user_ids:
        return Decimal("0.00"), 0
    row = (
        db.session.query(
            func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")),
            func.count(Payment.id),
        )
        .select_from(Payment)
        .join(Order, Order.id == Payment.order_id)
        .filter(
            Payment.user_id.in_(list(user_ids)),
            Payment.payment_method == PaymentMethod.CASH,
            Payment.outstanding_amount > 0,
            Order.status == OrderStatus.DELIVERED,
        )
        .one()
    )
    return Decimal(str(row[0])).quantize(Decimal("0.01")), int(row[1])


def _open_delivered_cod_at_addresses(db, address_ids, *, excluding_owners=()):
    """Σ / count of open DELIVERED cash debt delivered to these addresses."""
    if not address_ids:
        return Decimal("0.00"), 0
    query = (
        db.session.query(
            func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")),
            func.count(Payment.id),
        )
        .select_from(Payment)
        .join(Order, Order.id == Payment.order_id)
        .filter(
            Order.delivery_address_id.in_(list(address_ids)),
            Payment.payment_method == PaymentMethod.CASH,
            Payment.outstanding_amount > 0,
            Order.status == OrderStatus.DELIVERED,
        )
    )
    if excluding_owners:
        query = query.filter(~Payment.user_id.in_(list(excluding_owners)))
    row = query.one()
    return Decimal(str(row[0])).quantize(Decimal("0.01")), int(row[1])


def _cluster_ids_from_rows(db, user_id):
    """The cluster, read back from ``users.canonical_customer_id``."""
    canonical = db.session.query(User.canonical_customer_id).filter(User.id == user_id).scalar()
    if canonical is None:
        return [user_id]
    return sorted(
        r[0] for r in db.session.query(User.id).filter(User.canonical_customer_id == canonical).all()
    )


def _place_address_ids_from_rows(db, group_id):
    return sorted(
        r[0] for r in db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group_id).all()
    )


def _unapplied_credit_from_rows(db, user_ids):
    if not user_ids:
        return Decimal("0.00")
    total = db.session.query(
        func.coalesce(func.sum(CashCollectionEvent.unapplied_amount), Decimal("0.00"))
    ).filter(
        CashCollectionEvent.customer_id.in_(list(user_ids)),
        CashCollectionEvent.voided_at.is_(None),
        CashCollectionEvent.unapplied_amount > 0,
    ).scalar()
    return Decimal(str(total or 0)).quantize(Decimal("0.01"))


def _assert_person_matches_rows(db, scenario, person_key):
    """Every declared money figure, re-derived from the tables."""
    expected = scenario.expect(person_key)

    cluster_ids = _cluster_ids_from_rows(db, expected.user_id)
    assert tuple(cluster_ids) == expected.cluster_user_ids, (
        f"{scenario.name}/{person_key}: declared cluster {expected.cluster_user_ids} "
        f"but users.canonical_customer_id says {tuple(cluster_ids)}"
    )

    account_sum, account_count = _open_delivered_cod_for_users(db, [expected.user_id])
    assert account_sum == expected.account_delivered_outstanding
    assert account_count == expected.account_delivered_debt_count

    cluster_sum, cluster_count = _open_delivered_cod_for_users(db, cluster_ids)
    assert cluster_sum == expected.cluster_delivered_outstanding, (
        f"{scenario.name}/{person_key}: declared own debt "
        f"{expected.cluster_delivered_outstanding}, rows say {cluster_sum}"
    )
    assert cluster_count == expected.cluster_delivered_debt_count

    # The per-account non-terminal headline — the figure that inflates with a
    # PENDING order and settles nothing.
    headline = (
        db.session.query(func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")))
        .select_from(Payment)
        .join(Order, Order.id == Payment.order_id)
        .filter(
            Payment.user_id == expected.user_id,
            Payment.payment_method == PaymentMethod.CASH,
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.RETURNED]),
        )
        .scalar()
    )
    assert Decimal(str(headline or 0)).quantize(Decimal("0.01")) == expected.account_outstanding_amount

    # The foreign half, deduped across the cluster's places exactly as the
    # ring-1 ∪ ring-2 definition requires.
    place_address_ids = sorted(
        {aid for gid in expected.place_group_ids for aid in _place_address_ids_from_rows(db, gid)}
    )
    foreign_sum, foreign_count = _open_delivered_cod_at_addresses(
        db, place_address_ids, excluding_owners=cluster_ids
    )
    assert foreign_sum == expected.foreign_place_outstanding, (
        f"{scenario.name}/{person_key}: declared coworkers' debt "
        f"{expected.foreign_place_outstanding}, rows say {foreign_sum}"
    )
    assert foreign_count == expected.foreign_place_debt_count

    assert expected.collectible_total == cluster_sum + foreign_sum
    assert expected.collectible_debt_count == cluster_count + foreign_count

    # The wallet: pooled across a cluster, never across a place, and never
    # shared with a grocery sibling (contract-mirrored cash cannot co-mingle).
    by_uid = {e.user_id: e for e in scenario.people.values()}
    credit_ids = (
        [expected.user_id]
        if expected.is_grocery
        else [uid for uid in cluster_ids if not by_uid[uid].is_grocery]
    )
    assert _unapplied_credit_from_rows(db, credit_ids) == expected.prepaid_credit, (
        f"{scenario.name}/{person_key}: declared credit {expected.prepaid_credit}, "
        f"rows say {_unapplied_credit_from_rows(db, credit_ids)}"
    )

    # The grouped addresses the cluster owns, from the rows.
    grouped = sorted(
        r[0]
        for r in db.session.query(UserAddress.id)
        .filter(UserAddress.user_id.in_(cluster_ids), UserAddress.address_group_id.isnot(None))
        .all()
    )
    assert tuple(grouped) == expected.grouped_address_ids


def _assert_place_matches_rows(db, scenario, place_key):
    expected = scenario.place_expect(place_key)
    group = db.session.get(AddressGroup, expected.group_id)
    assert group is not None
    assert group.label == expected.label
    assert group.dissolved_onto_address_id == expected.dissolved_onto_address_id
    assert (group.dissolved_onto_address_id is not None) is expected.is_dissolved

    address_ids = _place_address_ids_from_rows(db, expected.group_id)
    assert tuple(address_ids) == expected.address_ids

    owners = sorted(
        {
            r[0]
            for r in db.session.query(UserAddress.user_id)
            .filter(UserAddress.address_group_id == expected.group_id)
            .all()
        }
    )
    assert tuple(owners) == expected.member_user_ids
    assert len(owners) == expected.member_count

    total, count = _open_delivered_cod_at_addresses(db, address_ids)
    assert total == expected.open_cod_total, (
        f"{scenario.name}/{place_key}: declared place debt {expected.open_cod_total}, "
        f"rows say {total}"
    )
    assert count == expected.open_cod_debt_count

    balance_row = (
        db.session.query(BottleBalance)
        .filter(BottleBalance.address_group_id == expected.group_id)
        .one_or_none()
    )
    written = Decimal("0.00") if balance_row is None else Decimal(str(balance_row.balance))
    assert written.quantize(Decimal("0.01")) == expected.bottle_balance

    ledger_sum = db.session.query(
        func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00"))
    ).filter(BottleLedger.address_group_id == expected.group_id).scalar()
    assert Decimal(str(ledger_sum or 0)).quantize(Decimal("0.01")) == expected.bottle_balance, (
        f"{scenario.name}/{place_key}: the materialised balance and its own ledger disagree"
    )


# --------------------------------------------------------------------------- #
# 1. EVERY SCENARIO BUILDS, AND ITS DECLARED MONEY IS THE MONEY ON DISK
# --------------------------------------------------------------------------- #


class TestEveryScenarioBuilds:
    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_scenario_builds_real_rows(self, db, name):
        scenario = build(db, name)

        assert len(scenario.users) == len(scenario.spec.people)
        for key, user in scenario.users.items():
            assert user.id is not None
            assert db.session.get(User, user.id) is not None
        for key, address in scenario.addresses.items():
            assert db.session.get(UserAddress, address.id) is not None
        for key, group in scenario.groups.items():
            assert db.session.get(AddressGroup, group.id) is not None
        for key, order in scenario.orders.items():
            assert db.session.get(Order, order.id) is not None
        for key, payment in scenario.payments.items():
            assert db.session.get(Payment, payment.id) is not None

        # Nothing hand-waved: a spec with debts wrote exactly that many orders.
        assert len(scenario.orders) == len(scenario.spec.debts)
        assert len(scenario.payments) == len(scenario.spec.debts)
        assert len(scenario.ledger) == len(scenario.spec.bottles)
        assert len(scenario.credits) == len(scenario.spec.credits)

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_declared_person_money_matches_the_rows(self, db, name):
        scenario = build(db, name)
        assert scenario.people, f"{name} declares no people to check"
        for person_key in scenario.people:
            _assert_person_matches_rows(db, scenario, person_key)

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_declared_place_money_matches_the_rows(self, db, name):
        scenario = build(db, name)
        for place_key in scenario.places:
            _assert_place_matches_rows(db, scenario, place_key)

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_ungrouped_bottle_balances_match_their_own_ledger(self, db, name):
        """A place is the group when grouped, the ADDRESS when not. The
        ungrouped arm must keep ``address_group_id IS NULL`` or a departed
        address inherits its former place's whole history."""
        scenario = build(db, name)
        for key, address in scenario.addresses.items():
            row = (
                db.session.query(BottleBalance)
                .filter(
                    BottleBalance.address_id == address.id,
                    BottleBalance.address_group_id.is_(None),
                )
                .one_or_none()
            )
            ledger_sum = db.session.query(
                func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00"))
            ).filter(
                BottleLedger.address_id == address.id,
                BottleLedger.address_group_id.is_(None),
            ).scalar()
            written = Decimal("0.00") if row is None else Decimal(str(row.balance))
            assert written.quantize(Decimal("0.01")) == Decimal(str(ledger_sum or 0)).quantize(
                Decimal("0.01")
            ), f"{name}/{key}: address-scoped balance and ledger disagree"

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_every_balance_row_has_exactly_one_scope_key(self, db, name):
        """``ck_bottle_balance_scope`` — SQLite enforces it too via create_all,
        but assert it explicitly so a Postgres-only failure can never be the
        first time we hear about it."""
        build(db, name)
        for row in db.session.query(BottleBalance).all():
            assert (row.address_group_id is None) != (row.address_id is None)


# --------------------------------------------------------------------------- #
# 2. THE CANONICAL A6 NUMBERS, PINNED AS LITERALS
# --------------------------------------------------------------------------- #


class TestA6CanonicalPreset:
    """45 000 and 35 000 are the owner's ruling. They live here as literals so a
    future edit to the factory that quietly changes them fails HERE, loudly,
    instead of silently re-baselining every downstream test."""

    def test_alice_is_forty_five_thousand_and_bob_is_thirty_five(self, db):
        s = build(db, "a6_canonical")
        alice, bob = s.expect("alice"), s.expect("bob")

        assert alice.cluster_delivered_outstanding == Decimal("25000.00")
        assert alice.foreign_place_outstanding == Decimal("20000.00")
        assert alice.collectible_total == Decimal("45000.00")
        assert alice.collectible_debt_count == 3

        assert bob.cluster_delivered_outstanding == Decimal("20000.00")
        assert bob.foreign_place_outstanding == Decimal("15000.00")
        assert bob.collectible_total == Decimal("35000.00")
        assert bob.collectible_debt_count == 2

        assert s.place_expect("g").open_cod_total == Decimal("35000.00")

    def test_the_state_that_makes_a_max_differ_from_a_union(self, db):
        """THE WHOLE POINT. ``max(own, cluster, place)`` = 35 000 while the union
        is 45 000. Both expressions are computed here from the scenario, so the
        preset is proved to actually CONTAIN the divergence — a fixture that
        lost it would go on passing every downstream test while testing nothing.
        """
        s = build(db, "a6_canonical")
        alice = s.expect("alice")
        place_total = s.place_expect("g").open_cod_total

        as_a_max = max(
            alice.account_outstanding_amount,
            alice.cluster_delivered_outstanding,
            place_total,
        )
        assert as_a_max == Decimal("35000.00")
        assert alice.collectible_total == Decimal("45000.00")
        assert as_a_max != alice.collectible_total

    def test_the_control_scenario_is_the_one_where_they_agree(self, db):
        """``debt_inside_place_only`` is the old fixture, kept on purpose: there
        the max and the union are the same number, which is exactly why the
        defect survived. Proving the two presets differ in this respect is what
        makes the pair meaningful."""
        s = build(db, "debt_inside_place_only")
        alice = s.expect("alice")
        place_total = s.place_expect("g").open_cod_total
        as_a_max = max(
            alice.account_outstanding_amount,
            alice.cluster_delivered_outstanding,
            place_total,
        )
        assert as_a_max == alice.collectible_total == Decimal("35000.00")

    def test_bottles_pool_at_the_place_and_stay_split_outside_it(self, db):
        s = build(db, "a6_canonical")
        assert s.place_expect("g").bottle_balance == Decimal("6.00")

        group_rows = (
            db.session.query(BottleBalance)
            .filter(BottleBalance.address_group_id == s.place("g").id)
            .all()
        )
        assert len(group_rows) == 1, "two coworkers at one place are ONE pool, not two"
        assert Decimal(str(group_rows[0].balance)) == Decimal("6.00")

        home_row = (
            db.session.query(BottleBalance)
            .filter(BottleBalance.address_id == s.address("alice_home").id)
            .one()
        )
        assert Decimal(str(home_row.balance)) == Decimal("2.00")


class TestPendingVersusDelivered:
    """The status axis, which widened two of the five defects."""

    def test_a_pending_order_inflates_the_headline_but_not_the_settlement(self, db):
        s = build(db, "a6_with_pending")
        alice = s.expect("alice")
        assert alice.account_outstanding_amount == Decimal("95000.00")
        assert alice.collectible_total == Decimal("45000.00")
        assert alice.engine_settleable_total == Decimal("45000.00")

    def test_the_pending_order_is_really_on_disk_and_really_pending(self, db):
        s = build(db, "a6_with_pending")
        order = s.order("alice_pending")
        assert order.status == OrderStatus.PENDING
        assert Decimal(str(s.payment("alice_pending").outstanding_amount)) == Decimal("70000.00")

    def test_a_settled_debt_is_completed_with_a_collector(self, db):
        """``ck_payments_cash_completed_requires_collector`` is a Postgres-only
        CHECK; SQLite would let a collector-less COMPLETED cash payment through
        and the scenario would stop being portable."""
        s = build(db, "solo_ungrouped_debtor")
        payment = s.payment("sam_settled")
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.collected_by is not None
        assert s.collector is not None and payment.collected_by == s.collector.id
        assert s.expect("sam").account_outstanding_amount == Decimal("20000.00")
        assert s.expect("sam").cluster_delivered_outstanding == Decimal("15000.00")

    @pytest.mark.parametrize("name", ALL_SCENARIOS)
    def test_orders_past_pending_carry_an_address(self, db, name):
        """``ck_orders_address_required_after_pending`` — a Postgres-only CHECK,
        so SQLite would happily write the row the migration forbids."""
        s = build(db, name)
        for key, order in s.orders.items():
            if order.status in {
                OrderStatus.CONFIRMED,
                OrderStatus.PREPARING,
                OrderStatus.OUT_FOR_DELIVERY,
                OrderStatus.DELIVERED,
                OrderStatus.RETURNED,
            }:
                assert order.delivery_address_id is not None, f"{name}/{key}"


# --------------------------------------------------------------------------- #
# 3. THE SHAPES THAT EXPOSED THE OTHER DEFECTS
# --------------------------------------------------------------------------- #


class TestTheDivergentShapes:
    def test_a_debt_free_coworker_is_still_worth_the_office(self, db):
        s = build(db, "debt_free_coworker")
        bob = s.expect("bob")
        assert bob.cluster_delivered_outstanding == Decimal("0.00")
        assert bob.foreign_place_outstanding == Decimal("15000.00")
        assert bob.collectible_total == Decimal("15000.00")
        assert bob.expected_row_present is True
        assert bob.expected_row_is_synthesised is True
        assert bob.expected_row_total == Decimal("15000.00")
        # He owns an address and no orders at all — the row cannot come from him.
        assert not [o for o in s.orders.values() if o.user_id == bob.user_id]

    def test_a_sibling_who_owns_the_place_but_owes_nothing_still_reaches_it(self, db):
        """The rule-3 gap: discover places through the accounts that carry DEBT
        and this person's office vanishes, taking Bob's 20 000 with it."""
        s = build(db, "sibling_owns_place_address")
        alice_a, alice_b = s.expect("alice_a"), s.expect("alice_b")

        assert alice_a.cluster_user_ids == alice_b.cluster_user_ids
        assert len(alice_a.cluster_user_ids) == 2
        # The debt-carrying account owns NO grouped address ...
        assert s.address("alice_office").user_id == alice_b.user_id
        assert s.address("alice_home").user_id == alice_a.user_id
        # ... yet the person reaches the place, and the coworker's debt.
        assert alice_a.place_group_ids == (s.place("g").id,)
        assert alice_a.cluster_delivered_outstanding == Decimal("10000.00")
        assert alice_a.foreign_place_outstanding == Decimal("20000.00")
        assert alice_a.collectible_total == Decimal("30000.00")
        # Both accounts of one person report the same person-level figure.
        assert alice_b.collectible_total == Decimal("30000.00")

    def test_credit_pools_across_the_cluster_and_not_across_the_place(self, db):
        s = build(db, "sibling_owns_place_address")
        assert s.expect("alice_a").prepaid_credit == Decimal("2500.00")
        assert s.expect("alice_b").prepaid_credit == Decimal("2500.00")
        assert s.expect("bob").prepaid_credit == Decimal("0.00"), (
            "a coworker at the same place must NOT see the cluster's credit"
        )

    def test_three_members_produce_three_different_collectible_totals(self, db):
        s = build(db, "three_member_place")
        assert s.place_expect("g").member_count == 3
        assert s.place_expect("g").open_cod_total == Decimal("12000.00")
        assert s.expect("ann").collectible_total == Decimal("15000.00")
        assert s.expect("ben").collectible_total == Decimal("12000.00")
        assert s.expect("cara").collectible_total == Decimal("12000.00")
        # Ann's answer differs from her coworkers' AND from the place total, so
        # a surface that renders the place figure on every member's row is
        # measurably wrong here — and measurably right on
        # ``debt_inside_place_only``, where all three coincide.
        assert s.expect("ann").collectible_total != s.expect("ben").collectible_total
        assert s.expect("ann").collectible_total != s.place_expect("g").open_cod_total

    def test_two_places_degrade_the_figure_and_the_address_together(self, db):
        """Decision E7. The failure mode this guards is keeping the place scope
        while falling back on the number, so both halves are asserted."""
        s = build(db, "two_places_one_cluster")
        alice = s.expect("alice")
        assert len(alice.place_group_ids) == 2
        assert alice.collect_scope_type == "cluster"
        assert alice.collect_scope_address_ids == ()
        assert alice.collect_scope_amount == Decimal("10000.00")
        assert alice.expected_row_total == Decimal("10000.00")
        # The union across both places exists and must appear nowhere.
        assert alice.collectible_total == Decimal("35000.00")

        bob = s.expect("bob")
        assert bob.collect_scope_type == "place"
        assert bob.collect_scope_address_ids == (s.address("bob_g1").id,)
        assert bob.collect_scope_amount == Decimal("30000.00")

    def test_a_dissolved_place_keeps_its_history_and_forwards(self, db):
        s = build(db, "dissolved_place")
        place = s.place_expect("g_old")
        assert place.is_dissolved is True
        assert place.dissolved_onto_address_id == s.address("survivor").id
        assert place.address_ids == ()
        assert place.member_count == 0
        assert place.open_cod_total == Decimal("0.00")
        # Frozen ledger rows still name the dissolved group ...
        stamped = (
            db.session.query(func.count(BottleLedger.id))
            .filter(BottleLedger.address_group_id == s.place("g_old").id)
            .scalar()
        )
        assert stamped == 2
        # ... and net to zero, with the bottles now on the survivor.
        assert place.bottle_balance == Decimal("0.00")
        survivor_row = (
            db.session.query(BottleBalance)
            .filter(BottleBalance.address_id == s.address("survivor").id)
            .one()
        )
        assert Decimal(str(survivor_row.balance)) == Decimal("6.00")

    def test_the_grocery_account_is_offered_only_what_the_engine_settles(self, db):
        """WAS ``..._shows_one_number_and_settles_another``. The factory used to
        publish the divergence as data and decline to judge it: the display
        resolved PLACE/18 000 while the engine forced PERSONAL/8 000, and the
        oracle said only "here are both numbers".

        It is judged now. The engine's refusal is deliberate — a grocery's cash
        is mirrored onto a corporate contract and may never co-mingle with a
        household's — so the ENGINE is right and the DISPLAY was the defect.
        ``collect_scope_type`` is DERIVED from ``engine_scope_type`` rather than
        racing it, so the two can no longer part company here for the same reason
        they cannot in production: one decision, not two that agree.

        The whole point of the preset is the contrast on the next four lines: the
        refusal is per-ACCOUNT, so Alice, an ordinary individual at the SAME
        place, still gets the whole 18 000 union.
        """
        s = build(db, "grocery_at_place")
        mart = s.expect("mart")
        assert mart.is_grocery is True
        assert mart.engine_scope_type == "personal"
        assert mart.collect_scope_type == "cluster"
        # The figure and the address degrade TOGETHER — never one without the other.
        assert mart.collect_scope_address_ids == ()
        assert mart.collect_scope_amount == mart.engine_settleable_total == Decimal("8000.00")
        # The place's 18 000 is still a fact about the topology; it is simply not
        # this account's to be offered.
        assert mart.collectible_total == Decimal("18000.00")

        alice = s.expect("alice")
        assert alice.engine_scope_type == "place"
        assert alice.collect_scope_type == "place"
        assert alice.collect_scope_address_ids == (s.address("alice_office").id,)
        assert alice.engine_settleable_total == alice.collect_scope_amount == Decimal("18000.00")

    def test_the_zero_scenario_is_zero_everywhere(self, db):
        s = build(db, "zero_everything")
        nora = s.expect("nora")
        assert nora.account_outstanding_amount == Decimal("0.00")
        assert nora.cluster_delivered_outstanding == Decimal("0.00")
        assert nora.collectible_total == Decimal("0.00")
        assert nora.expected_row_present is False
        assert nora.place_group_ids == ()
        assert db.session.query(func.count(Payment.id)).scalar() == 0
        assert db.session.query(func.count(BottleBalance.id)).scalar() == 0

    def test_a_debt_free_place_puts_nobody_on_the_list(self, db):
        s = build(db, "debt_outside_place_only")
        assert s.place_expect("g").open_cod_total == Decimal("0.00")
        assert s.expect("bob").expected_row_present is False
        alice = s.expect("alice")
        assert alice.collectible_total == Decimal("10000.00")
        assert alice.foreign_place_outstanding == Decimal("0.00")

    def test_an_exempt_member_exempts_the_whole_cluster(self, db):
        s = build(db, "cod_exempt_cluster")
        a, b = s.expect("vip_a"), s.expect("vip_b")
        assert a.cluster_user_ids == b.cluster_user_ids
        assert a.is_cod_exempt is True
        assert b.is_cod_exempt is False  # the FLAG is per-account ...
        # ... while the money is per-person.
        assert a.cluster_delivered_outstanding == b.cluster_delivered_outstanding == Decimal("70000.00")
        assert a.prepaid_credit == b.prepaid_credit == Decimal("1500.00")
        assert a.engine_scope_type == "cluster"

    def test_the_engine_settles_what_the_surface_offers(self, db):
        """The invariant the defects broke, asserted across the whole catalogue:
        SHOWN == SETTLED, **with no carve-out**.

        🔴 THE CARVE-OUT IS THE PROOF. This test used to be named
        ``..._everywhere_but_grocery`` and opened with ``if expected.is_grocery:
        continue`` — ``grocery_at_place`` was the single declared exception,
        named here rather than skipped silently precisely so that deleting it
        would be the visible end of the defect. Instance #6 closed it (the
        display now ASKS ``resolve_allocation_scope`` before widening anyone), so
        the exception is gone and every person of every preset is held to the
        invariant. Do not re-introduce a skip here: an exception to this
        assertion IS the bug, in every form it has taken.
        """
        for name in ALL_SCENARIOS:
            scenario = build(db, name)
            for person_key, expected in scenario.people.items():
                assert expected.collect_scope_amount == expected.engine_settleable_total, (
                    f"{name}/{person_key}: offers {expected.collect_scope_amount}, "
                    f"settles {expected.engine_settleable_total}"
                )


# --------------------------------------------------------------------------- #
# 4. INDEPENDENCE, DETERMINISM, UNIQUENESS
# --------------------------------------------------------------------------- #


class TestScenariosAreIndependent:
    def test_building_one_scenario_does_not_perturb_another(self, db):
        first = build(db, "a6_canonical")
        assert first.expect("alice").collectible_total == Decimal("45000.00")

        second = build(db, "three_member_place")
        third = build(db, "sibling_owns_place_address")

        # Re-check the FIRST scenario against the rows, with two other worlds
        # now sharing the database. A leak (a shared address group, a phone
        # collision resolved by overwriting, a place whose membership grew)
        # would move one of these numbers.
        for person_key in first.people:
            _assert_person_matches_rows(db, first, person_key)
        for place_key in first.places:
            _assert_place_matches_rows(db, first, place_key)
        for person_key in second.people:
            _assert_person_matches_rows(db, second, person_key)
        for person_key in third.people:
            _assert_person_matches_rows(db, third, person_key)

        assert first.expect("alice").collectible_total == Decimal("45000.00")
        assert second.expect("ann").collectible_total == Decimal("15000.00")
        assert third.expect("alice_a").collectible_total == Decimal("30000.00")

    def test_all_presets_coexist_in_one_database(self, db):
        built = [build(db, name) for name in ALL_SCENARIOS]
        for scenario in built:
            for person_key in scenario.people:
                _assert_person_matches_rows(db, scenario, person_key)
            for place_key in scenario.places:
                _assert_place_matches_rows(db, scenario, place_key)

        phones = [r[0] for r in db.session.query(User.phone).all()]
        assert len(phones) == len(set(phones))

    def test_presets_have_distinct_namespaces(self):
        namespaces = [SCENARIOS[n].namespace for n in ALL_SCENARIOS]
        assert None not in namespaces, "every preset must pin its namespace"
        assert len(namespaces) == len(set(namespaces))

    def test_rebuilding_the_same_scenario_into_one_database_is_refused(self, db):
        build(db, "a6_canonical")
        with pytest.raises(ScenarioNamespaceCollision) as exc:
            build(db, "a6_canonical")
        assert "namespace" in str(exc.value)
        assert "a6_canonical" in str(exc.value)

    def test_an_explicit_namespace_builds_a_second_independent_copy(self, db):
        first = build(db, "a6_canonical")
        second = build(db, "a6_canonical", namespace=77)

        assert first.user("alice").id != second.user("alice").id
        assert first.user("alice").phone != second.user("alice").phone
        assert first.place("g").id != second.place("g").id
        # Two identical offices in one database, and neither absorbs the other.
        assert first.place_expect("g").open_cod_total == Decimal("35000.00")
        assert second.place_expect("g").open_cod_total == Decimal("35000.00")
        _assert_place_matches_rows(db, first, "g")
        _assert_place_matches_rows(db, second, "g")
        assert first.expect("alice").collectible_total == Decimal("45000.00")
        assert second.expect("alice").collectible_total == Decimal("45000.00")

    def test_generated_identities_never_collide_with_the_conftest_fixtures(
        self, db, sample_user, second_sample_user, admin_user, delivery_driver, place
    ):
        """``users.phone`` is UNIQUE and the shared fixtures own +99890123456x.
        A collision here would surface as an IntegrityError at setup in every
        test that combines the factory with the standard fixtures."""
        scenario = build(db, "a6_canonical")
        phones = [r[0] for r in db.session.query(User.phone).all()]
        assert len(phones) == len(set(phones))
        for user in scenario.users.values():
            assert user.phone.startswith("+99877")

        # The conftest `place` group must not have swallowed our addresses.
        assert scenario.place_expect("g").member_count == 2
        _assert_place_matches_rows(db, scenario, "g")
        _assert_person_matches_rows(db, scenario, "alice")


class TestScenariosAreDeterministic:
    def test_identity_columns_do_not_depend_on_when_the_test_runs(self, db):
        """PKs belong to the database; every value the factory CHOOSES must be a
        pure function of (scenario, namespace)."""
        first = build(db, "a6_canonical")
        second = build(db, "a6_canonical", namespace=78)

        # Namespace-scoped identity differs only in the namespace segment.
        assert first.user("alice").phone == "+998770110001"
        assert second.user("alice").phone == "+998770780001"
        assert first.user("alice").email == "pf011.alice@place-factory.test"
        assert second.user("alice").email == "pf078.alice@place-factory.test"
        assert first.order("alice_home").order_number == "PF011-ORD-001"
        assert second.order("alice_home").order_number == "PF078-ORD-001"

    def test_no_timestamp_comes_from_the_wall_clock(self, db):
        from tests.integration.place_state_factory import BASE_TIME

        s = build(db, "a6_canonical")
        assert s.user("alice").created_at.replace(tzinfo=None) == BASE_TIME.replace(tzinfo=None)
        # Declaration order is age order, so oldest-first ranking is reproducible.
        numbers = [s.order(k).order_number for k in ("alice_home", "alice_office", "bob_office")]
        assert numbers == sorted(numbers)
        times = [s.order(k).created_at for k in ("alice_home", "alice_office", "bob_office")]
        assert times == sorted(times)
        assert len(set(times)) == 3

    def test_the_oracle_is_a_pure_function_of_the_spec(self, db):
        """Two builds of the same spec at different namespaces must declare the
        same MONEY (only ids may differ)."""
        first = build(db, "three_member_place")
        second = build(db, "three_member_place", namespace=79)
        for key in ("ann", "ben", "cara"):
            a, b = first.expect(key), second.expect(key)
            assert a.collectible_total == b.collectible_total
            assert a.cluster_delivered_outstanding == b.cluster_delivered_outstanding
            assert a.foreign_place_outstanding == b.foreign_place_outstanding
            assert a.expected_row_total == b.expected_row_total
            assert a.user_id != b.user_id


# --------------------------------------------------------------------------- #
# 5. COMPOSABILITY — an ad-hoc spec is a first-class citizen
# --------------------------------------------------------------------------- #


class TestAdHocSpecs:
    def test_a_spec_written_inline_gets_the_same_oracle(self, db):
        spec = ScenarioSpec(
            name="inline_two_offices_one_debtor",
            people=(PersonSpec("x"), PersonSpec("y")),
            places=(PlaceSpec("p"),),
            addresses=(
                AddressSpec("x_home", owner="x"),
                AddressSpec("x_office", owner="x", place="p"),
                AddressSpec("y_office", owner="y", place="p"),
            ),
            debts=(
                DebtSpec("x_home", owner="x", at="x_home", amount="1000"),
                DebtSpec("y_office", owner="y", at="y_office", amount="4000"),
            ),
            bottles=(BottleSpec("p", at="y_office", quantity="7"),),
        )
        scenario = build_scenario(db, spec)
        assert scenario.namespace >= 100, "ad-hoc specs must not land in the preset range"
        assert scenario.expect("x").collectible_total == Decimal("5000.00")
        assert scenario.expect("y").collectible_total == Decimal("4000.00")
        assert scenario.place_expect("p").bottle_balance == Decimal("7.00")
        _assert_person_matches_rows(db, scenario, "x")
        _assert_place_matches_rows(db, scenario, "p")

    def test_a_malformed_spec_fails_at_the_spec_not_at_the_database(self, db):
        bad = ScenarioSpec(
            name="inline_bad",
            people=(PersonSpec("x"),),
            addresses=(AddressSpec("x_home", owner="ghost"),),
        )
        with pytest.raises(ValueError, match="unknown 'ghost'"):
            build_scenario(db, bad)

    def test_a_delivered_debt_without_an_address_is_refused(self, db):
        bad = ScenarioSpec(
            name="inline_addressless_delivered",
            people=(PersonSpec("x"),),
            addresses=(AddressSpec("x_home", owner="x"),),
            debts=(DebtSpec("d", owner="x", amount="100"),),
        )
        with pytest.raises(ValueError, match="requires an address"):
            build_scenario(db, bad)

    def test_duplicate_keys_are_refused(self, db):
        bad = ScenarioSpec(
            name="inline_dupe",
            people=(PersonSpec("x"), PersonSpec("x")),
        )
        with pytest.raises(ValueError, match="duplicate people keys"):
            build_scenario(db, bad)

    def test_unknown_preset_name_lists_the_known_ones(self, db):
        with pytest.raises(KeyError, match="a6_canonical"):
            build(db, "no_such_scenario")


# --------------------------------------------------------------------------- #
# 6. THE GUARD ON THE GUARD
# --------------------------------------------------------------------------- #


class TestTheOracleIsFalsifiable:
    """If these pass while the rows are wrong, every green above is worthless."""

    def test_corrupting_a_payment_breaks_the_person_cross_check(self, db):
        scenario = build(db, "a6_canonical")
        _assert_person_matches_rows(db, scenario, "alice")  # green first

        payment = scenario.payment("alice_office")
        payment.outstanding_amount = Decimal("1.00")
        db.session.commit()

        with pytest.raises(AssertionError):
            _assert_person_matches_rows(db, scenario, "alice")

    def test_corrupting_place_membership_breaks_the_place_cross_check(self, db):
        scenario = build(db, "a6_canonical")
        _assert_place_matches_rows(db, scenario, "g")  # green first

        scenario.address("bob_office").address_group_id = None
        db.session.commit()

        with pytest.raises(AssertionError):
            _assert_place_matches_rows(db, scenario, "g")

    def test_corrupting_a_bottle_balance_breaks_the_ledger_agreement(self, db):
        scenario = build(db, "a6_canonical")
        _assert_place_matches_rows(db, scenario, "g")

        row = (
            db.session.query(BottleBalance)
            .filter(BottleBalance.address_group_id == scenario.place("g").id)
            .one()
        )
        row.balance = Decimal("99.00")
        db.session.commit()

        with pytest.raises(AssertionError):
            _assert_place_matches_rows(db, scenario, "g")

    def test_breaking_the_link_breaks_the_cluster_cross_check(self, db):
        scenario = build(db, "sibling_owns_place_address")
        _assert_person_matches_rows(db, scenario, "alice_a")

        scenario.user("alice_b").canonical_customer_id = None
        db.session.commit()

        with pytest.raises(AssertionError):
            _assert_person_matches_rows(db, scenario, "alice_a")


# --------------------------------------------------------------------------- #
# 7. THE FACTORY DESCRIBES THE SAME WORLD THE ENGINE READS
#
#    Two PRIMITIVE engine readers only — a place's open COD total and a
#    customer's cluster figure. Both are plain sums, not the composed figures
#    the defects lived in, so this is a validity check on the factory rather
#    than a behavioural test of anyone's screen. It exists because an oracle
#    that is internally consistent but describes rows the engine cannot see
#    (wrong payment method, wrong order status, an address the query filters
#    out) would be perfectly self-consistent and completely useless.
# --------------------------------------------------------------------------- #


class TestTheEngineSeesWhatTheFactoryBuilt:
    def test_place_statement_totals_match_the_declared_place(self, db):
        from business_app.services.cash_collection_service import CashCollectionService

        scenario = build(db, "a6_canonical")
        expected = scenario.place_expect("g")
        statement = CashCollectionService().get_place_cod_statement(expected.group_id)

        assert Decimal(str(statement["total_outstanding_amount"])) == expected.open_cod_total
        assert statement["active_cod_debt_count"] == expected.open_cod_debt_count
        assert statement["member_count"] == expected.member_count
        assert sorted(i["owner_user_id"] for i in statement["items"]) == sorted(
            {scenario.user("alice").id, scenario.user("bob").id}
        )

    def test_customer_statement_figures_match_the_declared_person(self, db):
        from business_app.services.cash_collection_service import CashCollectionService

        scenario = build(db, "a6_with_pending")
        alice = scenario.expect("alice")
        statement = CashCollectionService().get_customer_cod_statement(alice.user_id)

        assert Decimal(str(statement["total_outstanding_amount"])) == alice.account_outstanding_amount
        assert Decimal(str(statement["cluster_delivered_outstanding_amount"])) == (
            alice.cluster_delivered_outstanding
        )
        assert statement["active_cod_debt_count"] == alice.cluster_delivered_debt_count
        assert [p["place_group_id"] for p in statement["places"]] == list(alice.place_group_ids)

    def test_the_linked_cluster_is_the_cluster_the_engine_resolves(self, db):
        from business_app.services.customer_link_service import CustomerLinkService

        scenario = build(db, "sibling_owns_place_address")
        expected = scenario.expect("alice_a")
        assert tuple(CustomerLinkService().get_cluster_user_ids(expected.user_id)) == (
            expected.cluster_user_ids
        )

    def test_every_preset_builds_against_real_postgres(self, pg_db):
        """THE PORTABILITY CLAIM, paid for.

        The default suite is in-memory SQLite with FOREIGN KEYS OFF and every
        migration-only CHECK silently absent — the documented blind spot behind
        several shipped constraint violations. A fixture other streams will run
        under ``pg_app`` must therefore be proved against a real, fully-migrated
        Postgres at least once, or "Postgres-safe" is just a comment.

        One test builds ALL presets into ONE ephemeral database: the migration
        run is the expensive part, and a shared database additionally proves the
        namespaces do not collide under real UNIQUE indexes and real FKs.
        """
        built = [build(pg_db, name) for name in ALL_SCENARIOS]
        for scenario in built:
            for person_key in scenario.people:
                _assert_person_matches_rows(pg_db, scenario, person_key)
            for place_key in scenario.places:
                _assert_place_matches_rows(pg_db, scenario, place_key)

        a6 = next(s for s in built if s.name == "a6_canonical")
        assert a6.expect("alice").collectible_total == Decimal("45000.00")
        assert a6.expect("bob").collectible_total == Decimal("35000.00")

    def test_the_canonical_customer_row_exists_and_points_at_a_member(self, db):
        scenario = build(db, "cod_exempt_cluster")
        canonical_id = scenario.user("vip_a").canonical_customer_id
        assert canonical_id is not None
        canonical = db.session.get(CanonicalCustomer, canonical_id)
        assert canonical is not None
        assert canonical.primary_user_id in scenario.expect("vip_a").cluster_user_ids
