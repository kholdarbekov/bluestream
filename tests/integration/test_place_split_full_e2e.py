"""E2E: `bottles_leaving` — spec §7.1's split — across its ENTIRE input domain.

One axis, exhaustively: what the admin may put in the "how many bottles leave
with this address" box, what happens for every one of those values, and what
must NOT happen when the value is impossible.

Three things this file insists on everywhere:

* **The pair, never one side.** Bottles are neither minted nor destroyed by a
  membership edit, so every accepted split asserts `place_after + departed ==
  place_before` (and, where a wider blast radius is plausible, the GLOBAL
  `Σ bottle_balances` before/after). Assert only "the place is 5 now" and a bug
  that also minted 2 at the departing address sails straight through.
* **A rejection writes NOTHING** — no ledger row, no balance movement, no
  `CustomerLinkEvent`, and the address is still in the group. Asserted WITHOUT
  an intervening `db.session.rollback()`, because the rollback is exactly what
  would hide a flushed-but-uncommitted phantom event. Several tests say so
  again in their own docstring; do not "tidy" them by adding one.
* **Real write paths only.** Balances are built with `admin_adjust_balance`,
  `record_bottles_delivered` and `record_bottles_returned` against real `Order`
  rows, and the HTTP tests go through the real route with a real JWT. The only
  hand-written `BottleBalance` mutations in this file are the two DRIFT
  fixtures and the stale-own-row fixture, where the ROW ITSELF is the subject —
  each says so at the point of use.

THE DISTINCTION THIS AXIS RESTS ON. A place has two figures that legitimately
disagree on production data: the STORED balance (`bottle_balances.balance`,
what `get_place_balance` and every operational reader returns) and the LEDGER
SUM. The split's cap is the STORED figure. `TestTheCapIsTheStoredBalance`
pins both directions of the drift, because an "improvement" that capped
against `SUM(bottle_ledger.quantity)` would look more correct and would refuse
every legal split at a hand-seeded place — the majority of production places.

Bugs pinned here rather than fixed (see the module's test names and the run
notes): the split's cap is read by an UNLOCKED SUM and a concurrent commit
drives the place NEGATIVE; two concurrent removals leave a ONE-member place
group undissolved; a delivery committing during a JOIN strands an own-scope
balance row that the next split then compounds; a grouped address carrying such
a row breaks conservation on a split; `suggested_bottles_leaving` never checks
the address is in the group it is passed (FENCED at the route — see that test
for the real blast radius); sub-cent splits are silently discarded; `-0`
survives to the wire as `-0.0`; and `reconcile_balance` is still exposed on a
route that destroys the drift it was built to preserve.

A NOTE ON THE TWO STRICT XFAILS, because they are easy to get wrong. Each names
an invariant the racing tests defeat, and each is written so it XPASSES under
the fix — which means the shared race helper asserts NOTHING about how the two
sessions ended. Every plausible fix here is a lock, and a lock makes the second
session BLOCK and then abort on its `lock_timeout`; an xfail that pinned "both
sessions succeeded AND the invariant held" would stay red forever and could
never force its own marker off. Verified by applying each fix and watching the
xfail turn into a strict-xpass failure.
"""

import itertools
import json
import math
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from business_app import db as _db
from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import (
    BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
    BottleTrackingService,
)
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)

# --------------------------------------------------------------------------- #
# Builders — every balance below is produced by a REAL service write path.
# --------------------------------------------------------------------------- #

_SEQ = itertools.count(1)


def _user(db, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL):
    n = next(_SEQ)
    u = User(
        email=f"place-split-{n}@example.com",
        phone=f"+99870{n:07d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=f"F{n}",
        last_name=f"L{n}",
        user_type=user_type,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id, title="Office"):
    a = UserAddress(
        user_id=user_id,
        title=title,
        full_address=f"desk {next(_SEQ)}, Tashkent",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
    )
    db.session.add(a)
    db.session.commit()
    return a


def _order(db, user, address):
    order = Order(
        user_id=user.id,
        order_number=f"ORD-SPLIT-{next(_SEQ)}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _seed(db, address, user, qty, notes="seed"):
    """Put `qty` bottles at this address's PLACE through the real write path.

    Deliberately NOT a hand-inserted `BottleBalance`: the row is keyed by PLACE
    and only the service knows which place an address resolves to.
    """
    entry = BottleTrackingService().admin_adjust_balance(
        user_id=user.id,
        address_id=address.id,
        adjustment=Decimal(str(qty)),
        actor_user_id=user.id,
        notes=notes,
    )
    db.session.commit()
    return entry


def _deliver(db, user, address, qty):
    order = _order(db, user, address)
    entry = BottleTrackingService().record_bottles_delivered(
        order.id, user.id, address.id, Decimal(str(qty))
    )
    db.session.commit()
    return entry


def _give_back(db, user, address, qty):
    order = _order(db, user, address)
    entry = BottleTrackingService().record_bottles_returned(
        user.id, address.id, Decimal(str(qty)), order_id=order.id
    )
    db.session.commit()
    return entry


def _place(address_id):
    """The operational read: what the PLACE this address belongs to holds."""
    return BottleTrackingService.get_place_balance(address_id)


def _group_row(group_id):
    return BottleBalance.query.filter_by(address_group_id=group_id).one_or_none()


def _stored_total():
    """Σ of every `bottle_balances` row in the database."""
    total = _db.session.query(
        func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00"))
    ).scalar()
    return Decimal(str(total or 0))


def _coupled_total():
    """Σ of every BALANCE-COUPLED ledger quantity in the database.

    The decoupled writer (`merge_backfill:`) is the one thing that may move the
    ledger without moving a balance, so it is excluded here — that is what makes
    `Σ balances` and this figure comparable as a PAIR.
    """
    key = func.coalesce(BottleLedger.idempotency_key, "")
    q = _db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
    for prefix in BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES:
        q = q.filter(key.notlike(f"{prefix}%"))
    return Decimal(str(q.scalar() or 0))


def _ledger_sum(scope):
    return Decimal(
        str(
            _db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
            .filter(*scope.ledger_filter())
            .scalar()
            or 0
        )
    )


def _group_ledger_sum(group_id):
    return _ledger_sum(BottleScope.for_group(group_id))


def _own_ledger_sum(address_id):
    return _ledger_sum(BottleScope.for_address(address_id))


def _snapshot():
    """Everything a rejected split must leave untouched, in one comparable value."""
    return {
        "ledger_rows": BottleLedger.query.count(),
        "balance_rows": BottleBalance.query.count(),
        "stored_total": _stored_total(),
        "coupled_total": _coupled_total(),
        "link_events": CustomerLinkEvent.query.count(),
    }


def _leave_rows(address_id):
    """The split's two halves, oldest first."""
    return (
        BottleLedger.query.filter(
            BottleLedger.address_id == address_id,
            BottleLedger.idempotency_key.like("place_leave:%"),
        )
        .order_by(BottleLedger.id.asc())
        .all()
    )


class _Office:
    """One place group over N distinct customers, one address each."""

    def __init__(self, db, member_count=3):
        self.svc = CustomerLinkService()
        self.admin = _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)
        self.users = [_user(db) for _ in range(member_count)]
        self.addrs = [_addr(db, u.id) for u in self.users]
        group = self.svc.create_place_group(
            [a.id for a in self.addrs],
            acting_admin_id=self.admin.id,
            reason="same office",
            label="office",
        )
        self.group_id = group.id

    # Convenience handles: `a` departs in most tests, `b` stays, `c` is quiet.
    @property
    def a(self):
        return self.addrs[0]

    @property
    def b(self):
        return self.addrs[1]

    @property
    def c(self):
        return self.addrs[2]

    @property
    def ua(self):
        return self.users[0]

    @property
    def ub(self):
        return self.users[1]

    def remove(self, address, **kwargs):
        kwargs.setdefault("reason", "left the office")
        return self.svc.remove_address_from_group(
            address.id, acting_admin_id=self.admin.id, **kwargs
        )


def _office_holding(db, qty, member_count=3):
    """A place group holding exactly `qty`, seeded through member A."""
    office = _Office(db, member_count=member_count)
    if Decimal(str(qty)) != 0:
        _seed(db, office.a, office.ua, qty)
    assert _place(office.a.id) == Decimal(str(qty)).quantize(Decimal("0.01"))
    return office


def _assert_rejected(office, address, **kwargs):
    """The split is refused with PLACE_SPLIT_INVALID and NOTHING is written.

    No `db.session.rollback()` anywhere near this helper, deliberately: the
    absence is what pins that validation runs BEFORE the `CustomerLinkEvent` is
    flushed. A rollback would discard a phantom event and make the property
    untestable — exactly as the HTTP layer's `_rollback_db_session()` masks it.
    """
    before = _snapshot()
    group_before = address.address_group_id
    with pytest.raises(ValidationError) as exc:
        office.remove(address, **kwargs)
    assert exc.value.error_code == "PLACE_SPLIT_INVALID", exc.value.message
    assert _snapshot() == before
    assert address.address_group_id == group_before
    return exc.value


# --------------------------------------------------------------------------- #
# A. The default, and its identity with an explicit zero
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.e2e
class TestTheDefaultMovesNothing:
    def test_omitting_bottles_leaving_keeps_every_bottle_with_the_place(self, db):
        """§8's retirement in one assertion: the DEFAULT writes nothing at all.

        A future re-introduction of netting — or a default of
        `bottles_leaving=suggested` — would silently move money-equivalent
        stock on every single removal.
        """
        office = _office_holding(db, "7")
        before = _snapshot()

        result = office.remove(office.a, reason="left")

        assert result == {
            "group_id": office.group_id,
            "bottles_leaving": Decimal("0.00"),
            "dissolved": False,
        }
        assert "netting" not in result
        assert _place(office.b.id) == Decimal("7.00")
        assert _place(office.a.id) == Decimal("0.00")
        # The departing address gains no balance row of its own.
        assert BottleTrackingService.get_place_balance_row(office.a.id) is None
        assert BottleLedger.query.count() == before["ledger_rows"]
        assert BottleBalance.query.count() == before["balance_rows"]
        assert _stored_total() == before["stored_total"]
        assert (
            CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").count() == 1
        )

    def test_explicit_zero_is_indistinguishable_from_omitting_the_field(self, db):
        """`bottles_leaving is not None else 0` and `if leaving > 0` are two
        separate gates. A refactor that folds the `> 0` check into the validator
        (or treats 0 as "write a zero-quantity audit pair") diverges the two
        paths — so run both on equivalent fixtures and compare everything."""
        omitted_office = _office_holding(db, "7")
        explicit_office = _office_holding(db, "7")

        before = _snapshot()
        omitted = omitted_office.remove(omitted_office.a, reason="left")
        explicit = explicit_office.remove(explicit_office.a, reason="left", bottles_leaving=0)

        assert {k: v for k, v in omitted.items() if k != "group_id"} == {
            k: v for k, v in explicit.items() if k != "group_id"
        }
        assert BottleLedger.query.count() == before["ledger_rows"]
        assert BottleBalance.query.count() == before["balance_rows"]
        assert _stored_total() == before["stored_total"]
        for office in (omitted_office, explicit_office):
            assert _place(office.b.id) == Decimal("7.00")
            assert _place(office.a.id) == Decimal("0.00")
            assert BottleTrackingService.get_place_balance_row(office.a.id) is None


# --------------------------------------------------------------------------- #
# B. The accepted domain — boundaries, fractions, quantization, input types
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.e2e
class TestTheAcceptedDomain:
    def test_a_split_of_the_exact_place_total_empties_the_place_to_zero(self, db):
        """The cap is INCLUSIVE: "they took everything" is legal. A `>=` slip
        would reject it; a "delete the group row when it hits 0" optimisation
        would orphan the two members who stayed."""
        office = _office_holding(db, "7")
        stored_before, place_before = _stored_total(), _place(office.a.id)

        result = office.remove(office.a, reason="took the lot", bottles_leaving=7)

        assert result["bottles_leaving"] == Decimal("7.00")
        assert result["dissolved"] is False
        assert _place(office.b.id) == Decimal("0.00")
        assert _place(office.a.id) == Decimal("7.00")
        assert _place(office.b.id) + _place(office.a.id) == place_before
        assert _stored_total() == stored_before          # global pair
        # The place still has two members, so its row survives — at 0.00.
        row = _group_row(office.group_id)
        assert row is not None and row.balance == Decimal("0.00")
        out, inn = _leave_rows(office.a.id)
        assert (out.quantity, out.balance_after) == (Decimal("-7.00"), Decimal("0.00"))
        assert (inn.quantity, inn.balance_after) == (Decimal("7.00"), Decimal("7.00"))

    def test_the_cap_boundary_pair_cap_passes_and_one_cent_over_is_refused(self, db):
        """The exact edge of `leaving > cap`, both sides, on equivalent places."""
        ok_office = _office_holding(db, "7")
        no_office = _office_holding(db, "7")

        accepted = ok_office.remove(ok_office.a, reason="all of it",
                                    bottles_leaving=Decimal("7.00"))
        assert accepted["bottles_leaving"] == Decimal("7.00")

        error = _assert_rejected(no_office, no_office.a, bottles_leaving=Decimal("7.01"))
        # The message names the REAL place balance so the admin learns the cap.
        # Derived, never hand-copied: a formatting change must not silently
        # turn this into a test of nothing.
        assert f"({_place(no_office.a.id)})" in error.message

    def test_the_smallest_representable_split_of_one_hundredth(self, db):
        """`if leaving > 0` must not become `if leaving:`-style truthiness or an
        int() coercion: quantities are Numeric(12,2) precisely because
        half-crates exist."""
        office = _office_holding(db, "7")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="one cent", bottles_leaving=Decimal("0.01"))

        assert result["bottles_leaving"] == Decimal("0.01")
        assert _place(office.b.id) == Decimal("6.99")
        assert _place(office.a.id) == Decimal("0.01")
        assert _stored_total() == stored_before
        out, inn = _leave_rows(office.a.id)
        assert (out.quantity, out.balance_after) == (Decimal("-0.01"), Decimal("6.99"))
        assert (inn.quantity, inn.balance_after) == (Decimal("0.01"), Decimal("0.01"))

    def test_a_fractional_split_of_two_and_a_half_bottles(self, db):
        office = _office_holding(db, "7")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="half crates", bottles_leaving=Decimal("2.5"))

        assert result["bottles_leaving"] == Decimal("2.50")
        assert _place(office.b.id) == Decimal("4.50")
        assert _place(office.a.id) == Decimal("2.50")
        assert _stored_total() == stored_before
        out, inn = _leave_rows(office.a.id)
        # Exact halves, not just "they sum to zero": a pair of -2.50/+2.50 and a
        # pair of -0.01/+0.01 both sum to zero.
        assert (out.quantity, out.balance_after) == (Decimal("-2.50"), Decimal("4.50"))
        assert (inn.quantity, inn.balance_after) == (Decimal("2.50"), Decimal("2.50"))

    def test_a_sub_cent_split_is_SILENTLY_DISCARDED(self, db):
        """PINS CURRENT (buggy-ish) BEHAVIOUR — see the module notes.

        The range check runs on the RAW value and the caller's `if leaving > 0`
        gate runs on the QUANTIZED one, so 0.004 returns success with
        `bottles_leaving == 0.00`, writes nothing, and tells the admin zero
        without an error or a warning. Moving the quantize would either write a
        pointless 0.00 ledger pair or reject a value that passes today; this
        test is here so that change is a deliberate one.
        """
        office = _office_holding(db, "7")
        before = _snapshot()

        result = office.remove(office.a, reason="sub cent", bottles_leaving=Decimal("0.004"))

        assert result["bottles_leaving"] == Decimal("0.00")
        assert BottleLedger.query.count() == before["ledger_rows"]
        assert _place(office.b.id) == Decimal("7.00")
        assert BottleTrackingService.get_place_balance_row(office.a.id) is None

    def test_a_sub_cent_split_that_rounds_up_writes_exactly_one_cent(self, db):
        """Both halves must be built from the SAME quantized figure — build one
        from the raw Decimal and bottles are minted at the third decimal."""
        office = _office_holding(db, "7")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="rounds up", bottles_leaving=Decimal("0.006"))

        assert result["bottles_leaving"] == Decimal("0.01")
        assert _place(office.b.id) == Decimal("6.99")
        assert _place(office.a.id) == Decimal("0.01")
        assert _stored_total() == stored_before
        out, inn = _leave_rows(office.a.id)
        assert (out.quantity, out.balance_after) == (Decimal("-0.01"), Decimal("6.99"))
        assert (inn.quantity, inn.balance_after) == (Decimal("0.01"), Decimal("0.01"))

    @pytest.mark.parametrize(
        "requested,expected",
        [
            ("2.345", Decimal("2.34")),   # banker's rounding: down to the even cent
            ("2.355", Decimal("2.36")),   # ...and up when the cent digit is odd
            ("0.005", Decimal("0.00")),
            ("0.015", Decimal("0.02")),
        ],
    )
    def test_rounding_at_the_third_decimal_is_half_even(self, db, requested, expected):
        """Nothing in the code STATES a rounding mode, so a Decimal-context
        change or an explicit ROUND_HALF_UP would shift every one of these by a
        cent. The conservation pair is the only other thing that would notice."""
        office = _office_holding(db, "7")
        stored_before, place_before = _stored_total(), _place(office.a.id)

        result = office.remove(office.a, reason="rounding", bottles_leaving=Decimal(requested))

        assert result["bottles_leaving"] == expected
        assert _place(office.b.id) == place_before - expected
        assert _place(office.a.id) == expected
        assert _place(office.b.id) + _place(office.a.id) == place_before
        assert _stored_total() == stored_before

    def test_a_value_that_rounds_UP_to_the_cap_is_accepted_and_lands_on_zero(self, db):
        """6.999 quantizes to exactly the cap, so the place lands on 0.00 and
        never below it. The range check runs pre-quantize and the write uses the
        quantized figure — if the stored balance ever stopped being 2-dp,
        rounding past the cap would drive a place negative through the one path
        whose whole job is to stop that."""
        office = _office_holding(db, "7")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="rounds to the cap",
                               bottles_leaving=Decimal("6.999"))

        assert result["bottles_leaving"] == Decimal("7.00")
        assert _place(office.b.id) == Decimal("0.00")
        assert _place(office.a.id) == Decimal("7.00")
        assert _stored_total() == stored_before

    def test_a_value_above_the_cap_is_refused_even_though_it_would_quantize_down(self, db):
        """7.0049 quantizes to 7.00 — but the range check sees the RAW value and
        refuses it. Pins that the check is pre-quantize."""
        office = _office_holding(db, "7")
        _assert_rejected(office, office.a, bottles_leaving=Decimal("7.0049"))

    @pytest.mark.parametrize(
        "requested", ["3", "3.0", "3.00", " 3 ", "+3", "1e0", "5E-1"]
    )
    def test_numeric_string_forms_are_accepted(self, db, requested):
        """The value arrives off a JSON body, so strings are routine (mobile
        clients, curl, integration scripts). `Decimal(str(x))` parses each of
        these — including the whitespace-padded form, which Decimal strips.
        Swapping in float() or a regex validator changes which of them parse and
        silently turns a legal admin action into a 400."""
        office = _office_holding(db, "7")
        expected = Decimal(requested.strip()).quantize(Decimal("0.01"))
        place_before, stored_before = _place(office.a.id), _stored_total()

        result = office.remove(office.a, reason="string form", bottles_leaving=requested)

        assert result["bottles_leaving"] == expected
        assert _place(office.b.id) == place_before - expected
        assert _place(office.a.id) == expected
        assert _stored_total() == stored_before

    @pytest.mark.parametrize(
        "requested", [3, 3.0, Decimal("3"), (0.1 + 0.2) * 10]
    )
    def test_int_float_decimal_and_binary_dust_all_land_on_the_same_figure(self, db, requested):
        """`(0.1+0.2)*10` is 3.0000000000000004. `Decimal(str(float))` is the
        ONLY safe bridge: `Decimal(float)` directly would write
        3.000000000000000444089209850062616169452667236328125 into the ledger
        and break the pair sum if only one half were converted."""
        office = _office_holding(db, "7")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="input types", bottles_leaving=requested)

        assert result["bottles_leaving"] == Decimal("3.00")
        assert _place(office.b.id) == Decimal("4.00")
        assert _place(office.a.id) == Decimal("3.00")
        assert _stored_total() == stored_before
        out, inn = _leave_rows(office.a.id)
        assert (out.quantity, inn.quantity) == (Decimal("-3.00"), Decimal("3.00"))


# --------------------------------------------------------------------------- #
# C. The rejected domain
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.e2e
class TestTheRejectedDomain:
    @pytest.mark.parametrize(
        "requested", [-1, -0.01, Decimal("-0.01"), "-1", "-1e0", "-7", -99999]
    )
    def test_negative_quantities_are_refused(self, db, requested):
        """A negative split means the departing address hands bottles TO the
        place it is leaving — representable in the ledger, and the exact class
        of silent absorption that retiring netting was meant to stop. The
        string forms matter: a `bottles_leaving < 0` guard written BEFORE the
        Decimal coercion would let '-1' through, so the coercion-then-check
        ordering is load-bearing."""
        office = _office_holding(db, "7")
        _assert_rejected(office, office.a, bottles_leaving=requested)

    @pytest.mark.parametrize("requested", [Decimal("-0"), "-0", -0.0, "-0.00"])
    def test_negative_zero_is_ACCEPTED_and_writes_nothing(self, db, requested):
        """PINS CURRENT BEHAVIOUR. `-0` is neither `< 0` nor `> cap`, quantizes
        to `Decimal('-0.00')`, and `leaving > 0` is False — so nothing is
        written and the result carries a NEGATIVE zero, which the route renders
        as JSON `-0.0` (see the HTTP section). Any future `if leaving != 0:`
        gate would start writing a -0.00 ledger pair.

        Note `-0` as a Python int is just 0, so the int form is not testable and
        is deliberately absent from the parameters.
        """
        office = _office_holding(db, "7")
        before = _snapshot()

        result = office.remove(office.a, reason="negative zero", bottles_leaving=requested)

        assert result["bottles_leaving"] == Decimal("0.00")
        assert str(result["bottles_leaving"]) == "-0.00"      # the sign survives
        assert result["bottles_leaving"].is_signed()
        assert BottleLedger.query.count() == before["ledger_rows"]
        assert _place(office.b.id) == Decimal("7.00")
        assert BottleTrackingService.get_place_balance_row(office.a.id) is None

    def test_a_wildly_high_quantity_is_refused_and_not_clamped(self, db):
        """Asserted WITHOUT a rollback, so a flushed-but-uncommitted
        `CustomerLinkEvent` would be visible to the queries below. Validation
        currently runs BEFORE the event is flushed; move it one line later and a
        rejected split leaves a phantom "address removed" audit row in the
        session for the next commit to persist."""
        office = _office_holding(db, "7")

        _assert_rejected(office, office.a, bottles_leaving=99)

        assert _place(office.a.id) == Decimal("7.00")   # still resolves to the place
        assert _place(office.b.id) == Decimal("7.00")
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 0

    @pytest.mark.parametrize(
        "requested", [float("nan"), float("inf"), float("-inf")]
    )
    def test_nan_and_infinity_floats_are_refused(self, db, requested):
        """EVERY comparison against NaN is False, so an unguarded NaN sails past
        BOTH `< 0` and `> cap` straight into a ledger row with quantity NaN.
        Deleting the `is_finite()` line reintroduces exactly that, and no range
        test would catch it."""
        office = _office_holding(db, "7")
        _assert_rejected(office, office.a, bottles_leaving=requested)

    @pytest.mark.parametrize(
        "requested",
        [
            Decimal("NaN"),
            Decimal("sNaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            "NaN",
            "nan",
            "Infinity",
            "inf",
            "-inf",
            "-Infinity",
        ],
    )
    def test_decimal_nan_snan_and_infinity_literals_are_refused(self, db, requested):
        """`Decimal('sNaN')` RAISES `InvalidOperation` on any comparison, so the
        `is_finite()` check HAS to run before the range check — otherwise this
        surfaces as an unhandled arithmetic error (a 500) instead of a 400."""
        office = _office_holding(db, "7")
        _assert_rejected(office, office.a, bottles_leaving=requested)

    @pytest.mark.parametrize(
        "requested",
        ["two", "", "   ", "3 bottles", "3,5", [], [3], {}, {"n": 3}, b"3", object()],
    )
    def test_non_numeric_values_and_containers_are_refused_without_a_500(self, db, requested):
        """The `except (ArithmeticError, ValueError)` arm has to cover all of
        these — `decimal.InvalidOperation` IS an ArithmeticError. A type whose
        `str()` raised, or a TypeError from a future coercion change, would
        escape as a 500 with a half-open transaction."""
        office = _office_holding(db, "7")
        _assert_rejected(office, office.a, bottles_leaving=requested)

    @pytest.mark.parametrize("requested", [True, False])
    def test_booleans_are_refused_in_BOTH_directions(self, db, requested):
        """PINS CURRENT BEHAVIOUR: `Decimal(str(True))` is an InvalidOperation,
        so both 400. In particular `false` does NOT mean zero — a plausible
        client bug (an unchecked toggle) is rejected loudly rather than silently
        treated as the default. A refactor to `Decimal(bottles_leaving)` would
        make True == 1 and silently move a bottle."""
        office = _office_holding(db, "7")
        _assert_rejected(office, office.a, bottles_leaving=requested)

    def test_the_reason_fence_beats_the_split_fence(self, db):
        """Fence ordering is contractual, not arbitrary: the panel maps error
        codes to specific prose, so a blank reason must say
        PLACE_GROUP_REASON_REQUIRED even when the split is ALSO impossible."""
        office = _office_holding(db, "7")
        before = _snapshot()

        with pytest.raises(ValidationError) as exc:
            office.remove(office.a, reason="   ", bottles_leaving=99)

        assert exc.value.error_code == "PLACE_GROUP_REASON_REQUIRED"
        assert _snapshot() == before

    def test_the_missing_and_ungrouped_address_fences_beat_the_split_fence(self, db):
        """`_validated_bottles_leaving` calls `get_place_balance(address.id)`,
        which RAISES `NotFoundError` for a missing address. Validating before
        the existence check would turn a clean 400 into an unhandled error."""
        office = _office_holding(db, "7")
        loner = _addr(db, office.ua.id, title="Home")
        before = _snapshot()

        with pytest.raises(ValidationError) as missing:
            office.svc.remove_address_from_group(
                999999, acting_admin_id=office.admin.id, reason="r", bottles_leaving=99)
        assert missing.value.error_code == "CUSTOMER_LINK_ADDRESS_NOT_FOUND"

        with pytest.raises(ValidationError) as ungrouped:
            office.svc.remove_address_from_group(
                loner.id, acting_admin_id=office.admin.id, reason="r", bottles_leaving=99)
        assert ungrouped.value.error_code == "PLACE_GROUP_NOT_FOUND"

        assert _snapshot() == before

    def test_a_rejection_then_a_legal_retry_produces_exactly_one_episode(self, db):
        """The pin that validation precedes the flush, at the SERVICE layer —
        invisible over HTTP, where `_rollback_db_session()` would mask it. No
        rollback between the two calls, deliberately."""
        office = _office_holding(db, "7")
        stored_before = _stored_total()

        _assert_rejected(office, office.a, bottles_leaving=99)

        result = office.remove(office.a, reason="retry", bottles_leaving=3)

        assert result["bottles_leaving"] == Decimal("3.00")
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 1
        assert _place(office.b.id) == Decimal("4.00")
        assert _place(office.a.id) == Decimal("3.00")
        assert _stored_total() == stored_before
        assert len(_leave_rows(office.a.id)) == 2


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.parametrize(
    "requested", [10 ** 30, "1E+1000", Decimal("9.99E+999"), 2 ** 63, "1" * 40]
)
def test_huge_quantities_are_refused_without_raising(db, requested):
    """`quantize()` on a huge-exponent Decimal RAISES InvalidOperation, so this
    also pins that the range check runs BEFORE the quantize — otherwise these
    become 500s. And a value far beyond Numeric(12,2) never reaches the column."""
    office = _office_holding(db, "7")
    _assert_rejected(office, office.a, bottles_leaving=requested)


# --------------------------------------------------------------------------- #
# D. The three cap arms: negative places, empty places, places with no row
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.e2e
class TestTheCapArms:
    @staticmethod
    def _over_returned_office(db):
        """A place driven to -3.00 through the GENUINE primitives: a coworker
        returned more empties than were ever delivered."""
        office = _Office(db)
        _deliver(db, office.ua, office.a, "2")
        _give_back(db, office.ub, office.b, "5")
        assert _place(office.a.id) == Decimal("-3.00")
        return office

    @pytest.mark.parametrize("requested", [1, 0.01, 3, "0.01"])
    def test_a_negative_place_refuses_every_non_zero_split(self, db, requested):
        """The third rejection arm: `cap = max(0, place)`. Replacing the cap
        with `place` alone would reject the DEFAULT (0 > -3) and break every
        ordinary removal at an over-returned place."""
        office = self._over_returned_office(db)
        error = _assert_rejected(office, office.a, bottles_leaving=requested)
        # The message reports the REAL place balance, not the clamped cap.
        assert f"({_place(office.a.id)})" in error.message

    def test_a_negative_place_still_refuses_a_negative_split(self, db):
        office = self._over_returned_office(db)
        _assert_rejected(office, office.a, bottles_leaving=-3)

    def test_a_negative_place_accepts_zero_and_stays_negative(self, db):
        """Bottles returned before the next delivery is a real customer state;
        the default must never be rejected by the cap."""
        office = self._over_returned_office(db)
        stored_before = _stored_total()

        result = office.remove(office.a, reason="left anyway")

        assert result["bottles_leaving"] == Decimal("0.00")
        assert _place(office.b.id) == Decimal("-3.00")
        assert _place(office.a.id) == Decimal("0.00")
        assert BottleTrackingService.get_place_balance_row(office.a.id) is None
        assert _stored_total() == stored_before

    def test_a_place_holding_exactly_zero_refuses_one_hundredth(self, db):
        """Boundary of the third arm: `max(0, 0) == 0` and `0.01 > 0`. A `>=`
        slip or an `if place:` truthiness guard would mint a bottle out of an
        empty place. The row is a REAL one: +5 then -5 through the write path."""
        office = _Office(db)
        _seed(db, office.a, office.ua, "5")
        _seed(db, office.a, office.ua, "-5", notes="all returned")
        assert _place(office.a.id) == Decimal("0.00")
        assert _group_row(office.group_id) is not None      # the row exists, at 0

        _assert_rejected(office, office.a, bottles_leaving=Decimal("0.01"))

        result = office.remove(office.a, reason="nothing to take", bottles_leaving=0)
        assert result["bottles_leaving"] == Decimal("0.00")

    def test_a_place_that_never_moved_a_bottle_gains_no_balance_row(self, db):
        """Neither the validation's `get_place_balance` nor the zero path may
        call `get_or_create_balance`: a stray 0.00 row here is the nightly
        `orphaned_place_balances` violation."""
        office = _Office(db)
        assert BottleBalance.query.count() == 0

        _assert_rejected(office, office.a, bottles_leaving=1)
        assert BottleBalance.query.count() == 0

        result = office.remove(office.a, reason="never had any")
        assert result["bottles_leaving"] == Decimal("0.00")
        assert BottleBalance.query.count() == 0


# --------------------------------------------------------------------------- #
# E. The cap is the STORED balance — never the ledger sum
# --------------------------------------------------------------------------- #


def _office_stored_higher_than_its_ledger(db, stored="20"):
    """The dev address-24 shape, INSIDE a place group: the place HOLDS 20 and
    its ledger explains none of it.

    Built the way production got there, not by hand-writing the balance row:
    the figure is seeded on the address's OWN scope through the real write path,
    the ledger row that explained it is then dropped (this is what "a figure
    seeded before the ledger existed" looks like), and the address is grouped —
    §7.2's absorb CARRIES the stored figure across without re-deriving it.
    """
    svc = CustomerLinkService()
    admin = _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)
    users = [_user(db) for _ in range(3)]
    addrs = [_addr(db, u.id) for u in users]
    _seed(db, addrs[0], users[0], stored)
    BottleLedger.query.filter_by(address_id=addrs[0].id).delete(synchronize_session=False)
    db.session.commit()

    office = _Office.__new__(_Office)
    office.svc, office.admin, office.users, office.addrs = svc, admin, users, addrs
    office.group_id = svc.create_place_group(
        [a.id for a in addrs], acting_admin_id=admin.id, reason="office"
    ).id
    assert _place(addrs[0].id) == Decimal(stored).quantize(Decimal("0.01"))
    assert _group_ledger_sum(office.group_id) == Decimal("0.00")
    return office


def _office_ledger_higher_than_its_stored(db):
    """The inverse drift: the ledger sums to 10, the place only HOLDS 3.

    The stored row is written down by hand here because THE ROW ITSELF is the
    subject — this is the legacy manual correction (a figure moved without a
    ledger entry) that produced the drift in production. Everything that put
    the 10 there went through `record_bottles_delivered`.
    """
    office = _Office(db)
    _deliver(db, office.ua, office.a, "10")
    row = _group_row(office.group_id)
    row.balance = Decimal("3.00")
    db.session.commit()
    assert _place(office.a.id) == Decimal("3.00")
    assert _group_ledger_sum(office.group_id) == Decimal("10.00")
    return office


@pytest.mark.integration
@pytest.mark.e2e
class TestTheCapIsTheStoredBalance:
    """THE distinction the whole feature rests on. `get_place_balance` reads the
    STORED figure; `SUM(bottle_ledger.quantity)` is a different number on any
    place whose row was seeded or corrected by hand. An "improvement" that
    capped against the ledger sum would look more correct and would refuse
    every legal split at a hand-seeded place.
    """

    def test_a_split_of_the_whole_stored_figure_is_legal_with_an_empty_ledger(self, db):
        office = _office_stored_higher_than_its_ledger(db)
        stored_before = _stored_total()
        drift_before = _place(office.a.id) - _group_ledger_sum(office.group_id)
        assert drift_before == Decimal("20.00")

        result = office.remove(office.a, reason="took all twenty", bottles_leaving=20)

        assert result["bottles_leaving"] == Decimal("20.00")
        assert _place(office.b.id) == Decimal("0.00")
        assert _place(office.a.id) == Decimal("20.00")
        assert _stored_total() == stored_before                     # the global pair
        # The ledger moved by the same 20 in both scopes, so the DRIFT is
        # invariant under the split — which is precisely why a coupled append
        # can never close it (that is `_create_ledger_backfill_entry`'s job).
        assert _group_ledger_sum(office.group_id) == Decimal("-20.00")
        assert _own_ledger_sum(office.a.id) == Decimal("20.00")
        drift_after = (_place(office.b.id) - _group_ledger_sum(office.group_id)) + (
            _place(office.a.id) - _own_ledger_sum(office.a.id)
        )
        assert drift_after == drift_before

    def test_one_more_than_the_stored_figure_is_refused_even_though_the_ledger_is_empty(self, db):
        office = _office_stored_higher_than_its_ledger(db)
        error = _assert_rejected(office, office.a, bottles_leaving=21)
        # EXACT, not a substring: the whole point is that the number quoted is
        # the STORED 20.00 and not the ledger's 0.00, and `"20.00" in message`
        # would also pass on a message that quoted both.
        assert error.message == (
            "bottles_leaving must be between 0 and the place balance (20.00)"
        )

    def test_the_cap_does_not_rise_to_meet_a_HIGHER_ledger_sum(self, db):
        """The drift direction nobody thinks about. A cap read from the ledger
        would let an admin take 10 out of a place that only holds 3, driving the
        stored figure to -7."""
        office = _office_ledger_higher_than_its_stored(db)

        _assert_rejected(office, office.a, bottles_leaving=10)

        stored_before = _stored_total()
        result = office.remove(office.a, reason="all the place holds", bottles_leaving=3)
        assert result["bottles_leaving"] == Decimal("3.00")
        assert _place(office.b.id) == Decimal("0.00")
        assert _place(office.a.id) == Decimal("3.00")
        assert _stored_total() == stored_before

    def test_reconcile_after_a_split_on_a_drifted_place_DESTROYS_the_bottles(
        self, db, client, admin_auth_headers
    ):
        """PINS A DESTRUCTIVE ROUTE THAT PLAN C NEVER CALLS but that is still
        exposed. `reconcile_balance` assigns `balance = ledger_sum`
        unconditionally, writes NO ledger entry and only logs a warning. After a
        legitimate split on a drifted place the group's ledger sums to -20, so
        an admin "fixing a weird number" silently destroys 20 bottles with no
        audit trail. Asserted explicitly so anyone wiring reconcile into the
        place lifecycle sees the blast radius.
        """
        office = _office_stored_higher_than_its_ledger(db)
        office.remove(office.a, reason="took all twenty", bottles_leaving=20)
        stored_before = _stored_total()
        ledger_rows_before = BottleLedger.query.count()

        response = client.post(
            f"/api/v1/admin/bottles/reconcile/{office.b.id}", headers=admin_auth_headers
        )

        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["previous_balance"] == 0.0
        assert data["recalculated_balance"] == -20.0
        assert data["corrected"] is True
        assert _place(office.b.id) == Decimal("-20.00")
        # No ledger entry explains the change, and 20 bottles left the world.
        assert BottleLedger.query.count() == ledger_rows_before
        assert _stored_total() == stored_before - Decimal("20.00")


# --------------------------------------------------------------------------- #
# F. `suggested_bottles_leaving` — the pre-fill the dialog offers
# --------------------------------------------------------------------------- #


def _prefill(group_id, address_id, **kwargs):
    return BottleTrackingService.suggested_bottles_leaving(group_id, address_id, **kwargs)


@pytest.mark.integration
@pytest.mark.e2e
class TestThePrefill:
    def test_the_prefill_is_this_addresses_own_attributed_sum(self, db):
        """The query pins address_group_id AND address_id. Drop either and the
        pre-fill becomes the whole place total for every member — an admin
        accepting the default would hand one coworker the entire office."""
        office = _Office(db)
        _seed(db, office.a, office.ua, "4")
        _seed(db, office.b, office.ub, "9")
        assert _place(office.a.id) == Decimal("13.00")

        assert _prefill(office.group_id, office.a.id) == Decimal("4.00")
        assert _prefill(office.group_id, office.b.id) == Decimal("9.00")
        assert _prefill(office.group_id, office.c.id) == Decimal("0.00")

    def test_the_prefill_is_clamped_DOWN_to_what_the_place_holds(self, db):
        """An unclamped pre-fill is a value the dialog's own OK button rejects."""
        office = _Office(db)
        _deliver(db, office.ua, office.a, "9")
        _give_back(db, office.ub, office.b, "6")
        assert _place(office.a.id) == Decimal("3.00")
        stored_before = _stored_total()

        suggestion = _prefill(office.group_id, office.a.id)
        assert suggestion == Decimal("3.00")            # not 9

        result = office.remove(office.a, reason="took the prefill", bottles_leaving=suggestion)
        assert result["bottles_leaving"] == Decimal("3.00")
        assert _place(office.b.id) == Decimal("0.00")
        # BOTH sides of the pair: a prefill that emptied the place without
        # crediting the departing address conserves globally and is invisible to
        # `_stored_total()` alone.
        assert _place(office.a.id) == Decimal("3.00")
        assert _stored_total() == stored_before

    def test_the_prefill_is_floored_at_zero_for_an_over_returning_member(self, db):
        """`min(own_sum, place)` without the `max(0, ...)` yields -6, which the
        validator then rejects: the panel would pre-fill a guaranteed error."""
        office = _Office(db)
        _deliver(db, office.ua, office.a, "9")
        _give_back(db, office.ub, office.b, "6")

        suggestion = _prefill(office.group_id, office.b.id)
        assert suggestion == Decimal("0.00")
        assert office.remove(office.b, reason="prefill", bottles_leaving=suggestion)[
            "bottles_leaving"] == Decimal("0.00")

    def test_the_prefill_is_zero_for_every_member_of_a_negative_place(self, db):
        office = _Office(db)
        _deliver(db, office.ua, office.a, "8")          # strongly positive member
        _give_back(db, office.ub, office.b, "11")
        assert _place(office.a.id) == Decimal("-3.00")

        for address in office.addrs:
            assert _prefill(office.group_id, address.id) == Decimal("0.00")

    def test_the_prefill_still_counts_UNKEYED_entries(self, db):
        """`admin_adjust_balance` writes no idempotency key. Drop the
        `coalesce(..., '')` and `NULL NOT LIKE 'merge_correction:%'` evaluates
        to NULL, the row is filtered out, and every pre-fill in the system
        silently becomes 0."""
        office = _Office(db)
        for qty in ("1", "1", "1", "1"):
            _seed(db, office.a, office.ua, qty)
        assert BottleLedger.query.filter(
            BottleLedger.address_id == office.a.id,
            BottleLedger.idempotency_key.is_(None)).count() == 4

        assert _prefill(office.group_id, office.a.id) == Decimal("4.00")

    def test_the_prefill_excludes_place_level_merge_corrections_and_backfills(self, db):
        """`bottle_ledger.(user_id, address_id)` are NOT NULL, so a PLACE-level
        correction has to borrow ONE member's attribution. Counting it would
        inflate exactly that coworker's departure default by the whole place
        correction — a real quantity error, not a display one."""
        svc = CustomerLinkService()
        admin = _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)
        u1, u2 = _user(db), _user(db)
        a, b = _addr(db, u1.id), _addr(db, u2.id)
        _seed(db, a, u1, "4")                      # a's genuine own movement
        _seed(db, b, u2, "3")
        # Drift, so the merge review ALSO writes a decoupled backfill. The row
        # itself is the subject: this is the hand-corrected figure production
        # carries into a merge.
        own_row = BottleBalance.query.filter_by(address_id=a.id, address_group_id=None).one()
        own_row.balance = Decimal("6.00")
        db.session.commit()

        group = svc.create_place_group(
            [a.id, b.id], acting_admin_id=admin.id,
            reason="counted twenty on site", resulting_balance=Decimal("20"),
        )

        backfill = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("merge_backfill:%")).one()
        correction = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("merge_correction:%")).one()
        # Both borrowed A's attribution — that is the whole hazard.
        assert (backfill.address_id, correction.address_id) == (a.id, a.id)
        assert backfill.quantity == Decimal("2.00")      # stored 9 - ledger 7
        assert correction.quantity == Decimal("11.00")   # stated 20 - stored 9

        # A's pre-fill is its OWN 4 — not 4 + 2 + 11.
        assert _prefill(group.id, a.id) == Decimal("4.00")
        assert _prefill(group.id, b.id) == Decimal("3.00")

    def test_the_prefill_still_counts_merge_exclusion_reversals(self, db):
        """A reversal is attributed to the very entry it neutralises, so it
        cancels that address's own contribution. Excluding every `merge_*`
        prefix "for symmetry" would leave the excluded quantity in the
        pre-fill — the same bug in the other direction."""
        svc = CustomerLinkService()
        admin = _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)
        u1, u2 = _user(db), _user(db)
        a, b = _addr(db, u1.id), _addr(db, u2.id)
        dropped = _seed(db, a, u1, "4")
        _seed(db, a, u1, "3", notes="second, kept")
        _seed(db, b, u2, "5")

        group = svc.create_place_group(
            [a.id, b.id], acting_admin_id=admin.id,
            reason="the 4 was a data-entry error", excluded_ledger_entry_ids=[dropped.id],
        )

        reversal = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("merge_exclude:%")).one()
        assert (reversal.address_id, reversal.quantity) == (a.id, Decimal("-4.00"))
        # 4 + 3 - 4 == 3: the excluded quantity is gone from the pre-fill.
        assert _prefill(group.id, a.id) == Decimal("3.00")

    def test_the_prefill_after_a_leave_and_rejoin_nets_the_old_split_halves(self, db):
        """`place_leave:` keys are deliberately NOT in
        PLACE_LEVEL_LEDGER_KEY_PREFIXES. Adding them "for symmetry" would leave
        the departure's -4 uncancelled inside the group and understate the
        pre-fill of every rejoined member."""
        office = _Office(db)
        _seed(db, office.a, office.ua, "4")
        _seed(db, office.b, office.ub, "5")
        assert _prefill(office.group_id, office.a.id) == Decimal("4.00")
        stored_before = _stored_total()

        office.remove(office.a, reason="left with four", bottles_leaving=4)
        office.svc.add_addresses_to_group(
            office.group_id, [office.a.id], acting_admin_id=office.admin.id, reason="came back")

        assert _place(office.a.id) == Decimal("9.00")     # one pool again
        assert _prefill(office.group_id, office.a.id) == Decimal("4.00")

        again = office.remove(office.a, reason="left again",
                              bottles_leaving=_prefill(office.group_id, office.a.id))
        assert again["bottles_leaving"] == Decimal("4.00")
        assert _place(office.b.id) == Decimal("5.00")
        assert _place(office.a.id) == Decimal("4.00")
        assert _stored_total() == stored_before          # across the whole cycle

    def test_the_prefill_is_identical_hoisted_unhoisted_and_over_the_route(
        self, db, client, admin_auth_headers
    ):
        """`get_place_group_detail` hoists the place balance once for N members
        (N+1 avoidance). If the hoisted value were ever computed from a
        different address — or before a write — the panel would show a different
        pre-fill than a direct call returns."""
        office = _Office(db)
        _deliver(db, office.ua, office.a, "7")
        _give_back(db, office.ub, office.b, "2")
        _seed(db, office.c, office.users[2], "1")
        place = _place(office.a.id)

        detail = client.get(f"/api/v1/admin/place-groups/{office.group_id}",
                            headers=admin_auth_headers).get_json()["data"]
        published = {m["address_id"]: m["suggested_bottles_leaving"] for m in detail["members"]}

        for address in office.addrs:
            unhoisted = _prefill(office.group_id, address.id)
            hoisted = _prefill(office.group_id, address.id, place_balance=place)
            assert unhoisted == hoisted
            assert float(unhoisted) == published[address.id]

    def test_the_prefill_for_an_address_no_longer_in_the_named_group(self, db):
        """PINS CURRENT BEHAVIOUR OF A STALE-UI CALL.
        `suggested_bottles_leaving(group_id, address_id)` never checks the two
        agree: the own-sum is summed over the NAMED group while the clamp comes
        from `get_place_balance(address_id)`, which resolves the address's
        CURRENT place. A panel holding a stale detail payload — or a second
        admin who removed the address first — calls it in exactly this shape.

        After A leaves G with 2, G still carries A's seed (+4) and the split's
        `:out` (-2), so the own-sum over G is 2; the clamp is A's own place,
        also 2. It happens to agree here; what is asserted unconditionally is
        that the result is never negative and never exceeds A's own place.
        """
        office = _Office(db)
        _seed(db, office.a, office.ua, "4")
        _seed(db, office.b, office.ub, "5")
        group_id = office.group_id

        office.remove(office.a, reason="left", bottles_leaving=2)

        stale = _prefill(group_id, office.a.id)
        assert stale == Decimal("2.00")
        assert Decimal("0.00") <= stale <= _place(office.a.id)

    def test_a_stale_group_id_prefills_ANOTHER_places_bottles(self, db, client,
                                                              admin_auth_headers):
        """BUG PIN — reported, not fixed. The harmful shape of the test above.

        `suggested_bottles_leaving(group_id, address_id)` sums the address's own
        entries over the NAMED group but clamps against
        `get_place_balance(address_id)` — the address's CURRENT place. When those
        two are different places the suggestion is a quantity from the OLD place
        measured against the NEW one, and `remove_address_from_group` (which
        takes no group id at all) ACCEPTS it: bottles that belong to the new
        office's coworkers leave with a member who never brought any. Nothing is
        minted (the pair holds), which is exactly why only an attribution-aware
        assertion catches it.

        THE REACHABILITY, STATED HONESTLY, because it decides the severity.
        Over HTTP this is FENCED, and both fences are asserted below: the DELETE
        route rejects a (group, address) pair that disagrees with a 404 before
        the service is called at all, and the detail route only ever computes the
        pre-fill for addresses it just read OUT of that same group — so the
        stale-panel story cannot actually produce the harmful call. What is left
        is an unguarded static helper whose contract lets any NON-route caller (a
        script, a Celery task, a future route without the precheck) compute a
        quantity for one place and spend it at another. Any fix must change this
        test.
        """
        old_office = _Office(db)
        _seed(db, old_office.a, old_office.ua, "6")          # A's own six, at G1
        moving, owner, old_group = old_office.a, old_office.ua, old_office.group_id
        old_office.remove(moving, reason="left, bottles stay")   # default: 0 leave
        assert _place(moving.id) == Decimal("0.00")

        new_office = _Office(db)                              # G2: three coworkers
        _seed(db, new_office.a, new_office.ua, "10")
        new_office.svc.add_addresses_to_group(
            new_office.group_id, [moving.id],
            acting_admin_id=new_office.admin.id, reason="new desk")
        assert _place(moving.id) == Decimal("10.00")           # A's place is now G2

        # A brought nothing to G2, and G2's own prefill says so...
        assert _prefill(new_office.group_id, moving.id) == Decimal("0.00")
        # ...but the STALE group id yields A's six from the place it left,
        # clamped against G2's ten.
        stale = _prefill(old_group, moving.id)
        assert stale == Decimal("6.00")

        # FENCE 1 — the route refuses the stale pair outright, so the panel
        # story in the docstring cannot reach the service.
        stale_delete = client.delete(
            _remove_url(old_group, moving.id),
            json={"reason": "stale panel", "bottlesLeaving": float(stale)},
            headers=admin_auth_headers,
        )
        assert stale_delete.status_code == 404, stale_delete.get_json()
        # FENCE 2 — and the detail route never publishes this pair either: the
        # only production caller derives BOTH arguments from one member list.
        published = client.get(f"/api/v1/admin/place-groups/{old_group}",
                               headers=admin_auth_headers).get_json()["data"]["members"]
        assert moving.id not in {m["address_id"] for m in published}
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")).count() == 0

        # The unfenced surface: a direct service call spending the stale figure.
        stored_before = _stored_total()
        result = new_office.svc.remove_address_from_group(
            moving.id, acting_admin_id=new_office.admin.id,
            reason="accepted the stale prefill", bottles_leaving=stale)

        # THE BUG: six of the new office's bottles left with a member who never
        # contributed any — accepted, audited, and conserving.
        assert result["bottles_leaving"] == Decimal("6.00")
        assert _place(new_office.a.id) == Decimal("4.00")
        assert _place(moving.id) == Decimal("6.00")
        assert _stored_total() == stored_before
        assert owner.id == UserAddress.query.get(moving.id).user_id


_PREFILL_SHAPES = {
    # name -> factory(db) -> office. Every shape a production place can be in.
    "clean": lambda db: _clean_office(db),
    "drifted_high": lambda db: _office_stored_higher_than_its_ledger(db),
    "drifted_low": lambda db: _office_ledger_higher_than_its_stored(db),
    "negative": lambda db: _negative_office(db),
    "zero_with_a_row": lambda db: _zeroed_office(db),
    "never_moved_a_bottle": lambda db: _Office(db),
    "one_large_member": lambda db: _one_large_member_office(db),
}


def _clean_office(db):
    office = _Office(db)
    _deliver(db, office.ua, office.a, "6")
    _deliver(db, office.ub, office.b, "5")
    _give_back(db, office.ub, office.b, "4")
    return office


def _negative_office(db):
    office = _Office(db)
    _deliver(db, office.ua, office.a, "2")
    _give_back(db, office.ub, office.b, "5")
    return office


def _zeroed_office(db):
    office = _Office(db)
    _seed(db, office.a, office.ua, "5")
    _seed(db, office.a, office.ua, "-5", notes="all back")
    return office


def _one_large_member_office(db):
    office = _Office(db)
    _seed(db, office.a, office.ua, "40")
    _give_back(db, office.ub, office.b, "1")
    return office


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.parametrize("shape", sorted(_PREFILL_SHAPES))
@pytest.mark.parametrize("member_index", [0, 1, 2])
def test_the_prefill_is_always_an_acceptable_bottles_leaving(db, shape, member_index):
    """THE property that keeps the suggestion and the validator — two
    independent implementations of the same cap — in agreement across the whole
    domain: read the pre-fill, pass it straight back as `bottles_leaving`, and
    it must never be refused.

    KNOWN LIMIT, written down so nobody reads more into a green run than is
    there: this is an AGREEMENT property, not a correctness one. A pre-fill that
    returned the whole place total for every member would still be accepted by
    the validator and would still satisfy every assertion below — that failure
    mode is caught by `TestThePrefill`'s exact figures, not here. What this
    covers is the cross-product of place SHAPES against the validator's three
    arms, plus (since the assertions below name BOTH sides) the split's
    attribution: a bug that draws the right quantity out of the place and
    credits it somewhere other than the departing address conserves globally and
    would sail past `_stored_total()` alone.
    """
    office = _PREFILL_SHAPES[shape](db)
    address = office.addrs[member_index]
    place_before, stored_before = _place(address.id), _stored_total()
    suggestion = _prefill(office.group_id, address.id)
    assert suggestion >= Decimal("0.00")

    result = office.remove(address, reason=f"prefill on {shape}", bottles_leaving=suggestion)

    assert result["bottles_leaving"] == suggestion.quantize(Decimal("0.01"))
    assert _place(office.addrs[(member_index + 1) % 3].id) == place_before - suggestion
    # The OTHER side of the same pair — the half a global sum cannot see.
    assert _place(address.id) == suggestion
    assert _stored_total() == stored_before


# --------------------------------------------------------------------------- #
# G. The split combined with §7.3's dissolve
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.e2e
class TestTheSplitAndTheDissolve:
    def test_a_split_that_also_dissolves_conserves_the_pair(self, db):
        """The split runs BEFORE the dissolve deliberately — it writes while the
        place still has members. Reorder them and the split's `:out` half lands
        in a group whose balance row has already been deleted."""
        office = _Office(db, member_count=2)
        _seed(db, office.a, office.ua, "10")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="left, took four", bottles_leaving=4)

        assert result["dissolved"] is True
        assert _place(office.a.id) == Decimal("4.00")
        assert _place(office.b.id) == Decimal("6.00")
        assert _place(office.a.id) + _place(office.b.id) == Decimal("10.00")
        assert _stored_total() == stored_before
        assert _group_row(office.group_id) is None            # no orphan row
        assert AddressGroup.query.get(office.group_id) is not None   # ...but the group stays
        event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        pair = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"place_dissolve:{office.group_id}:{event.id}:%")
        ).order_by(BottleLedger.id.asc()).all()
        assert [r.quantity for r in pair] == [Decimal("-6.00"), Decimal("6.00")]
        assert [r.event_type for r in pair] == [BottleLedgerEventType.ADMIN_ADJUSTMENT] * 2

    def test_a_split_of_the_ENTIRE_place_then_dissolve_writes_no_dissolve_pair(self, db):
        """`inherited == 0` must skip the adjustment entirely: a 0.00 pair is
        ledger noise, and a sign error would hand the survivor a phantom -10."""
        office = _Office(db, member_count=2)
        _seed(db, office.a, office.ua, "10")
        stored_before = _stored_total()

        result = office.remove(office.a, reason="took everything", bottles_leaving=10)

        assert result["dissolved"] is True
        assert _place(office.a.id) == Decimal("10.00")
        assert _place(office.b.id) == Decimal("0.00")
        assert _stored_total() == stored_before
        assert _group_row(office.group_id) is None
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_dissolve:%")).count() == 0

    def test_a_split_on_a_ONE_member_group_nets_its_two_halves_to_zero(self, db):
        """The arm where the departing address is ALSO the survivor: its `:out`
        half is re-stamped into the very scope its `:in` half credited, so the
        two net to zero and in retrospect nothing ever left. A sign or filter
        slip here mints or destroys `leaving` bottles.

        UPDATED: the one-member place this arm needs is no longer buildable
        through any service path. It used to be reached by dissolve -> repopulate
        -> empty, and a dissolved group is now refused as a join target
        (`PLACE_GROUP_DISSOLVED`) — the last door, since `create_place_group`
        requires >= 2 and a removal that would leave one member dissolves in the
        same transaction. The state remains reachable in data written before that
        refusal, and `_dissolve_if_last_member`'s ZERO-REMAINING arm exists for
        it (it is why `release_group_history_to_address` passes
        `allow_memberless=True`), so the membership is pointed by hand rather
        than the coverage being retired.
        """
        office = _Office(db, member_count=2)
        _seed(db, office.a, office.ua, "10")
        stored_before = _stored_total()

        office.remove(office.b, reason="first one out")          # dissolves onto A
        db.session.refresh(office.a)
        assert office.a.address_group_id is None
        assert _place(office.a.id) == Decimal("10.00")

        with pytest.raises(ValidationError) as exc:
            office.svc.add_addresses_to_group(
                office.group_id, [office.a.id], acting_admin_id=office.admin.id,
                reason="repopulated with one member")
        assert exc.value.error_code == "PLACE_GROUP_DISSOLVED"
        db.session.rollback()
        # Hand-built one-member place — see the docstring.
        db.session.query(UserAddress).filter(UserAddress.id == office.a.id).update(
            {UserAddress.address_group_id: office.group_id}, synchronize_session=False
        )
        db.session.query(BottleLedger).filter(
            BottleLedger.address_id == office.a.id, BottleLedger.address_group_id.is_(None)
        ).update({BottleLedger.address_group_id: office.group_id}, synchronize_session=False)
        own_row = BottleBalance.query.filter_by(
            address_id=office.a.id, address_group_id=None
        ).one()
        db.session.delete(own_row)
        db.session.add(
            BottleBalance(address_group_id=office.group_id, balance=Decimal("10.00"))
        )
        db.session.commit()
        db.session.expire_all()
        assert _place(office.a.id) == Decimal("10.00")

        result = office.remove(office.a, reason="last one out", bottles_leaving=4)

        assert result["dissolved"] is True
        assert _place(office.a.id) == Decimal("10.00")           # exactly what it held
        assert _stored_total() == stored_before
        assert _group_row(office.group_id) is None
        own = _leave_rows(office.a.id)
        assert [r.quantity for r in own] == [Decimal("-4.00"), Decimal("4.00")]
        assert {r.address_group_id for r in own} == {None}       # both re-stamped home
        assert _own_ledger_sum(office.a.id) == Decimal("10.00")

    def test_a_split_plus_dissolve_is_ONE_episode_carrying_both_metadata_keys(self, db):
        """`reason` is String(500) and the dissolve marker is appended AFTER the
        truncation. An off-by-one in `[:500 - len(marker)]` either overflows the
        column (a Postgres error, invisible on SQLite) or eats the marker."""
        office = _Office(db, member_count=2)
        _seed(db, office.a, office.ua, "10")
        marker = " | place dissolved onto its last member"

        office.remove(office.a, reason="x" * 400, bottles_leaving=4)

        event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        assert len(event.reason) <= 500
        assert event.reason.startswith(f"[group {office.group_id}] xxx")
        assert event.reason.endswith(marker)
        assert event.event_metadata["dissolved_onto_address_id"] == office.b.id
        assert event.event_metadata["dissolved_inherited_bottles"] == "6.00"
        assert "dissolved_rescoped_ledger_entry_ids" in event.event_metadata
        # The split's rows key off that SAME episode id.
        assert [r.idempotency_key for r in _leave_rows(office.a.id)] == [
            f"place_leave:{office.group_id}:{event.id}:{office.a.id}:out",
            f"place_leave:{office.group_id}:{event.id}:{office.a.id}:in",
        ]
        # The full, untruncated prose survives on the ledger metadata — one
        # episode, two audit surfaces, two different reason strings.
        assert _leave_rows(office.a.id)[0].entry_metadata["reason"] == "x" * 400

    def test_a_dissolved_group_keeps_its_row_and_serves_a_memberless_detail(
        self, db, client, admin_auth_headers
    ):
        """"Tidying up the empty group" would violate `bottle_ledger`'s FK on
        Postgres while passing silently in the FK-off SQLite suite — the FK arm
        is pinned on `pg_app` further down; this is the API-visible half."""
        office = _Office(db, member_count=2)
        _seed(db, office.a, office.ua, "10")

        office.remove(office.a, reason="left", bottles_leaving=4)

        assert BottleBalance.query.filter_by(address_group_id=office.group_id).count() == 0
        assert AddressGroup.query.get(office.group_id) is not None
        detail = client.get(f"/api/v1/admin/place-groups/{office.group_id}",
                            headers=admin_auth_headers)
        assert detail.status_code == 200, detail.get_json()
        assert detail.get_json()["data"]["members"] == []
        assert detail.get_json()["data"]["place_balance"] == 0

    def test_a_three_member_place_does_NOT_dissolve_on_a_split(self, db):
        """The dissolve trigger is `len(remaining) > 1`. An off-by-one dissolves
        a live two-member office out from under its members."""
        office = _office_holding(db, "7")

        result = office.remove(office.a, reason="one of three", bottles_leaving=2)

        assert result["dissolved"] is False
        row = _group_row(office.group_id)
        assert row is not None and row.balance == Decimal("5.00")
        db.session.refresh(office.b)
        db.session.refresh(office.c)
        assert office.b.address_group_id == office.group_id
        assert office.c.address_group_id == office.group_id
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_dissolve:%")).count() == 0

    def test_the_survivors_running_snapshots_walk_its_new_timeline_after_a_split(self, db):
        """`recompute_balance_after` runs on both scopes during the dissolve, and
        the split's rows were written before it. If the recompute missed the
        split's `:out` row (which STAYS in the group scope) the customer-facing
        history would show a discontinuity."""
        office = _Office(db, member_count=2)
        _deliver(db, office.ub, office.b, "6")
        _give_back(db, office.ub, office.b, "2")
        _deliver(db, office.ub, office.b, "3")
        _seed(db, office.a, office.ua, "2")
        assert _place(office.b.id) == Decimal("9.00")

        office.remove(office.a, reason="left with four", bottles_leaving=4)

        rows = (
            BottleLedger.query.filter(*BottleScope.for_address(office.b.id).ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        running = Decimal("0.00")
        for row in rows:
            running += row.quantity
            assert row.balance_after == running
        assert rows[-1].balance_after == _place(office.b.id) == Decimal("5.00")


# --------------------------------------------------------------------------- #
# H. The HTTP layer — `bottlesLeaving` in the DELETE body
# --------------------------------------------------------------------------- #


def _remove_url(group_id, address_id):
    return f"/api/v1/admin/place-groups/{group_id}/addresses/{address_id}"


def _detail(client, headers, group_id):
    response = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


@pytest.mark.integration
@pytest.mark.e2e
class TestTheHttpBoundary:
    def test_a_split_is_forwarded_and_returned_as_a_json_number(self, db, client,
                                                                admin_auth_headers):
        """A bare Decimal renders as the STRING "2.00" through Flask's provider
        and breaks the panel's arithmetic; and axios sends DELETE bodies via
        `config.data`, which is easy to lose in a client refactor."""
        office = _office_holding(db, "5")

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 json={"reason": "left", "bottlesLeaving": 2},
                                 headers=admin_auth_headers)

        assert response.status_code == 200, response.get_json()
        body = response.get_json()["data"]
        assert isinstance(body["bottles_leaving"], (int, float))
        assert not isinstance(body["bottles_leaving"], bool)
        assert body["bottles_leaving"] == 2
        assert body["dissolved"] is False
        # The conservation pair, over HTTP.
        after = _detail(client, admin_auth_headers, office.group_id)
        assert after["place_balance"] == 3
        assert _place(office.a.id) == Decimal("2.00")
        assert after["place_balance"] + float(_place(office.a.id)) == 5

    def test_a_rejected_split_is_a_400_with_the_code_and_changes_nothing(
        self, db, client, admin_auth_headers
    ):
        """If the ValidationError arm stopped forwarding `error_code` the panel
        would fall back to "Validation failed" and the admin would never learn
        the cap. The follow-up GET proves the session is still usable after the
        route's `_rollback_db_session()`."""
        office = _office_holding(db, "3")
        before = _detail(client, admin_auth_headers, office.group_id)

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 json={"reason": "left", "bottlesLeaving": 99},
                                 headers=admin_auth_headers)

        assert response.status_code == 400, response.get_json()
        payload = response.get_json()
        assert payload["data"]["error_code"] == "PLACE_SPLIT_INVALID"
        # The envelope's `message` is the generic "Validation failed"; the
        # specific prose — including the cap the admin has to respect — travels
        # in `errors`, which is what the panel renders.
        assert payload["errors"] == [
            "bottles_leaving must be between 0 and the place balance (3.00)"
        ]
        after = _detail(client, admin_auth_headers, office.group_id)
        assert after["place_balance"] == before["place_balance"] == 3
        assert {m["address_id"] for m in after["members"]} == {
            m["address_id"] for m in before["members"]}

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_nan_and_infinity_json_literals_in_the_body_are_400s(
        self, db, client, admin_auth_headers, literal
    ):
        """The ONLY end-to-end proof that the finiteness guard is reachable from
        a real request: the admin UI's `numberOrZero()` maps NaN to 0, so the
        panel can never exercise it. Python's own JSON parser accepts all three
        literals, which is exactly how one reaches the service."""
        office = _office_holding(db, "7")

        response = client.delete(
            _remove_url(office.group_id, office.a.id),
            data=json.dumps({"reason": "r"})[:-1] + f', "bottlesLeaving": {literal}}}',
            content_type="application/json",
            headers=admin_auth_headers,
        )

        assert response.status_code == 400, response.get_json()
        assert response.get_json()["data"]["error_code"] == "PLACE_SPLIT_INVALID"
        assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 7
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")).count() == 0

    def test_missing_null_and_zero_bottles_leaving_all_default_to_no_split(
        self, db, client, admin_auth_headers
    ):
        """`body.get('bottlesLeaving')` yields None for BOTH missing and null; a
        `body.get('bottlesLeaving', 0)` refactor would turn null into a
        rejection."""
        offices = [_office_holding(db, "7") for _ in range(3)]
        bodies = [
            {"reason": "omitted"},
            {"reason": "explicit null", "bottlesLeaving": None},
            {"reason": "explicit zero", "bottlesLeaving": 0},
        ]

        for office, body in zip(offices, bodies):
            response = client.delete(_remove_url(office.group_id, office.a.id),
                                     json=body, headers=admin_auth_headers)
            assert response.status_code == 200, response.get_json()
            assert response.get_json()["data"]["bottles_leaving"] == 0
            assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 7
            assert BottleTrackingService.get_place_balance_row(office.a.id) is None

        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")).count() == 0

    def test_a_negative_zero_body_reaches_the_wire_as_minus_zero(
        self, db, client, admin_auth_headers
    ):
        """PINS CURRENT BEHAVIOUR: `-0` is accepted, writes nothing, and the
        route renders `float(Decimal('-0.00'))` as JSON `-0.0` — which reads as
        a bug to an admin looking at the panel."""
        office = _office_holding(db, "7")

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 json={"reason": "minus zero", "bottlesLeaving": -0.0},
                                 headers=admin_auth_headers)

        assert response.status_code == 200, response.get_json()
        rendered = response.get_json()["data"]["bottles_leaving"]
        assert rendered == 0
        assert math.copysign(1, rendered) == -1.0
        assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 7

    @pytest.mark.parametrize(
        "value,expected_status",
        [("3", 200), (True, 400), (False, 400), ([], 400), ({}, 400), ("", 400)],
    )
    def test_string_boolean_and_container_bodies(self, db, client, admin_auth_headers,
                                                 value, expected_status):
        """A JSON body is untyped; the boundary between "coerced" and "rejected"
        is defined only by `Decimal(str(x))` and has no schema behind it."""
        office = _office_holding(db, "7")

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 json={"reason": "typed body", "bottlesLeaving": value},
                                 headers=admin_auth_headers)

        assert response.status_code == expected_status, response.get_json()
        if expected_status == 200:
            assert response.get_json()["data"]["bottles_leaving"] == 3
            assert _place(office.a.id) == Decimal("3.00")
            assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 4
        else:
            assert response.get_json()["data"]["error_code"] == "PLACE_SPLIT_INVALID"
            assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 7

    def test_a_delete_with_no_body_at_all_fails_loudly_on_the_reason(
        self, db, client, admin_auth_headers
    ):
        """Some clients and proxies strip DELETE bodies entirely. If the body is
        dropped in transit the request must degrade to "reason is required"
        (400), not to a silent zero split and not to a 500 from a None
        subscript."""
        office = _office_holding(db, "7")

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 headers=admin_auth_headers)

        assert response.status_code == 400, response.get_json()
        assert response.get_json()["errors"] == ["reason is required"]
        assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 7
        db.session.refresh(office.a)
        assert office.a.address_group_id == office.group_id

    def test_a_whitespace_reason_is_a_400_before_the_split_runs(
        self, db, client, admin_auth_headers
    ):
        office = _office_holding(db, "7")

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 json={"reason": "   ", "bottlesLeaving": 3},
                                 headers=admin_auth_headers)

        assert response.status_code == 400, response.get_json()
        # The ROUTE's own guard fires (no `error_code`), before the service's
        # PLACE_GROUP_REASON_REQUIRED would have.
        assert response.get_json()["errors"] == ["reason is required"]
        assert response.get_json().get("data") is None
        assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 7
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")).count() == 0

    def test_the_wrong_group_id_in_the_url_404s_before_the_split_is_validated(
        self, db, client, admin_auth_headers
    ):
        """The membership precheck runs before the service call. Remove it (the
        service takes no group_id) and an admin looking at a stale panel could
        split bottles out of a place they were not editing."""
        first = _office_holding(db, "7")
        second = _office_holding(db, "4")

        for leaving in (99, 2):
            response = client.delete(_remove_url(second.group_id, first.a.id),
                                     json={"reason": "wrong group", "bottlesLeaving": leaving},
                                     headers=admin_auth_headers)
            assert response.status_code == 404, response.get_json()

        assert _detail(client, admin_auth_headers, first.group_id)["place_balance"] == 7
        assert _detail(client, admin_auth_headers, second.group_id)["place_balance"] == 4
        db.session.refresh(first.a)
        assert first.a.address_group_id == first.group_id
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")).count() == 0
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 0

    def test_a_repeated_delete_and_a_missing_address_both_404(
        self, db, client, admin_auth_headers
    ):
        """Double-submit is routine (impatient admin, retried request). The
        second DELETE must not re-run the split."""
        office = _office_holding(db, "7")

        first = client.delete(_remove_url(office.group_id, office.a.id),
                              json={"reason": "left", "bottlesLeaving": 2},
                              headers=admin_auth_headers)
        assert first.status_code == 200, first.get_json()

        repeat = client.delete(_remove_url(office.group_id, office.a.id),
                               json={"reason": "left", "bottlesLeaving": 2},
                               headers=admin_auth_headers)
        missing = client.delete(_remove_url(office.group_id, 999999),
                                json={"reason": "left", "bottlesLeaving": 2},
                                headers=admin_auth_headers)

        assert repeat.status_code == 404, repeat.get_json()
        assert missing.status_code == 404, missing.get_json()
        assert _detail(client, admin_auth_headers, office.group_id)["place_balance"] == 5
        assert _place(office.a.id) == Decimal("2.00")
        assert len(_leave_rows(office.a.id)) == 2      # ONE split, not two

    def test_a_split_that_also_dissolves_reports_dissolved_true(
        self, db, client, admin_auth_headers
    ):
        """The panel must be told the group it was editing has no members left,
        or it keeps polling a dead group and offering remove buttons for members
        that no longer exist."""
        office = _Office(db, member_count=2)
        _seed(db, office.a, office.ua, "5")

        response = client.delete(_remove_url(office.group_id, office.a.id),
                                 json={"reason": "left", "bottlesLeaving": 2},
                                 headers=admin_auth_headers)

        assert response.status_code == 200, response.get_json()
        body = response.get_json()["data"]
        assert body["bottles_leaving"] == 2
        assert body["dissolved"] is True
        after = _detail(client, admin_auth_headers, office.group_id)
        assert after["members"] == []
        assert after["place_balance"] == 0
        assert _place(office.b.id) == Decimal("3.00")
        assert _place(office.a.id) + _place(office.b.id) == Decimal("5.00")

    def test_the_detail_route_publishes_every_prefill_as_a_json_number(
        self, db, client, admin_auth_headers
    ):
        """Flask renders a bare Decimal as a STRING and the panel does
        `Math.max`/arithmetic on these. A new member key added at the service
        that is not floated at the route reintroduces "4.00" into the UI's
        arithmetic."""
        office = _Office(db)
        _seed(db, office.a, office.ua, "4")
        _seed(db, office.b, office.ub, "9")           # c's prefill is 0

        data = _detail(client, admin_auth_headers, office.group_id)

        assert isinstance(data["place_balance"], (int, float))
        assert not isinstance(data["place_balance"], bool)
        assert data["place_balance"] == 13
        published = {m["address_id"]: m for m in data["members"]}
        assert set(published) == {a.id for a in office.addrs}
        for address in office.addrs:
            member = published[address.id]
            value = member["suggested_bottles_leaving"]
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
            assert value == float(_prefill(office.group_id, address.id))
            # A place's pool has no per-coworker slice; only the suggestion.
            assert "balance" not in member
        assert published[office.c.id]["suggested_bottles_leaving"] == 0

    def test_the_split_route_enforces_manage_users_at_every_identity(
        self, db, app, client, admin_auth_headers
    ):
        """The read route is `view_users`, the write route `manage_users`. A
        copy-paste of the read decorator would let a view-only operator move
        stock. Every denial must also be a NO-OP."""
        from flask_jwt_extended import create_access_token

        office = _office_holding(db, "7")
        customer = _user(db)
        manager = _user(db, role=UserRole.MANAGER, user_type=UserType.STAFF)
        with app.app_context():
            customer_headers = {"Authorization": f"Bearer {create_access_token(identity=str(customer.id))}"}
            manager_headers = {"Authorization": f"Bearer {create_access_token(identity=str(manager.id))}"}

        anonymous = app.test_client()      # a FRESH client: the shared one carries cookies
        unauthenticated = anonymous.delete(_remove_url(office.group_id, office.a.id),
                                           json={"reason": "left", "bottlesLeaving": 2})
        as_customer = client.delete(_remove_url(office.group_id, office.a.id),
                                    json={"reason": "left", "bottlesLeaving": 2},
                                    headers=customer_headers)
        as_manager = client.delete(_remove_url(office.group_id, office.a.id),
                                   json={"reason": "left", "bottlesLeaving": 2},
                                   headers=manager_headers)

        assert unauthenticated.status_code == 401, unauthenticated.get_json()
        assert as_customer.status_code == 403, as_customer.get_json()
        assert as_manager.status_code == 403, as_manager.get_json()
        # ...and nothing moved on any of the three denials.
        assert _place(office.a.id) == Decimal("7.00")
        db.session.refresh(office.a)
        assert office.a.address_group_id == office.group_id
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")).count() == 0

        # The manager CAN read the same place (view_users), which is what makes
        # the 403 above a permission boundary rather than a broken fixture.
        assert client.get(f"/api/v1/admin/place-groups/{office.group_id}",
                          headers=manager_headers).status_code == 200
        allowed = client.delete(_remove_url(office.group_id, office.a.id),
                                json={"reason": "left", "bottlesLeaving": 2},
                                headers=admin_auth_headers)
        assert allowed.status_code == 200, allowed.get_json()


# --------------------------------------------------------------------------- #
# I. What a split does to everything AROUND it
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.e2e
class TestTheSplitInContext:
    def test_a_split_after_real_deliveries_and_returns(self, db):
        """The dev group-9 shape, built entirely from order write paths: +6 to
        A, +5 to B, -4 returned at B. The place holds 7 with ZERO drift, and B's
        pre-fill is its own 1. A scope bug in `record_bottles_delivered` /
        `record_bottles_returned` (address vs group keying) shows up here and
        nowhere else on this axis."""
        office = _Office(db)
        _deliver(db, office.ua, office.a, "6")
        _deliver(db, office.ub, office.b, "5")
        _give_back(db, office.ub, office.b, "4")
        assert _place(office.a.id) == Decimal("7.00")
        assert _group_ledger_sum(office.group_id) == Decimal("7.00")   # drift 0
        stored_before = _stored_total()

        suggestion = _prefill(office.group_id, office.b.id)
        assert suggestion == Decimal("1.00")
        result = office.remove(office.b, reason="took one crate", bottles_leaving=suggestion)

        assert result["bottles_leaving"] == Decimal("1.00")
        assert _place(office.a.id) == Decimal("6.00")
        assert _place(office.b.id) == Decimal("1.00")
        assert _stored_total() == stored_before
        # Drift stays 0 on BOTH scopes.
        assert _place(office.a.id) == _group_ledger_sum(office.group_id)
        assert _place(office.b.id) == _own_ledger_sum(office.b.id)
        out, inn = _leave_rows(office.b.id)
        assert (out.balance_after, inn.balance_after) == (Decimal("6.00"), Decimal("1.00"))

    def test_a_delivery_to_a_remaining_member_after_the_split_lands_on_the_place_only(self, db):
        """The departing address's pointer is cleared in the same transaction as
        the split. If the ordering slipped (pointer cleared before the `:out`
        half) the `:out` row would land in A's own scope and the next delivery
        would resolve the wrong place."""
        office = _office_holding(db, "7")
        office.remove(office.a, reason="left with two", bottles_leaving=2)
        stored_before = _stored_total()

        entry = _deliver(db, office.ub, office.b, "3")

        assert _place(office.b.id) == Decimal("8.00")
        assert _place(office.a.id) == Decimal("2.00")            # untouched
        assert _stored_total() == stored_before + Decimal("3.00")
        assert entry.address_group_id == office.group_id
        assert entry.address_id == office.b.id
        out, _inn = _leave_rows(office.a.id)
        assert out.address_group_id == office.group_id

    def test_a_return_at_the_departed_address_draws_down_only_its_own_scope(self, db):
        """The customer keeps returning empties after leaving the office. If A's
        scope had inherited the group's ledger filter, the return would be
        applied against the place and would over-collect from the coworkers."""
        office = _office_holding(db, "7")
        office.remove(office.a, reason="left with four", bottles_leaving=4)
        assert (_place(office.b.id), _place(office.a.id)) == (Decimal("3.00"), Decimal("4.00"))

        _give_back(db, office.ua, office.a, "4")

        assert _place(office.a.id) == Decimal("0.00")
        assert _place(office.b.id) == Decimal("3.00")
        own = (
            BottleLedger.query.filter(*BottleScope.for_address(office.a.id).ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        assert [r.balance_after for r in own] == [Decimal("4.00"), Decimal("0.00")]

    def test_remove_readd_remove_conserves_across_two_splits(self, db):
        """§7.2's absorb was written to close exactly the split → re-add
        stranding bug: a regression strands the split quantity in a deleted
        own-scope row and DESTROYS it."""
        office = _office_holding(db, "9")
        stored_before = _stored_total()

        office.remove(office.a, reason="first departure", bottles_leaving=4)
        assert _stored_total() == stored_before
        assert (_place(office.b.id), _place(office.a.id)) == (Decimal("5.00"), Decimal("4.00"))

        office.svc.add_addresses_to_group(
            office.group_id, [office.a.id], acting_admin_id=office.admin.id, reason="came back")
        assert _place(office.a.id) == Decimal("9.00")
        assert BottleBalance.query.filter_by(address_id=office.a.id).count() == 0
        assert _stored_total() == stored_before

        # 9 seeded, 4 taken out, the 4 re-stamped back in on the re-add: A's own
        # attribution over the group is whole again.
        suggestion = _prefill(office.group_id, office.a.id)
        assert suggestion == Decimal("9.00")
        office.remove(office.a, reason="second departure", bottles_leaving=suggestion)

        assert _stored_total() == stored_before
        # Named sides, not just the pair sum: 5/4 and 0/9 both sum to 9.
        assert _place(office.b.id) == Decimal("0.00")
        assert _place(office.a.id) == Decimal("9.00")
        assert _place(office.b.id) + _place(office.a.id) == Decimal("9.00")
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group").count() == 2
        assert CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").count() == 1

    def test_splitting_everything_then_re_adding_restores_the_place_exactly(self, db):
        """The absorb selector is `address_id = a AND address_group_id IS NULL`.
        Drop the IS NULL arm and the re-add drags the whole place history back
        in and double-counts it."""
        office = _office_holding(db, "7")
        stored_before = _stored_total()
        group_ledger_before = _group_ledger_sum(office.group_id)

        office.remove(office.a, reason="took it all", bottles_leaving=7)
        assert _place(office.b.id) == Decimal("0.00")

        office.svc.add_addresses_to_group(
            office.group_id, [office.a.id], acting_admin_id=office.admin.id, reason="back again")

        assert _place(office.b.id) == Decimal("7.00")
        # A is grouped again, so its "place row" IS the group's row; what must
        # be gone is its OWN-scope row, which the absorb deletes after carrying
        # the figure across.
        assert BottleBalance.query.filter_by(
            address_id=office.a.id, address_group_id=None).count() == 0
        assert _stored_total() == stored_before
        # The ±7 cancel inside the group once the `:in` half is re-stamped.
        assert _group_ledger_sum(office.group_id) == group_ledger_before
        rows = (
            BottleLedger.query.filter(*BottleScope.for_group(office.group_id).ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        running = Decimal("0.00")
        for row in rows:
            running += row.quantity
            assert row.balance_after == running          # no duplicate, no stale snapshot

    def test_removing_the_representative_member_attributes_the_split_to_its_own_owner(self, db):
        """The split deliberately uses `address.user_id` while place-level writes
        derive `resolve_place_attribution_user_id`. Unifying them "for
        consistency" would attribute a departure to a coworker who had nothing
        to do with it — and `bottle_ledger.user_id` is NOT NULL, so there is no
        null escape hatch."""
        office = _office_holding(db, "7")
        assert office.a.id == min(a.id for a in office.addrs)      # the representative

        office.remove(office.a, reason="the representative leaves", bottles_leaving=2)

        for row in _leave_rows(office.a.id):
            assert row.user_id == office.ua.id
            assert row.address_id == office.a.id
            assert row.actor_user_id == office.admin.id

        # The NEXT place-level write derives the NEW lowest-id member's owner.
        entry = BottleTrackingService().admin_adjust_balance(
            user_id=None, address_id=office.b.id, adjustment=Decimal("1"),
            actor_user_id=office.admin.id, notes="place-level, no member named")
        db.session.commit()
        assert entry.user_id == office.ub.id

    def test_a_split_touches_no_money_state(self, db):
        """§5.7: place scope creates reservations only via the cluster-keyed
        ring-3 sweep, so ungroup has none to release. A well-meaning "release the
        group's reservations here" edit would silently reverse money on every
        removal, and the COD suite would never catch it — it never removes an
        address."""
        from business_app.models.payment import (
            CashCollectionAllocation,
            CashCollectionEvent,
            Payment,
        )
        from business_app.services.cash_collection_service import CashCollectionService
        from shared.enums import PaymentMethod

        office = _office_holding(db, "7")
        cash = CashCollectionService()
        db.session.add(
            CashCollectionEvent(
                customer_id=office.ub.id, collector_user_id=office.admin.id,
                recorded_by_user_id=office.admin.id, amount=Decimal("50000.00"), currency="UZS",
                source="standalone_meeting", occurred_at=datetime.now(UTC),
                unapplied_amount=Decimal("50000.00"),
            )
        )
        order = Order(
            user_id=office.ub.id, order_number=f"ORD-COD-{next(_SEQ)}",
            status=OrderStatus.CONFIRMED, subtotal=Decimal("50000.00"),
            delivery_fee=Decimal("0.00"), discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"), total_amount=Decimal("50000.00"),
            delivery_address_id=office.b.id, payment_method=PaymentMethod.CASH,
            created_at=datetime.now(UTC),
        )
        db.session.add(order)
        db.session.flush()
        payment = cash.ensure_cod_payment_for_order(order)
        db.session.flush()
        reserved = cash.reserve_customer_prepaid_credit_for_payment(
            payment, actor_user_id=office.admin.id)
        db.session.commit()
        assert reserved == Decimal("50000.00")
        statement_before = cash.get_place_cod_statement(office.group_id)
        payments_before = {p.id: (p.amount, p.status) for p in Payment.query.all()}

        office.remove(office.a, reason="left with two", bottles_leaving=2)

        allocation = CashCollectionAllocation.query.filter_by(
            payment_id=payment.id, allocation_mode="prepaid_reservation").one()
        assert allocation.reversed_at is None
        assert allocation.allocated_amount == Decimal("50000.00")
        # Every MONEY figure on the place's COD statement is unchanged.
        # `member_count` legitimately drops from 3 to 2 — one coworker really did
        # leave the place — so it is compared separately rather than pretended
        # away.
        after = cash.get_place_cod_statement(office.group_id)
        assert {k: v for k, v in after.items() if k != "member_count"} == {
            k: v for k, v in statement_before.items() if k != "member_count"
        }
        assert (statement_before["member_count"], after["member_count"]) == (3, 2)
        assert {p.id: (p.amount, p.status) for p in Payment.query.all()} == payments_before

    def test_a_split_does_not_touch_the_owners_OTHER_place(self, db):
        """Several helpers key off `user_id`. A user-keyed rather than
        place-keyed write would bleed across an owner's places — the exact class
        of bug the re-key exists to eliminate."""
        svc = CustomerLinkService()
        admin = _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)
        owner, mate_one, mate_two = _user(db), _user(db), _user(db)
        here, there = _addr(db, owner.id, title="office"), _addr(db, owner.id, title="warehouse")
        # Three members in the place being edited, so §7.3's dissolve does not
        # fire and this stays a test about CROSS-PLACE isolation.
        first = svc.create_place_group(
            [here.id, _addr(db, mate_one.id).id, _addr(db, mate_two.id).id],
            acting_admin_id=admin.id, reason="office")
        second = svc.create_place_group(
            [there.id, _addr(db, mate_two.id).id, _addr(db, mate_one.id).id],
            acting_admin_id=admin.id, reason="warehouse")
        _seed(db, here, owner, "9")
        _seed(db, there, owner, "4")
        untouched_before = (
            _place(there.id),
            _group_ledger_sum(second.id),
            _prefill(second.id, there.id),
            CustomerLinkEvent.query.filter(
                CustomerLinkEvent.reason.like(f"[group {second.id}]%")).count(),
        )
        stored_before = _stored_total()

        svc.remove_address_from_group(
            here.id, acting_admin_id=admin.id, reason="left the office", bottles_leaving=5)

        assert (
            _place(there.id),
            _group_ledger_sum(second.id),
            _prefill(second.id, there.id),
            CustomerLinkEvent.query.filter(
                CustomerLinkEvent.reason.like(f"[group {second.id}]%")).count(),
        ) == untouched_before
        assert _place(here.id) == Decimal("5.00")
        assert _group_ledger_sum(first.id) == Decimal("4.00")
        assert _stored_total() == stored_before

    def test_a_split_mid_delivery_resolves_the_scope_at_WRITE_time(self, db):
        """PINS A REAL-WORLD SURPRISE. Membership is resolved lazily by
        `resolve_scope` at write time, so an order already in flight to a
        departing address lands wherever that address belongs when the driver
        closes the delivery — NOT where it belonged when the order was placed.
        This is the most likely way for a split to produce a number nobody can
        explain, so the behaviour is pinned rather than assumed."""
        office = _office_holding(db, "7")
        in_flight = _order(db, office.ua, office.a)
        in_flight.status = OrderStatus.OUT_FOR_DELIVERY
        db.session.commit()

        office.remove(office.a, reason="left mid-delivery", bottles_leaving=2)
        stored_before = _stored_total()

        entry = BottleTrackingService().record_bottles_delivered(
            in_flight.id, office.ua.id, office.a.id, Decimal("3"))
        db.session.commit()

        # It lands on A's OWN scope, because that is where A is now.
        assert entry.address_group_id is None
        assert _place(office.a.id) == Decimal("5.00")
        assert _place(office.b.id) == Decimal("5.00")
        assert _stored_total() == stored_before + Decimal("3.00")

    def test_a_grouped_address_with_a_STALE_own_scope_row_breaks_the_split_pair(self, db):
        """BUG PIN — reported, not fixed.

        Nothing on the removal path asserts the departing address has no
        own-scope balance row. The join path (`absorb_address_into_group`) is the
        only thing that deletes one, so any address grouped by a script, a
        fixture or a pre-Plan-C migration carries this shape into production.
        The `:in` half calls `get_or_create_balance` on the address scope and
        ADDS onto the stale figure, so the departing address ends with
        stale + leaving while the place only loses `leaving`.

        The hand-inserted row IS the subject of this test. Σ bottle_balances is
        unchanged (the stale row was already counted), but the LOCAL pair —
        `place_after + departed == place_before` — is broken by exactly the
        stale figure, which is what an admin would see.
        """
        office = _office_holding(db, "7")
        db.session.add(
            BottleBalance(address_id=office.a.id, address_group_id=None, balance=Decimal("5.00"))
        )
        db.session.commit()
        stored_before = _stored_total()
        place_before = _place(office.a.id)
        assert place_before == Decimal("7.00")      # the stale row is invisible here
        # The shape is already a REPORTED violation before the removal runs —
        # which is what makes "the removal path does not check for it" the defect
        # rather than the data.
        from business_app.tasks.customer_link_tasks import (
            reconcile_customer_link_invariants,
        )

        stale_row = BottleBalance.query.filter_by(
            address_id=office.a.id, address_group_id=None).one()
        assert stale_row.id in reconcile_customer_link_invariants()[
            "stranded_address_balances"]

        office.remove(office.a, reason="left with three", bottles_leaving=3)

        assert _place(office.b.id) == Decimal("4.00")
        assert _place(office.a.id) == Decimal("8.00")        # 5 stale + 3 that left
        # THE BUG, stated as the pair it breaks:
        assert _place(office.b.id) + _place(office.a.id) != place_before
        assert _place(office.b.id) + _place(office.a.id) == place_before + Decimal("5.00")
        # The global figure is unchanged only because the stale 5 was always in it.
        assert _stored_total() == stored_before
        inn = _leave_rows(office.a.id)[1]
        assert inn.balance_after == Decimal("8.00")


@pytest.mark.integration
@pytest.mark.e2e
def test_global_conservation_over_a_long_mixed_sequence(db):
    """Two places, five addresses, three customers, one scripted day.

    Individual tests each assert a LOCAL pair; only a running global ledger
    catches a bug that moves a bottle from one place into a third scope nobody
    is looking at. The invariant asserted after every step is
    `Δ Σ bottle_balances == Δ Σ COUPLED bottle_ledger.quantity` — membership
    edits move neither, deliveries/returns/adjustments move both by the same
    amount, and only a balance-DECOUPLED writer (or `reconcile_balance`) may
    break it.
    """
    svc = CustomerLinkService()
    admin = _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)
    one, two, three = _user(db), _user(db), _user(db)
    a1, a2 = _addr(db, one.id, "desk"), _addr(db, one.id, "lab")
    b1 = _addr(db, two.id)
    c1, c2 = _addr(db, three.id, "front"), _addr(db, three.id, "back")
    first = svc.create_place_group([a1.id, b1.id, c1.id], acting_admin_id=admin.id,
                                   reason="floor 1")
    second = svc.create_place_group([a2.id, c2.id], acting_admin_id=admin.id, reason="floor 2")

    history = []

    def step(label, fn):
        stored_before, coupled_before = _stored_total(), _coupled_total()
        fn()
        stored_after, coupled_after = _stored_total(), _coupled_total()
        history.append((label, stored_after - stored_before, coupled_after - coupled_before))
        assert stored_after - stored_before == coupled_after - coupled_before, (
            f"conservation broke at step {label!r}"
        )

    step("deliver 6 to floor1 via a1", lambda: _deliver(db, one, a1, "6"))
    step("deliver 5 to floor1 via b1", lambda: _deliver(db, two, b1, "5"))
    step("return 4 at floor1 via b1", lambda: _give_back(db, two, b1, "4"))
    step("adjust floor2 by +8", lambda: _seed(db, a2, one, "8"))
    step("deliver 2 to floor2 via c2", lambda: _deliver(db, three, c2, "2"))

    assert _place(a1.id) == Decimal("7.00")
    assert _place(a2.id) == Decimal("10.00")

    def rejected():
        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(b1.id, acting_admin_id=admin.id,
                                          reason="too many", bottles_leaving=99)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"

    step("REJECTED split of 99 from floor1", rejected)
    step("legal split of 1 out of floor1", lambda: svc.remove_address_from_group(
        b1.id, acting_admin_id=admin.id, reason="took one", bottles_leaving=1))
    step("re-add b1 to floor1", lambda: svc.add_addresses_to_group(
        first.id, [b1.id], acting_admin_id=admin.id, reason="came back"))
    step("second split of 2 out of floor1", lambda: svc.remove_address_from_group(
        b1.id, acting_admin_id=admin.id, reason="took two", bottles_leaving=2))
    step("dissolve floor2 by removing c2", lambda: svc.remove_address_from_group(
        c2.id, acting_admin_id=admin.id, reason="floor 2 closed"))

    # Every membership operation moved NOTHING globally.
    membership_steps = [d for label, d, _ in history if "split" in label or "add" in label
                        or "dissolve" in label]
    assert membership_steps == [Decimal("0.00")] * 5
    # NAMED per-place figures, then the pair sums. A bug that moved floor1's
    # bottles onto the wrong side of its own split — or onto floor2 — conserves
    # both the global stored total and these two sums, so the sums alone are
    # blind to exactly the class of scope-attribution bug this file hunts.
    assert _place(a1.id) == Decimal("5.00")     # floor1 after two splits out
    assert _place(b1.id) == Decimal("2.00")     # b1's own scope, twice departed
    assert _place(a2.id) == Decimal("10.00")    # floor2 dissolved onto a2
    assert _place(c2.id) == Decimal("0.00")     # ...and c2 left with nothing
    assert _place(a1.id) + _place(b1.id) == Decimal("7.00")
    assert _place(a2.id) + _place(c2.id) == Decimal("10.00")
    assert _stored_total() == Decimal("17.00")

    # A reconcile on a place whose ledger already explains its figure is a
    # no-op: nothing to destroy, and the invariant survives.
    result = BottleTrackingService().reconcile_balance(a1.id)
    assert result["corrected"] is False
    assert result["discrepancy"] == 0
    assert _stored_total() == Decimal("17.00")


# --------------------------------------------------------------------------- #
# J. REAL POSTGRES — the FK, and the two races the SQLite suite cannot see
#
# Everything above runs on in-memory SQLite, where FOREIGN KEYS are OFF and
# `with_for_update()` is a NO-OP. Neither the "the memberless group row cannot
# be deleted" claim nor either concurrency property is testable there: a
# passing SQLite test proves nothing about FK integrity or locking. These run
# on `pg_app`/`pg_db` — a fresh, fully-migrated Postgres database per test.
#
# The two races are made DETERMINISTIC rather than raced: the interfering
# session is started and JOINED from inside a hook on the production call the
# scenario needs to interleave with, so the ordering is fixed and the test can
# neither flake nor hang (the second session carries a `lock_timeout`, so a
# regression that made it block surfaces as an error, not as a hung suite).
# --------------------------------------------------------------------------- #


def _is_deadlock(exc) -> bool:
    """True for a Postgres 40P01 DeadlockDetected, wherever SQLAlchemy wrapped it.

    A lock TIMEOUT (55P03) is an acceptable outcome under this harness — it is
    how a deliberate block is made visible instead of hanging the suite. A
    DEADLOCK never is: it means the ladder has a cycle in it.
    """
    return getattr(getattr(exc, "orig", None), "pgcode", None) == "40P01"


def _in_a_second_session(pg_app, work, *, lock_timeout_ms=4000):
    """Run `work()` to completion in a SEPARATE Postgres session, then join it.

    The CALLER's transaction stays open across the call, which is the whole
    point: this is how "another admin/driver committed while I was mid-removal"
    is reproduced without a barrier and without a sleep. Returns
    {'value': ...} or {'error': exc}; `work` must reference ids only, never ORM
    instances belonging to the caller's session.
    """
    outcome = {}

    def worker():
        with pg_app.app_context():
            from business_app import db as other

            try:
                # A regression that made this session BLOCK on a row the caller
                # holds must fail loudly instead of hanging the suite.
                other.session.execute(text(f"SET lock_timeout = '{lock_timeout_ms}ms'"))
                outcome["value"] = work()
                other.session.commit()
            except BaseException as exc:  # noqa: BLE001 - re-asserted by the caller
                other.session.rollback()
                outcome["error"] = exc
            finally:
                other.session.remove()

    thread = threading.Thread(target=worker, name="second-session")
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive(), "the second session never finished — a lock was held"
    return outcome


def _fire_once(monkeypatch, owner, name, hook):
    """Patch `owner.name` so `hook(original, *a, **kw)` runs on the FIRST call only.

    Without the once-guard the interfering session — which re-enters the same
    production method — would recurse into the hook and spawn threads forever.
    """
    original = getattr(owner, name)
    state = {"fired": False}

    def patched(*args, **kwargs):
        if state["fired"]:
            return original(*args, **kwargs)
        state["fired"] = True
        return hook(original, *args, **kwargs)

    monkeypatch.setattr(owner, name, staticmethod(patched))
    return state


def _pg_office(pg_db, qty, member_count=3):
    office = _Office(pg_db, member_count=member_count)
    if Decimal(str(qty)) != 0:
        _seed(pg_db, office.a, office.ua, qty)
    assert _place(office.a.id) == Decimal(str(qty)).quantize(Decimal("0.01"))
    return office


@pytest.mark.integration
@pytest.mark.e2e
class TestOnRealPostgres:
    def test_the_memberless_group_row_is_HELD_by_the_ledgers_foreign_key(self, pg_app, pg_db):
        """§7.3's "the AddressGroup row is KEPT" is a FK fact, not a preference.

        A "tidy up the empty group" change would pass silently on the FK-off
        SQLite suite and raise `ForeignKeyViolation` in production, because every
        DEPARTED member's ledger rows still carry `address_group_id`. Proven by
        attempting the delete on a real database.
        """
        office = _pg_office(pg_db, "10", member_count=2)

        office.remove(office.a, reason="left", bottles_leaving=4)

        assert BottleBalance.query.filter_by(address_group_id=office.group_id).count() == 0
        assert AddressGroup.query.get(office.group_id) is not None
        # This is what holds the FK: the departed member's `:out` half is still
        # stamped with the group it left.
        held_by = BottleLedger.query.filter_by(address_group_id=office.group_id).all()
        assert held_by, "no ledger row references the group — the FK argument is void"

        with pytest.raises(IntegrityError) as exc:
            pg_db.session.execute(
                text("DELETE FROM address_groups WHERE id = :g"), {"g": office.group_id}
            )
            pg_db.session.flush()
        assert "bottle_ledger" in str(exc.value).lower()
        pg_db.session.rollback()
        assert AddressGroup.query.get(office.group_id) is not None

    def test_a_departed_address_holding_split_bottles_cannot_be_deleted(self, pg_app, pg_db):
        """The two guards have to hand off cleanly: the place fence stops being
        applicable the moment the address is ungrouped, and the FK takes over.

        On SQLite with FKs off the delete would appear to succeed and silently
        orphan a `bottle_balances` row and two ledger rows — an address holding
        4 bottles would simply vanish. All THREE delete entry points are driven.
        """
        from flask_jwt_extended import create_access_token

        office = _pg_office(pg_db, "7")
        _addr(pg_db, office.ua.id, title="Home")     # so "only address" never fires
        # `manager_or_higher_required` reads the role off the JWT CLAIMS, not the
        # DB row, so the admin token has to carry it.
        admin_token = create_access_token(
            identity=str(office.admin.id), additional_claims={"role": UserRole.ADMIN.value}
        )
        owner_token = create_access_token(identity=str(office.ua.id))
        client = pg_app.test_client()

        office.remove(office.a, reason="left with four", bottles_leaving=4)
        assert _place(office.a.id) == Decimal("4.00")

        # The place fence must NOT fire any more — A is ungrouped.
        CustomerLinkService.assert_address_not_in_place_group(office.a.id)

        customer = client.delete(
            f"/api/v1/addresses/{office.a.id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        through_auth = client.delete(
            f"/api/v1/auth/addresses/{office.a.id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        admin = client.delete(
            f"/api/v1/admin/users/{office.ua.id}/addresses/{office.a.id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        for response in (customer, through_auth, admin):
            assert response.status_code == 400, response.get_json()
            assert "referenced by existing records" in json.dumps(response.get_json())
        # ...and the 4 bottles are still where the split put them.
        pg_db.session.expire_all()
        assert UserAddress.query.get(office.a.id) is not None
        assert _place(office.a.id) == Decimal("4.00")
        assert len(_leave_rows(office.a.id)) == 2

    # -- The cap's TOCTOU ---------------------------------------------------- #

    @staticmethod
    def _race_a_return_against_the_cap(pg_app, pg_db, monkeypatch):
        """Place holding 7.00; a return of the whole 7 commits in ANOTHER session
        between the cap being read and the split being written.

        Returns (office, Σ bottle_balances before the race, race) where `race`
        carries BOTH sessions' outcomes.

        NOTHING IS ASSERTED HERE, deliberately, and that is not tidiness — it is
        what makes the strict xfail below able to xpass. Every plausible fix
        changes one of the two outcomes this helper used to assert: reading the
        cap off the LOCKED row makes the interfering return BLOCK (and abort on
        its `lock_timeout`), and re-validating after the lock makes the split
        RAISE. An assertion on either one inside the shared helper turns the
        xfail into a permanently-red test that can never force its own marker
        off, which is precisely the failure this file exists to prevent.
        """
        office = _pg_office(pg_db, "7")
        stored_before = _stored_total()
        member_b_user_id, member_b_address_id = office.ub.id, office.b.id
        race = {}

        def interfere():
            order = Order(
                user_id=member_b_user_id,
                order_number=f"ORD-RACE-{next(_SEQ)}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("0.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                delivery_address_id=member_b_address_id,
                created_at=datetime.now(UTC),
            )
            _db.session.add(order)
            _db.session.flush()
            return BottleTrackingService().record_bottles_returned(
                member_b_user_id, member_b_address_id, Decimal("7"), order_id=order.id
            )

        def hook(original, address, bottles_leaving):
            cap_basis = original(address, bottles_leaving)      # the UNLOCKED read
            race["interference"] = _in_a_second_session(pg_app, interfere)
            return cap_basis

        _fire_once(monkeypatch, CustomerLinkService, "_validated_bottles_leaving", hook)
        try:
            race["removal"] = office.remove(office.a, reason="took all seven",
                                            bottles_leaving=7)
        except ValidationError as exc:
            # Only reachable once the cap is re-checked under the lock — i.e.
            # once the bug is fixed by the "refuse it" shape rather than the
            # "serialise it" one.
            race["removal_error"] = exc
            pg_db.session.rollback()
        pg_db.session.expire_all()
        return office, stored_before, race

    def test_a_return_committed_under_the_unlocked_cap_read_drives_the_place_NEGATIVE(
        self, pg_app, pg_db, monkeypatch
    ):
        """UPDATED: every number below changed when the cap moved under the lock.

        This used to pin the cap's TOCTOU. `_validated_bottles_leaving` read the
        place balance with a plain, UNLOCKED `get_place_balance` SUM and only
        `_split_bottles_out_of_place` took the row FOR UPDATE, afterwards — so
        under READ COMMITTED a return committing in between drove the place BELOW
        zero through the one code path whose entire purpose is
        `cap = max(0, place)`. BOTH sessions succeeded, nothing blocked, and the
        place ended at -7.00 with the nightly sweep reporting a negative place.

        The removal now takes `address_groups(G)` and every member's `addresses`
        row before any bottle work, and the group's `bottle_balances` row before
        the cap is read. The interfering return at member B therefore blocks at
        RUNG 1 and is cancelled by its own `lock_timeout`; the cap is evaluated
        against a figure nothing can move underneath it. Serialising it is one of
        the two shapes the invariant test below accepts.
        """
        office, stored_before, race = self._race_a_return_against_the_cap(
            pg_app, pg_db, monkeypatch)
        assert stored_before == Decimal("7.00")
        # The interfering return is now FENCED OUT, not raced.
        assert "error" in race["interference"], race["interference"]
        assert "lock timeout" in str(race["interference"]["error"]).lower()
        assert "removal_error" not in race, race.get("removal_error")
        assert race["removal"]["bottles_leaving"] == Decimal("7.00")

        # The return wrote nothing, so the split legitimately emptied a place
        # that really did still hold seven.
        assert _stored_total() == stored_before
        assert _place(office.b.id) == Decimal("0.00")
        assert _place(office.a.id) == Decimal("7.00")
        out, inn = _leave_rows(office.a.id)
        assert (out.quantity, inn.quantity) == (Decimal("-7.00"), Decimal("7.00"))
        assert out.balance_after == Decimal("0.00")

        from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants

        report = reconcile_customer_link_invariants()
        assert report["negative_place_balances"] == []

    def test_the_split_must_never_leave_the_place_below_zero(self, pg_app, pg_db, monkeypatch):
        """FIXED — the xfail is gone.

        WAS: the split's cap was read by an UNLOCKED `get_place_balance` SUM in
        `_validated_bottles_leaving` and the row was only locked FOR UPDATE
        afterwards, inside `_split_bottles_out_of_place`. Under READ COMMITTED a
        delivery/return/adjustment committing in between made the cap stale, so
        the split could take more than the place held and drove it NEGATIVE —
        through the one code path whose whole purpose is `cap = max(0, place)`.

        NOW the group's `bottle_balances` row is acquired in the removal's header,
        just before `_validated_bottles_leaving`, so the cap is validated under
        the very lock that guards the figure it caps.

        Stated as the INVARIANT and nothing else, so that it xpasses under
        EITHER fix shape: serialise the two sessions (the return blocks, the
        split legitimately empties a place that still holds 7 → 0.00) or
        re-check the cap under the lock (the split is refused → the place keeps
        what the return left it). Asserting "the split succeeded AND the place
        is >= 0" would be red under both, and a strict xfail that can only ever
        stay red never forces its own marker off.
        """
        office, _stored_before, _race = self._race_a_return_against_the_cap(
            pg_app, pg_db, monkeypatch)

        assert _place(office.b.id) >= Decimal("0.00"), (
            f"the place went to {_place(office.b.id)} through the capped split path"
        )

    # -- The dissolve's unlocked member count -------------------------------- #

    @staticmethod
    def _race_two_removals(pg_app, pg_db, monkeypatch):
        """Two admins remove two DIFFERENT members of a three-member place, each
        with the default `bottles_leaving`. T1's member count is taken before T2
        clears its pointer, and T2's before T1 commits — so each sees two
        remaining. Returns (office, seen) where `seen` carries both removals'
        outcomes and `seen['stored_before']` is Σ bottle_balances before the race.
        """
        office = _pg_office(pg_db, "9")
        admin_id, second_address_id = office.admin.id, office.b.id
        seen = {"stored_before": _stored_total()}

        def interfere():
            return CustomerLinkService().remove_address_from_group(
                second_address_id, acting_admin_id=admin_id, reason="the other admin"
            )

        def hook(original, **kwargs):
            # T1 counts the remaining members FIRST (it sees B and C, so it does
            # not dissolve), and only THEN does T2 run to completion.
            seen["first_dissolved"] = original(**kwargs)
            seen["second"] = _in_a_second_session(pg_app, interfere)
            return seen["first_dissolved"]

        _fire_once(monkeypatch, CustomerLinkService, "_dissolve_if_last_member", hook)
        office.remove(office.a, reason="one admin")
        pg_db.session.expire_all()
        return office, seen

    def test_two_concurrent_removals_leave_a_ONE_member_place_UNDISSOLVED(
        self, pg_app, pg_db, monkeypatch
    ):
        """UPDATED: every number below changed. This used to pin the damage.

        The old pin: `_dissolve_if_last_member` counted the remaining members
        with a plain, UNLOCKED SELECT, so two overlapping removals from a
        three-member place each saw two members left, NEITHER dissolved, and the
        place was left with exactly ONE member — the state `create_place_group`
        refuses to build and §7.3 exists to prevent. No bottles were lost, the
        survivor still resolved to the group, and `orphaned_place_balances` (which
        only looks for group rows with NO members) did not flag it. The damage
        was a broken structural invariant nothing would ever clean up.

        The second removal now blocks on `address_groups(G)` — RUNG 0, taken
        before either transaction reads a member count — and is cancelled by its
        own `lock_timeout` under this harness. The place is left with TWO members
        and never passes through an illegal state; production, which sets no
        `lock_timeout`, simply queues the second admin behind the first.
        """
        office, seen = self._race_two_removals(pg_app, pg_db, monkeypatch)
        stored_before = seen["stored_before"]
        assert stored_before == Decimal("9.00")

        assert "error" in seen["second"], seen["second"]
        assert "lock timeout" in str(seen["second"]["error"]).lower(), seen["second"]
        assert seen["first_dissolved"] is False
        members = sorted(
            m.id for m in UserAddress.query.filter_by(address_group_id=office.group_id).all()
        )
        assert members == sorted([office.b.id, office.c.id]), (
            "the second removal wrote something despite being cancelled"
        )
        row = _group_row(office.group_id)
        assert row is not None and row.balance == Decimal("9.00")
        # Membership edits moved nothing, so the bottles are all still there.
        assert _stored_total() == stored_before
        assert _place(office.c.id) == Decimal("9.00")
        assert _place(office.a.id) == Decimal("0.00")

        from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants

        report = reconcile_customer_link_invariants()
        assert office.group_id not in report["orphaned_place_balances"]

    def test_a_removal_that_leaves_one_member_dissolves_even_under_a_concurrent_removal(
        self, pg_app, pg_db, monkeypatch
    ):
        """FIXED — the xfail is gone.

        WAS: `_dissolve_if_last_member` counted the remaining members with an
        UNLOCKED SELECT, after the caller had flushed its own pointer clear. Two
        overlapping removals from a three-member place each saw two remaining
        under READ COMMITTED, so neither dissolved and a ONE-member place group
        survived — precisely the state `create_place_group` refuses to build and
        §7.3 exists to prevent.

        NOW the count is a re-read of a member set pinned by rung 0 + rung 1.

        §7.3's rule — "the LAST member out takes the place's history" — must
        hold when two admins work the same office.

        Asserted as the STRUCTURAL INVARIANT ("a place group is never left with
        exactly one member") rather than as one particular happy ending, because
        the fix does not produce that ending under this harness: any lock taken
        before the member count makes the SECOND session block on the first,
        which its `lock_timeout` then aborts — so the fixed system leaves TWO
        members and a place that never went through an illegal state. Pinning
        "both removals landed and the group is empty" would keep this red after
        the bug was fixed, and a strict xfail that can only ever stay red never
        forces its own marker off.
        """
        office, seen = self._race_two_removals(pg_app, pg_db, monkeypatch)

        remaining = UserAddress.query.filter_by(address_group_id=office.group_id).count()
        assert remaining != 1, (
            "a ONE-member place group survived — the state create_place_group refuses "
            "to build (§7.3)"
        )
        # Whatever the outcome, the bottles are all still at the place they
        # belong to and nothing was minted by the membership edits.
        assert _stored_total() == seen["stored_before"] == Decimal("9.00")
        if remaining == 0:
            assert _group_row(office.group_id) is None
            assert _place(office.c.id) == Decimal("9.00")
        else:
            assert _group_row(office.group_id).balance == Decimal("9.00")

    # -- Lock ordering: the split vs the join, on the same pair --------------- #

    def test_a_split_and_a_JOIN_of_the_same_pair_do_not_deadlock(
        self, pg_app, pg_db, monkeypatch
    ):
        """UPDATED: the MECHANISM changed, and that is the whole point of the fix.

        This test used to rest on the membership FENCE: the join's late-create
        branch took the group's balance row LAST, so ordering alone did not rule
        out an ABBA cycle, and what saved it was that a removal reads the address
        as GROUPED while a join requires it UNGROUPED — "of two concurrent
        transactions on one address exactly one passes its fence". THAT ARGUMENT
        IS FALSE for two joins (under READ COMMITTED both read the same
        pre-image, so a read-based test can never serialise anything) and it is
        REPLACED, not repaired.

        Deadlock-freedom now rests on ORDERING ALONE: every transaction takes a
        prefix of `address_groups` -> `addresses` (ascending id, one statement)
        -> `bottle_balances(group)` -> `bottle_balances(address)`. So the join no
        longer "fails its fence and returns" — it BLOCKS at rung 0/1 behind the
        removal, which is correct and is what makes the outcome deterministic. The
        second session's 4s `lock_timeout` turns that block into a visible error
        here; production, which sets none, queues the second admin and then
        rejects the join on a fence that is now TRUE AS A CONSEQUENCE of the lock
        rather than as its justification.

        What must not happen — and is what this test still guards — is a 40P01.
        """
        office = _pg_office(pg_db, "7")
        group_id, joining_address_id, admin_id = office.group_id, office.a.id, office.admin.id

        def interfere():
            return CustomerLinkService().add_addresses_to_group(
                group_id, [joining_address_id], acting_admin_id=admin_id, reason="rejoin"
            )

        def hook(original, **kwargs):
            original(**kwargs)                       # both rows are now FOR UPDATE
            outcome = _in_a_second_session(pg_app, interfere)
            hook.outcome = outcome

        _fire_once(monkeypatch, CustomerLinkService, "_split_bottles_out_of_place", hook)
        stored_before = _stored_total()

        result = office.remove(office.a, reason="left with two", bottles_leaving=2)
        pg_db.session.expire_all()

        error = hook.outcome.get("error")
        assert error is not None, hook.outcome
        assert not _is_deadlock(error), f"a 40P01 formed between a split and a join: {error!r}"
        assert "lock timeout" in str(error).lower(), repr(error)
        # The split committed normally and conserved.
        assert result["bottles_leaving"] == Decimal("2.00")
        assert _place(office.b.id) == Decimal("5.00")
        assert _place(office.a.id) == Decimal("2.00")
        assert _stored_total() == stored_before
        # The rejected join wrote nothing: one episode, no add event.
        assert CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").count() == 0
        assert len(_leave_rows(office.a.id)) == 2

    # -- Where the stale own-scope row actually comes from -------------------- #

    def test_a_delivery_CANNOT_commit_during_a_JOIN_so_no_stale_row_is_stranded(
        self, pg_app, pg_db, monkeypatch
    ):
        """UPDATED: this used to be the LIVE PRODUCER of a stranded own-scope row.

        THE OLD MECHANISM, in three lines of production code.
        `_absorb_joiners_into_group` step 1 set the joiner's `address_group_id`
        and FLUSHED it — flushed, not committed — so under READ COMMITTED a
        concurrent delivery's `resolve_scope` still read the joiner as UNGROUPED
        and booked onto its OWN scope. `absorb_address_into_group` then selected
        that own row `FOR UPDATE`, but a row inserted by an uncommitted
        transaction is INVISIBLE and `FOR UPDATE` does not lock rows it cannot
        see — so the absorb found nothing, carried 0.00 across, and the delivery
        committed its brand-new address-keyed row afterwards. The joiner ended up
        GROUPED and holding a balance row of its own, invisible to
        `get_place_balance` and to every operational reader; §7.1's split then
        ADDED the departing quantity on top of the stale figure. Σ
        `bottle_balances` never moved, which is why only a scope-attribution
        assertion could see any of it.

        The chain is broken at its first link. `_load_addresses` takes
        `addresses(joiner)` FOR NO KEY UPDATE before any bottle work, and the
        delivery takes the same row FOR SHARE BEFORE it resolves its scope — so
        the delivery cannot read the joiner's membership, let alone commit
        against it, until the join has finished. It is cancelled by its own
        `lock_timeout` here; in production it waits and then resolves to the
        place.

        Not reproducible on SQLite at all — there is no second session there.
        """
        svc = CustomerLinkService()
        admin = _user(pg_db, role=UserRole.ADMIN, user_type=UserType.STAFF)
        users = [_user(pg_db) for _ in range(3)]
        addrs = [_addr(pg_db, u.id) for u in users]
        joiner_id, joiner_user_id = addrs[0].id, users[0].id
        group = svc.create_place_group(
            [addrs[1].id, addrs[2].id], acting_admin_id=admin.id, reason="the office"
        )
        _seed(pg_db, addrs[1], users[1], "10")
        assert _place(addrs[1].id) == Decimal("10.00")
        stored_before = _stored_total()

        def interfere():
            order = Order(
                user_id=joiner_user_id,
                order_number=f"ORD-JOINRACE-{next(_SEQ)}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("0.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                delivery_address_id=joiner_id,
                created_at=datetime.now(UTC),
            )
            _db.session.add(order)
            _db.session.flush()
            return BottleTrackingService().record_bottles_delivered(
                order.id, joiner_user_id, joiner_id, Decimal("5")
            )

        def hook(original, *args, **kwargs):
            moved = original(*args, **kwargs)          # the absorb saw nothing
            hook.outcome = _in_a_second_session(pg_app, interfere)
            return moved

        _fire_once(monkeypatch, BottleTrackingService, "absorb_address_into_group", hook)
        svc.add_addresses_to_group(
            group.id, [joiner_id], acting_admin_id=admin.id, reason="new desk"
        )
        pg_db.session.expire_all()

        error = hook.outcome.get("error")
        assert error is not None, hook.outcome
        assert not _is_deadlock(error), repr(error)
        assert "lock timeout" in str(error).lower(), repr(error)

        # NO own-scope row survives under a grouped address.
        assert BottleBalance.query.filter_by(
            address_id=joiner_id, address_group_id=None).first() is None
        assert UserAddress.query.get(joiner_id).address_group_id == group.id

        from business_app.tasks.customer_link_tasks import (
            reconcile_customer_link_invariants,
        )

        assert reconcile_customer_link_invariants()["stranded_address_balances"] == []
        assert _place(joiner_id) == Decimal("10.00")
        assert _place(addrs[1].id) == Decimal("10.00")
        assert _stored_total() == stored_before
        stored_after_join = _stored_total()

        # ...and §7.1's split now has nothing stale to compound.
        result = svc.remove_address_from_group(
            joiner_id, acting_admin_id=admin.id, reason="left with three", bottles_leaving=3
        )
        pg_db.session.expire_all()

        assert result["bottles_leaving"] == Decimal("3.00")
        assert _place(addrs[1].id) == Decimal("7.00")
        assert _place(joiner_id) == Decimal("3.00")
        assert _place(addrs[1].id) + _place(joiner_id) == Decimal("10.00")
        assert _stored_total() == stored_after_join
