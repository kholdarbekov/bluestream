"""GLOBAL CONSERVATION across every place mutation combination (e2e).

ONE property, swept over every shape a place can be in and every mutation that
can touch it:

    Σ over ALL `bottle_balances.balance` moves by EXACTLY the sum of the
    BALANCE-COUPLED ledger quantities that mutation appended — and by zero for
    a mutation that appends none.

and its partner, which is what makes the first one meaningful:

    Σ over ALL `bottle_ledger.quantity` moves by COUPLED + DECOUPLED, where
    DECOUPLED is exactly the `merge_backfill:`-keyed rows
    (`BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES`) — the single sanctioned
    balance-decoupled writer in the tree.

WHY BOTH SIDES. Asserting only "the balance moved by 6" passes against code
that mints 6 bottles from nothing. Asserting only "a +6 entry exists" passes
against code that writes the entry and never moves the balance. The PAIR is the
invariant; every probe below asserts the pair.

⚠️ WHAT A GREEN GLOBAL ORACLE DOES **NOT** PROVE — read this before trusting it.

Both sums above are taken over the WHOLE database, so they are INVARIANT under
every defect that conserves bottles globally while corrupting WHICH PLACE holds
them. Concretely, this file's global pair is blind to:

  (a) PURE SCOPE TRANSFERS. Moving 3 bottles out of place P into place Q leaves
      ΔΣbalances == 0, Σcoupled == 0 and the row count unchanged. Both fine bugs
      pinned in section 2 are this exact shape and `assert_moved` passes on both.
      CLOSED by `ConservationReport.assert_scope_deltas`, whose own known-bad
      control is `test_the_global_pair_is_BLIND_to_a_pure_ATTRIBUTION_defect`.
      Any test whose world contains more than one place MUST assert it.

  (b) LEDGER RE-STAMPING. A join/split/dissolve re-keys existing `bottle_ledger`
      rows by UPDATEing `address_group_id`. That is not an append and does not
      change Σ(quantity), so `assert_rekeyed`/`assert_wrote_nothing` cannot see a
      row left behind under the old scope. PARTIALLY CLOSED: the join tests
      additionally assert `_place_ledger_sum`/`_scope_ledger_sum` per scope —
      which is the only reason they catch it. A new lifecycle test without one of
      those is uncovered on this axis.

  (c) NON-QUANTITY LEDGER COLUMNS. `user_id`, `address_id`, `balance_after`,
      `event_type`, `entry_metadata` and `notes` never enter either sum. Only the
      handful of tests that name them cover them.

  (d) MONEY. `bottle_fines.fine_amount` is not a bottle and is not conserved
      here at all; that is `tests/integration/test_place_money_boundary_e2e.py`.

  (e) THE DRIVER SESSION DOMAIN. `DriverBottleSession.bottles_collected_from_
      customers` is an INTEGER column and `record_standalone_collection` tallies
      it as `int(qty)`, so a fractional collection moves the place by -4.50 and
      the van by 4. The two domains are deliberately not summed together; the
      asymmetry is real and is out of scope for a bottle-BALANCE invariant.

  (f) FK INTEGRITY AND LOCKING — see the TEST-INFRA note at the bottom.

  (g) THE NIGHTLY SWEEP'S OWN BLIND SPOTS. Many tests below finish with
      `_assert_sweep_clean(_invariants())` and it is tempting to read that as
      "and nothing is corrupt". It is a NARROW claim, and the exact width of it
      moved when the locking design's ORACLES 2 and 3 shipped: the sweep is no
      longer balance-only. `stamp_incoherent_ledger_entries` (design §6.2) reads
      `bottle_ledger` against live membership and
      `duplicate_rescoped_ledger_entries` (§6.3) replays place custody over
      `customer_link_events`. `bottle_fines` is still queried by NOTHING.
      `TestTheSweepCanActuallyFail::test_the_sweep_detection_matrix` runs the
      reachable corrupt end states through the sweep one by one and asserts,
      key by key, which of them it can actually see — FOUR OF THE SEVEN are
      still reported COMPLETELY CLEAN, and the seventh is the one ORACLE 2
      moved from a balance-only finding to a ledger-and-balance finding.
      `TestTheSweepHasNoLedgerOrFineTwin` carries what remains of that gap.
      A green sweep is still not evidence that a place's ledger SUM matches its
      stored figure, and still says nothing at all about fines; read the matrix
      before quoting it.

THE ROOT CAUSE THIS FILE IS STRUCTURALLY BLIND TO — and what it does instead.

`resolve_scope` reads `user_addresses` UNLOCKED; `get_or_create_balance` then
takes the balance row FOR UPDATE; and nothing re-validates the scope AFTER the
lock. Several separately-reported defects are that one hole wearing different
clothes. Every symptom of it is a PURE ATTRIBUTION move — the right total in the
wrong place — so the global pair at the top of this docstring is invariant under
all of them, and on this backend `with_for_update()` is a no-op so the race
itself is not even expressible. The interleavings live in
`tests/integration/test_place_concurrency_pg_e2e.py`.

What this file CAN do — and what every multi-scope test below therefore does —
is assert the SEQUENTIAL shapes of the same defect per scope: which scope holds
the bottles, which scope the ledger rows are stamped to, and whether any address
can still resolve to them. The damage in this class is that bottles become
UNREACHABLE, not that they vanish, so a global sum (or one side of a
conservation pair) is never a sufficient assertion here.

WHY A HARNESS SELF-TEST COMES FIRST. A conservation helper that silently passes
is worse than no helper: it advertises coverage that does not exist. The first
test in this file therefore runs the ONE production writer that provably moves
a balance with no ledger row at all — `BottleTrackingService.reconcile_balance`,
still exposed at `POST /admin/bottles/reconcile/<address_id>` and deliberately
never called by the place lifecycle — and asserts the helper REPORTS THE
VIOLATION. If that test ever goes green-by-silence, every other test in this
file is worthless.

THE TWO FIGURES. A place has a STORED balance (`bottle_balances.balance`, what
every operational reader returns) and a LEDGER SUM (`SUM(bottle_ledger.quantity)`
over the scope). They legitimately disagree on production data — dev address 24
holds a stored 20.00 with ZERO ledger rows — because addresses were manually
adjusted before grouping. `_create_ledger_entry` moves BOTH by the same
quantity, so **the drift is invariant under any balance-coupled append**. That
invariance is the reason `_create_ledger_backfill_entry` has to exist, and it is
pinned directly below.

EVERY BALANCE HERE COMES FROM A REAL WRITE PATH. `record_bottles_delivered`,
`record_bottles_returned`, `record_standalone_collection`, `admin_adjust_balance`,
`set_initial_balance`, `issue_fine`/`mark_fine_paid`, `OrderService`'s DELIVERED
edge, the admin HTTP routes, and the real `CustomerLinkService` place lifecycle.
No `BottleBalance` row is ever hand-constructed. The one deliberate exception is
`_delete_ledger_row` — used only to MANUFACTURE DRIFT, and it is exactly what
`OrderDeletionService` does to a delivered order in production today (it
FK-traverses from `orders` into `bottle_ledger` and leaves `bottle_balances`
untouched), so the drifted shapes below are reproductions of a live production
state, not fiction.

TEST-INFRA NOTE. This file runs on the default in-memory SQLite backend, where
FOREIGN KEYS are OFF and `with_for_update()` is a NO-OP. Nothing here claims to
prove FK integrity or lock ordering; those live in
`tests/integration/test_bottle_place_lock_order.py` and the `pg_app`/`pg_db`
files. CHECK constraints ARE enforced on SQLite, so the scope-key rule is real
here.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, UTC
from decimal import Decimal
import json

import pytest
from sqlalchemy import func

from business_app import db as _db
from business_app.models.bottle import (
    BottleBalance,
    BottleFine,
    BottleLedger,
    DriverBottleSession,
)
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import (
    BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
    BottleTrackingService,
    format_bottle_quantity,
)
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.order_service import OrderService
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    EntitySubtype,
    DriverBottleSessionStatus,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)


pytestmark = pytest.mark.integration


CENT = Decimal("0.01")


def D(value) -> Decimal:
    """A quantity at the ledger column's own scale, Numeric(12,2).

    Every figure in this file is compared at cent scale because that is the
    scale the columns store. SQLite renders `Numeric` through float, so a raw
    `Decimal(str(sum))` of 1.25 - 0.75 - 0.30 can arrive as
    `0.19999999999999998`; quantizing makes the comparison exact to the column's
    own precision instead of to the backend's float noise. NaN/Infinity pass
    through unquantized so the NaN-poisoning test can still see them.
    """
    dec = Decimal(str(value if value is not None else 0))
    return dec if not dec.is_finite() else dec.quantize(CENT)


# --------------------------------------------------------------------------- #
# The conservation probe — the single most load-bearing object in this file
# --------------------------------------------------------------------------- #

class ConservationViolation(AssertionError):
    """Σ balances moved by something other than the coupled ledger quantities."""


class ScopeAttributionViolation(AssertionError):
    """The right TOTAL landed in the WRONG place(s).

    Separate from `ConservationViolation` on purpose: a global-sum failure and
    an attribution failure are different defects with different fixes, and
    conflating them in one exception type would let a test that meant to assert
    one accidentally satisfy itself with the other.
    """


@dataclass
class ConservationReport:
    label: str
    balances_before: Decimal
    balances_after: Decimal
    ledger_before: Decimal
    ledger_after: Decimal
    rows_before: int
    rows_after: int
    appended: list = field(default_factory=list)   # [(id, quantity, key)]
    removed_ids: list = field(default_factory=list)
    # scope_key -> Decimal, where scope_key is (address_id, address_group_id).
    by_scope_before: dict = field(default_factory=dict)
    by_scope_after: dict = field(default_factory=dict)

    # -- the two sides of the pair ---------------------------------------- #

    @property
    def delta_balances(self) -> Decimal:
        return D(self.balances_after - self.balances_before)

    @property
    def delta_ledger(self) -> Decimal:
        return D(self.ledger_after - self.ledger_before)

    @property
    def coupled(self) -> Decimal:
        """Σ quantities of appended entries that MUST have moved a balance."""
        return D(sum((q for _i, q, k in self.appended if not _is_decoupled(k)), Decimal("0")))

    @property
    def decoupled(self) -> Decimal:
        """Σ quantities of appended `merge_backfill:` entries — balance-neutral."""
        return D(sum((q for _i, q, k in self.appended if _is_decoupled(k)), Decimal("0")))

    @property
    def delta_rows(self) -> int:
        return self.rows_after - self.rows_before

    # -- the third side: WHICH scope paid ---------------------------------- #
    #
    # The global pair above is invariant under a PURE ATTRIBUTION DEFECT: move
    # 3 bottles out of place P and into place Q and ΔΣbalances is 0, Σcoupled is
    # 0, the row count is unchanged and `assert_conserved` says nothing. Both
    # confirmed fine bugs pinned in this file are exactly that shape. The scope
    # map below is the only thing in this harness that can see them, so any test
    # whose subject is a MULTI-SCOPE world must assert it.

    @property
    def scope_deltas(self) -> dict:
        """{(address_id, address_group_id): Δbalance} over EVERY scope touched.

        A created row reads as 0 -> x, a deleted row as x -> 0, so a re-key and
        a mint are told apart by WHICH keys appear, not by the row count alone.
        Scopes that did not move are omitted, so the expected map a test writes
        is the exhaustive list of places whose figure changed.
        """
        keys = set(self.by_scope_before) | set(self.by_scope_after)
        deltas = {}
        for key in keys:
            delta = D(
                self.by_scope_after.get(key, Decimal("0"))
                - self.by_scope_before.get(key, Decimal("0"))
            )
            if delta != 0:
                deltas[key] = delta
        return deltas

    def assert_scope_deltas(self, expected: dict):
        """EXACTLY these scopes moved, by EXACTLY these amounts. Nothing else.

        `expected` is keyed by `(address_id, address_group_id)` — build the keys
        with `_addr_scope`/`_group_scope` so a test never hand-writes a tuple in
        the wrong order. An empty dict means "no place's figure changed", which
        is a far stronger claim than ΔΣ == 0.
        """
        expected = {k: D(v) for k, v in expected.items()}
        actual = self.scope_deltas
        if actual != expected:
            raise ScopeAttributionViolation(
                f"per-scope Δ mismatch\n  expected     : {_fmt_scopes(expected)}"
                f"\n  actual       : {_fmt_scopes(actual)}" + self._detail()
            )
        return self

    def keys(self) -> list:
        return [k for _i, _q, k in self.appended]

    def keys_with_prefix(self, prefix: str) -> list:
        return [k for k in self.keys() if k and k.startswith(prefix)]

    def quantities_with_prefix(self, prefix: str) -> list:
        return [q for _i, q, k in self.appended if k and k.startswith(prefix)]

    # -- the assertions ---------------------------------------------------- #

    def _detail(self) -> str:
        return (
            f"\n  shape/step   : {self.label}"
            f"\n  Σbalances    : {self.balances_before} -> {self.balances_after} "
            f"(Δ {self.delta_balances})"
            f"\n  Σledger      : {self.ledger_before} -> {self.ledger_after} "
            f"(Δ {self.delta_ledger})"
            f"\n  coupled Σ    : {self.coupled}"
            f"\n  decoupled Σ  : {self.decoupled}"
            f"\n  balance rows : {self.rows_before} -> {self.rows_after}"
            f"\n  per-scope Δ  : {_fmt_scopes(self.scope_deltas)}"
            f"\n  appended     : {[(i, str(q), k) for i, q, k in self.appended]}"
            f"\n  removed ids  : {self.removed_ids}"
        )

    def assert_conserved(self):
        """The invariant. Fails LOUDLY, naming the shape that broke it."""
        if self.removed_ids:
            raise ConservationViolation(
                "ledger entries DISAPPEARED during a mutation — the ledger is "
                "append-only" + self._detail()
            )
        if self.delta_balances != self.coupled:
            raise ConservationViolation(
                "ΔΣ(all bottle_balances) != Σ(coupled appended ledger quantities)"
                + self._detail()
            )
        if self.delta_ledger != D(self.coupled + self.decoupled):
            raise ConservationViolation(
                "ΔΣ(all bottle_ledger) != coupled + decoupled" + self._detail()
            )
        return self

    def assert_moved(self, expected):
        """Conserved AND the total moved by exactly `expected`."""
        self.assert_conserved()
        expected = D(expected)
        if self.delta_balances != expected:
            raise ConservationViolation(
                f"expected ΔΣbalances == {expected}" + self._detail()
            )
        return self

    def assert_wrote_nothing(self):
        """No balance moved, no ledger row appended, no balance row created.

        The right assertion for a REJECTION and for a no-op: the bottle domain
        is byte-for-byte where it was, down to the row count.
        """
        self.assert_moved(Decimal("0"))
        if self.appended:
            raise ConservationViolation("expected NO ledger rows" + self._detail())
        if self.delta_rows != 0:
            raise ConservationViolation(
                "expected NO new bottle_balances row" + self._detail()
            )
        return self

    def assert_rekeyed(self, *, row_delta: int):
        """Nothing minted, nothing appended — but balance ROWS were RE-KEYED.

        A join is not a no-op on `bottle_balances`: `absorb_address_into_group`
        DELETES each joiner's address-keyed row after reading its figure off the
        locked row, and `_absorb_joiners_into_group` credits the sum onto the
        group's single row (creating it only when `absorbed != 0`). So the row
        COUNT legitimately changes while the total does not. `assert_wrote_nothing`
        is the wrong shape for that and would have to be weakened to fit; this
        keeps the ledger/total halves exactly as strict and additionally pins the
        exact row-count delta each shape must produce — which is what catches a
        joiner's row being deleted without being credited, or a second row being
        left behind under the old key (`stranded_address_balances`).
        """
        self.assert_moved(Decimal("0"))
        if self.appended:
            raise ConservationViolation("expected NO ledger rows" + self._detail())
        if self.delta_rows != row_delta:
            raise ConservationViolation(
                f"expected Δ(bottle_balances rows) == {row_delta}" + self._detail()
            )
        return self


def _is_decoupled(key) -> bool:
    """A decoupled entry is identifiable BY ITS KEY — that is the whole fence.

    Deliberately NOT "has no key": an unkeyed entry must be counted as COUPLED,
    so a decoupled write that forgets its key is reported as a violation rather
    than excused as one. `_create_ledger_backfill_entry` enforces the same rule
    at write time (`BOTTLE_DECOUPLED_KEY_REQUIRED`).
    """
    return bool(key) and key.startswith(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES)


def _addr_scope(address) -> tuple:
    """The `bottle_balances` scope key of an UNGROUPED address."""
    return (getattr(address, "id", address), None)


def _group_scope(group) -> tuple:
    """The `bottle_balances` scope key of a place group."""
    return (None, getattr(group, "id", group))


def _fmt_scopes(deltas: dict) -> str:
    if not deltas:
        return "{}"
    return "{" + ", ".join(
        f"{'group ' + str(g) if g is not None else 'addr ' + str(a)}: {q:+}"
        for (a, g), q in sorted(deltas.items(), key=lambda kv: (kv[0][1] or 0, kv[0][0] or 0))
    ) + "}"


def _balances_by_scope() -> dict:
    return {
        (address_id, group_id): D(balance)
        for address_id, group_id, balance in _db.session.query(
            BottleBalance.address_id,
            BottleBalance.address_group_id,
            BottleBalance.balance,
        ).all()
    }


def _sum_all_balances() -> Decimal:
    total = _db.session.query(
        func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00"))
    ).scalar()
    return D(total)


def _ledger_map() -> dict:
    return {
        row_id: (D(qty), key)
        for row_id, qty, key in _db.session.query(
            BottleLedger.id, BottleLedger.quantity, BottleLedger.idempotency_key
        ).all()
    }


@contextmanager
def conservation(label: str = ""):
    """Snapshot the WHOLE bottle domain, run a mutation, report the pair.

    Snapshots BEFORE the body (never after — a helper that snapshots afterwards
    passes against anything), over EVERY `BottleBalance` and EVERY `BottleLedger`
    row in the database (never one scope's — a scope-limited helper is blind to
    bottles that landed somewhere else).

    `try/finally` so a rejection test can wrap its own `pytest.raises` inside and
    still get a report.
    """
    _db.session.expire_all()
    report = ConservationReport(
        label=label,
        balances_before=_sum_all_balances(),
        balances_after=Decimal("0"),
        ledger_before=Decimal("0"),
        ledger_after=Decimal("0"),
        rows_before=BottleBalance.query.count(),
        rows_after=0,
        by_scope_before=_balances_by_scope(),
    )
    before = _ledger_map()
    report.ledger_before = D(sum((q for q, _k in before.values()), Decimal("0")))
    try:
        yield report
    finally:
        _db.session.expire_all()
        after = _ledger_map()
        report.balances_after = _sum_all_balances()
        report.ledger_after = D(sum((q for q, _k in after.values()), Decimal("0")))
        report.rows_after = BottleBalance.query.count()
        report.by_scope_after = _balances_by_scope()
        report.appended = sorted(
            (i, q, k) for i, (q, k) in after.items() if i not in before
        )
        report.removed_ids = sorted(i for i in before if i not in after)


# --------------------------------------------------------------------------- #
# Builders — every figure below is produced by a real service write path
# --------------------------------------------------------------------------- #

_PHONE_SEQ = [900_000_000]


def _next_phone() -> str:
    _PHONE_SEQ[0] += 1
    return f"+998{_PHONE_SEQ[0]}"


def _user(role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL, **kwargs) -> User:
    phone = _next_phone()
    u = User(
        email=f"u{phone[4:]}@example.com",
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name="U",
        user_type=user_type,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
        **kwargs,
    )
    _db.session.add(u)
    _db.session.commit()
    return u


def _admin() -> User:
    return _user(role=UserRole.ADMIN, user_type=UserType.STAFF)


def _driver() -> User:
    return _user(role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)


def _addr(owner: User, title="Home") -> UserAddress:
    a = UserAddress(
        user_id=owner.id,
        title=title,
        full_address=f"{title}, Tashkent",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
    )
    _db.session.add(a)
    _db.session.commit()
    return a


_ORDER_SEQ = [0]


def _order(owner: User, address: UserAddress, status=OrderStatus.DELIVERED) -> Order:
    _ORDER_SEQ[0] += 1
    o = Order(
        user_id=owner.id,
        order_number=f"ORD-CONS-{_ORDER_SEQ[0]:05d}",
        status=status,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    _db.session.add(o)
    _db.session.commit()
    return o


def _bottle_product(per_unit="1") -> Product:
    category = ProductCategory(name=f"Water-{_ORDER_SEQ[0]}", description="w", is_active=True)
    _db.session.add(category)
    _db.session.commit()
    p = Product(
        name=f"Pure Water {per_unit}",
        description="d",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=1000,
        min_stock_level=1,
        max_stock_level=5000,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal(str(per_unit)),
        created_at=datetime.now(UTC),
    )
    _db.session.add(p)
    _db.session.commit()
    return p


def _order_with_item(owner, product, address, *, quantity, status) -> Order:
    order = _order(owner, address, status=status)
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=Decimal("15000.00"),
        total_price=Decimal("15000.00") * Decimal(str(quantity)),
    )
    _db.session.add(item)
    _db.session.commit()
    return order


# -- real write-path shorthands --------------------------------------------- #

def _deliver(owner, address, qty, actor=None) -> BottleLedger:
    order = _order(owner, address)
    entry = BottleTrackingService().record_bottles_delivered(
        order.id, owner.id, address.id, D(qty), actor_user_id=actor.id if actor else None
    )
    _db.session.commit()
    return entry


def _give_back(owner, address, qty, actor=None) -> BottleLedger:
    order = _order(owner, address)
    entry = BottleTrackingService().record_bottles_returned(
        owner.id, address.id, D(qty), order_id=order.id, delivery_id=None,
        actor_user_id=actor.id if actor else None,
    )
    _db.session.commit()
    return entry


def _adjust(admin, address, delta, notes="admin correction", owner=None):
    return BottleTrackingService().admin_adjust_balance(
        user_id=owner.id if owner else None,
        address_id=address.id,
        adjustment=D(delta),
        actor_user_id=admin.id,
        notes=notes,
    )


def _delete_ledger_row(entry_id: int) -> None:
    """Erase ONE ledger row, leaving the stored balance standing.

    The ONLY non-service write in this file, and it is not fiction: this is
    exactly what `OrderDeletionService` does today — it FK-traverses from
    `orders` into `bottle_ledger` and deletes every child row it finds, while
    `bottle_balances` (which has no FK to `orders`) stands. It is how the
    stored-vs-ledger drift that the whole merge review exists to repair gets
    manufactured in production, so reproducing a drifted place this way
    reproduces a REAL shape rather than inventing one.
    """
    _db.session.query(BottleLedger).filter(BottleLedger.id == entry_id).delete(
        synchronize_session=False
    )
    _db.session.commit()
    _db.session.expire_all()


def _force_balance_row(*, group_id=None, address_id=None, balance="0"):
    """Hand-build a corrupt `bottle_balances` row. NO SERVICE PATH DOES THIS NOW.

    Four of the detection-matrix rows below used to be built by REAL production
    writers. The scope-locking redesign closed every one of those paths:

      * a memberless group can no longer be minted a balance row at all —
        `get_or_create_balance`'s CREATE branch runs `assert_reachable`;
      * the join and the dissolve now re-stamp `bottle_fines` alongside
        `bottle_ledger`, so a frozen fine scope follows its address and can no
        longer settle into a place nobody is at;
      * a dissolved group id is refused as a join target (`PLACE_GROUP_DISSOLVED`).

    Those rows are therefore rebuilt BY HAND rather than retired, and this is a
    deliberate choice: the matrix's whole value is that every bucket of the only
    production alarm has a committed KNOWN-BAD CONTROL that makes it fire. An
    oracle whose control has quietly stopped producing its state is an oracle
    that passes vacuously — this file has already learned that once. The states
    remain reachable in production data written BEFORE the fix, by a restore, or
    by a manual SQL repair, so they are not fiction either.
    """
    row = BottleBalance(
        address_group_id=group_id, address_id=address_id, balance=Decimal(str(balance))
    )
    _db.session.add(row)
    _db.session.commit()
    _db.session.expire_all()
    return row


def _place(address_id) -> Decimal:
    """What every operational reader returns for this address's place."""
    return D(BottleTrackingService.get_place_balance(address_id))


def _place_ledger_sum(address_id) -> Decimal:
    scope = BottleTrackingService.resolve_scope(address_id)
    return _scope_ledger_sum(scope)


def _scope_ledger_sum(scope: BottleScope) -> Decimal:
    total = (
        _db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
        .filter(*scope.ledger_filter())
        .scalar()
    )
    return D(total)


def _drift(address_id) -> Decimal:
    """stored - ledger_sum, the gap the merge review exists to close."""
    return D(_place(address_id) - _place_ledger_sum(address_id))


def _seed_place(admin, owner, address, *, stored, ledger):
    """Put a SOLO address's place on `stored` with a ledger summing to `ledger`.

    Real paths only: a delivery (or a return, for a negative ledger) produces the
    ledger half, then an `admin_adjust_balance` of the difference produces the
    stored half and its ledger row is deleted — the `OrderDeletionService` shape.
    A shape with `stored == ledger == 0` deliberately writes NOTHING, so a place
    that has never moved a bottle really has no row.
    """
    stored, ledger = D(stored), D(ledger)
    if ledger > 0:
        _deliver(owner, address, ledger)
    elif ledger < 0:
        _give_back(owner, address, -ledger)
    gap = D(stored - ledger)
    if gap != 0:
        entry = _adjust(admin, address, gap, notes="figure carried from before the ledger")
        _delete_ledger_row(entry.id)
    assert _place(address.id) == stored, "seed produced the wrong stored figure"
    assert _place_ledger_sum(address.id) == ledger, "seed produced the wrong ledger sum"
    return address


def _group(admin, addresses, reason="same office", **kwargs) -> AddressGroup:
    return CustomerLinkService().create_place_group(
        [a.id for a in addresses], acting_admin_id=admin.id, reason=reason, **kwargs
    )


def _invariants() -> dict:
    _db.session.expire_all()
    return reconcile_customer_link_invariants()


def _clean_sweep_keys() -> list:
    """The sweep buckets that must be EMPTY after any conserving lifecycle op.

    The two LEDGER oracles are in this list deliberately. They are new, and a
    new check earns its place by being run over the dozens of REAL lifecycles
    this file already drives — joins, splits, dissolves, merge reviews,
    dissolve-then-regroup round trips — not only over the hand-built bad states
    that prove it can go red. Every one of those lifecycles is legitimate, so a
    single non-empty bucket here is a FALSE POSITIVE in the oracle and must be
    reported as an oracle defect, never absorbed by shortening this list.
    """
    return [
        "orphaned_place_balances",
        "stranded_address_balances",
        "invalid_scope_balances",
        "stamp_incoherent_ledger_entries",
        "duplicate_rescoped_ledger_entries",
    ]


def _assert_sweep_clean(report, *, allow_negative=False):
    for key in _clean_sweep_keys():
        assert report[key] == [], f"{key} is not empty: {report[key]}"
    if not allow_negative:
        assert report["negative_place_balances"] == [], (
            f"unexpected negative place: {report['negative_place_balances']}"
        )


# The sweep's ENTIRE surface, pinned as a literal so a future check cannot be
# added (or silently dropped) without the detection matrix below being re-read.
SWEEP_KEYS = frozenset({
    "negative_place_balances",
    "orphaned_canonical_pointers",
    "grocery_or_entity_members",
    "events_missing_scope_snapshot",
    "allocation_stamp_mismatches",
    "event_conservation_violations",
    "orphaned_place_balances",
    "stranded_address_balances",
    "invalid_scope_balances",
    # The FINE twin of the two ledger oracles: a PENDING fine frozen to a place
    # that has no members left. It is what the balance-driven buckets can only
    # see AFTER somebody settles the fine and the settlement mints the orphan.
    "stranded_fine_scopes",
    # The two LEDGER-side oracles (locking design §6.2/§6.3). Their arrival is
    # why several rows of the matrix below moved from `set()` to a named bucket.
    "stamp_incoherent_ledger_entries",
    "duplicate_rescoped_ledger_entries",
    "group_check_errors",
})


def _sweep_dirty(report: dict) -> dict:
    """The non-empty buckets only — the sweep's actual ANSWER, not its shape."""
    assert set(report) == SWEEP_KEYS, (
        f"the sweep's key set changed: {sorted(set(report) ^ SWEEP_KEYS)} — "
        "re-run the detection matrix before trusting any `_assert_sweep_clean`"
    )
    return {k: v for k, v in report.items() if v}


# --------------------------------------------------------------------------- #
# Corrupt END STATES, each produced through a REAL production path.
#
# Every builder below leaves the database in a state some already-confirmed bug
# produces, asserts the damage it just created (so a builder that silently
# stopped working cannot make its matrix row vacuously "clean"), and returns a
# human label. The matrix test then asks ONE question of each: what does the
# nightly sweep say?
# --------------------------------------------------------------------------- #

def _state_ledger_anchored_to_a_FORMER_place(app):
    """Ledger rows stamped place G1 while the address's membership says G2.

    Reached the ordinary way: an address leaves one place (§7.1 deliberately
    leaves its rows stamped with the former group) and joins another. The SAME
    on-disk shape is what BUG 22's join race produces by accident, and the sweep
    cannot tell the two apart because it never looks at `bottle_ledger` at all.
    """
    admin = _admin()
    ua, ub, uc, ud = _user(), _user(), _user(), _user()
    a, b, c, d = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C"), _addr(ud, "D")
    _deliver(ua, a, 10)
    g1 = _group(admin, [a, b, c])
    svc = CustomerLinkService()
    svc.remove_address_from_group(
        a.id, acting_admin_id=admin.id, reason="moved out", bottles_leaving=Decimal("4")
    )
    g2 = svc.create_place_group([a.id, d.id], acting_admin_id=admin.id, reason="new office")
    _db.session.expire_all()

    delivery = BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.DELIVERY, address_id=a.id
    ).one()
    assert delivery.address_group_id == g1.id
    assert UserAddress.query.get(a.id).address_group_id == g2.id, (
        "the state this row is meant to demonstrate was not produced"
    )
    return f"ledger row {delivery.id} anchored to place {g1.id}, membership at place {g2.id}"


def _state_memberless_group_row_resurrected_by_mark_fine_paid(app):
    """BUG 1's END STATE — a balance row on a group with no members.

    The production writer that used to produce it (settling a fine frozen to a
    place that has since dissolved) is CLOSED: the dissolve re-stamps the
    survivor's fines out of the group, and `assert_reachable` refuses to mint a
    group-scoped row for a memberless group. Both halves are asserted here, so
    this builder also pins the fix. The corrupt row is then written by hand —
    see `_force_balance_row` for why the control is kept rather than retired.
    """
    admin, ua, ub = _admin(), _user(), _user()
    a, b = _addr(ua, "A"), _addr(ub, "B")
    _deliver(ua, a, 6)
    group = _group(admin, [a, b])
    fine = BottleTrackingService().issue_fine(
        user_id=None, address_id=a.id, quantity=Decimal("3"),
        fine_amount=Decimal("50000"), actor_user_id=admin.id,
    )
    _db.session.commit()
    assert fine.address_group_id == group.id
    result = CustomerLinkService().remove_address_from_group(
        b.id, acting_admin_id=admin.id, reason="moved out"
    )
    assert result["dissolved"] is True
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0

    # THE FIX, pinned in passing: the dissolve carried the fine's frozen scope
    # onto the survivor, so settling it charges the survivor and mints nothing.
    BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)
    _db.session.commit()
    _db.session.expire_all()
    assert BottleFine.query.get(fine.id).address_group_id is None
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0, (
        "the memberless group's balance row was resurrected — assert_reachable "
        "is no longer guarding the CREATE branch"
    )
    assert _place(a.id) == D(3), "the survivor was not charged"

    row = _force_balance_row(group_id=group.id, balance="-3")
    return f"memberless group {group.id} holds {row.balance} (hand-built)"


def _state_phantom_zero_row_from_waive_fine_after_a_dissolve(app):
    """BUG 2's END STATE — the same shape at quantity 0, invisible to any Σ.

    Its production writer is closed for the same two reasons as BUG 1's; both
    are asserted before the row is hand-built.
    """
    admin, ua, ub = _admin(), _user(), _user()
    a, b = _addr(ua, "A"), _addr(ub, "B")
    _deliver(ua, a, 6)
    group = _group(admin, [a, b])
    fine = BottleTrackingService().issue_fine(
        user_id=None, address_id=a.id, quantity=Decimal("3"),
        fine_amount=Decimal("50000"), actor_user_id=admin.id,
    )
    _db.session.commit()
    CustomerLinkService().remove_address_from_group(
        b.id, acting_admin_id=admin.id, reason="moved out"
    )
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
    BottleTrackingService().waive_fine(fine.id, actor_user_id=admin.id)
    _db.session.commit()
    _db.session.expire_all()
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0, (
        "waiving minted a phantom 0.00 row on the memberless group"
    )
    assert _sum_all_balances() == D(6)

    row = _force_balance_row(group_id=group.id, balance="0")
    assert _sum_all_balances() == D(6), "a 0.00 row must not move any total"
    return f"phantom 0.00 row {row.id} on memberless group {group.id} (hand-built)"


def _state_order_deletion_drift(app):
    """BUG 25 — the order cascade eats the ledger row; the balance stands."""
    admin, owner = _admin(), _user()
    addr = _addr(owner)
    entry = _deliver(owner, addr, 6)
    _delete_ledger_row(entry.id)

    assert _place(addr.id) == D(6)
    assert _place_ledger_sum(addr.id) == D(0)
    assert _drift(addr.id) == D(6), "the drift this state is about was not produced"
    return f"address {addr.id}: stored 6.00, ledger 0.00"


def _state_repopulated_dissolved_group(app):
    """BUGS 23/59 — a dissolved group id accepts new members and keeps the
    departed members' ledger permanently in scope."""
    admin, ua, ub, uc, ud = _admin(), _user(), _user(), _user(), _user()
    a, b, c, d = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C"), _addr(ud, "D")
    _deliver(ua, a, 6)
    _deliver(ub, b, 4)
    group = _group(admin, [a, b, c])
    # A DRIFTED place — the shape §7.4's merge review exists for, and the reason
    # the dissolve's paired `place_dissolve:...:out` cannot zero the group's
    # ledger scope: it debits the STORED figure, not the ledger sum.
    drift_entry = _adjust(admin, a, 5, notes="figure carried from before the ledger")
    _delete_ledger_row(drift_entry.id)
    assert _place(a.id) == D(15) and _place_ledger_sum(a.id) == D(10)

    svc = CustomerLinkService()
    svc.remove_address_from_group(b.id, acting_admin_id=admin.id, reason="moved out")
    result = svc.remove_address_from_group(c.id, acting_admin_id=admin.id, reason="moved out")
    assert result["dissolved"] is True
    _db.session.expire_all()
    assert UserAddress.query.filter_by(address_group_id=group.id).count() == 0
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
    residual_before = _scope_ledger_sum(BottleScope.for_group(group.id))
    assert residual_before == D(-5), (
        "the dissolved group carries no residual ledger — this state is not what "
        "it claims"
    )

    _deliver(ud, d, 2)
    # THE FIX, pinned in passing: a dissolved group id is no longer a join
    # target at all — one guard closing both the cross-customer exposure and the
    # scope-blind `initial:place:{G}` idempotency arm.
    with pytest.raises(ValidationError) as exc:
        svc.add_addresses_to_group(
            group.id, [d.id], acting_admin_id=admin.id, reason="new tenant"
        )
    assert exc.value.error_code == "PLACE_GROUP_DISSOLVED"
    _db.session.rollback()

    # Rebuild the historical END STATE by hand so the matrix row survives — see
    # `_force_balance_row`. This state is still reachable in data written before
    # the fix, so the sweep's answer about it is still worth pinning.
    _db.session.query(UserAddress).filter(UserAddress.id == d.id).update(
        {UserAddress.address_group_id: group.id}, synchronize_session=False
    )
    _db.session.query(BottleLedger).filter(
        BottleLedger.address_id == d.id, BottleLedger.address_group_id.is_(None)
    ).update({BottleLedger.address_group_id: group.id}, synchronize_session=False)
    own_row = BottleBalance.query.filter_by(address_id=d.id, address_group_id=None).one()
    _db.session.delete(own_row)
    _db.session.commit()
    _force_balance_row(group_id=group.id, balance="2")

    assert _place(d.id) == D(2), "the new tenant's own figure was not carried"
    residual = _scope_ledger_sum(BottleScope.for_group(group.id))
    assert residual == D(-3), (
        "the new tenant's place does not carry the strangers' residual ledger"
    )
    # This is what makes it dangerous rather than merely untidy: the very next
    # `POST /admin/bottles/reconcile/<d>` writes -3.00 onto the new tenant.
    assert _drift(d.id) == D(5)
    return (
        f"group {group.id} re-populated with address {d.id}: stored {_place(d.id)}, "
        f"ledger {residual}"
    )


def _state_a_delivery_whose_bottle_write_RAISED(app):
    """BUG 11's residue — the DELIVERED edge raised, so the place has NO rows.

    `BOTTLE_SESSION_ENFORCEMENT_STRICT` is a real production config, and with no
    `DriverBottleSessionOrder` binding the delivered order's bottle block raises
    `BOTTLE_SESSION_REQUIRED`. Whether the status survives the raise is the
    money-boundary file's question; what matters HERE is the residue it leaves
    behind on the bottle side, and what the sweep makes of it.
    """
    admin, ua, ub = _admin(), _user(), _user()
    a, b = _addr(ua, "A"), _addr(ub, "B")
    group = _group(admin, [a, b])
    product = _bottle_product(per_unit="1")
    order = _order_with_item(ua, product, a, quantity=4, status=OrderStatus.OUT_FOR_DELIVERY)

    previous = app.config.get("BOTTLE_SESSION_ENFORCEMENT_STRICT", False)
    app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
    try:
        with pytest.raises(ValidationError) as exc:
            OrderService().update_order_status(
                order.id, OrderStatus.DELIVERED, updated_by=admin.id
            )
        assert exc.value.error_code == "BOTTLE_SESSION_REQUIRED"
    finally:
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = previous
        _db.session.rollback()
    _db.session.expire_all()

    assert BottleBalance.query.count() == 0, "a balance row survived the raise"
    assert BottleLedger.query.count() == 0, "a ledger row survived the raise"
    assert _place(a.id) == Decimal("0.00")
    return f"place {group.id}: four delivered bottles, zero bottle rows"


def _state_stranded_own_scope_row_on_a_GROUPED_address(app):
    """BUGS 6/46's END STATE — an own-scope balance row under a GROUPED address.

    Its production writer (a fine issued BEFORE the join, settled after it) is
    CLOSED: `absorb_address_into_group` now re-stamps `bottle_fines` with the
    same selector it uses for `bottle_ledger`, so the frozen scope follows its
    address into the place. Asserted here, then the corrupt row — plus the
    matching stranded LEDGER row the sweep still cannot see — is written by
    hand; see `_force_balance_row`.
    """
    admin, ua, ub = _admin(), _user(), _user()
    a, b = _addr(ua, "A"), _addr(ub, "B")
    _deliver(ua, a, 6)
    fine = BottleTrackingService().issue_fine(
        user_id=None, address_id=a.id, quantity=Decimal("3"),
        fine_amount=Decimal("50000"), actor_user_id=admin.id,
    )
    _db.session.commit()
    assert fine.address_group_id is None
    group = _group(admin, [a, b])
    BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)
    _db.session.commit()
    _db.session.expire_all()

    assert BottleFine.query.get(fine.id).address_group_id == group.id
    assert BottleBalance.query.filter_by(address_id=a.id).count() == 0, (
        "the settlement stranded an own-scope row under a grouped address"
    )
    assert _place(a.id) == D(3), "the place the bottles are at was not charged"

    # The hand-built end state: a stranded BALANCE row AND a stranded LEDGER row
    # stamped `address_group_id = NULL` under a grouped address. BOTH halves are
    # now reported — the balance one by `stranded_address_balances`, the ledger
    # one by `stamp_incoherent_ledger_entries` (ORACLE 2, design §6.2). The
    # ledger half USED to be invisible; see `TestTheSweepHasNoLedgerOrFineTwin`,
    # where that demand is now a pin rather than an xfail.
    row = _force_balance_row(address_id=a.id, balance="-3")
    stranded_entry = BottleLedger(
        user_id=ua.id,
        address_id=a.id,
        address_group_id=None,
        event_type=BottleLedgerEventType.FINE_PAID,
        quantity=Decimal("-3"),
        balance_after=Decimal("-3"),
        actor_user_id=admin.id,
        occurred_at=datetime.now(UTC),
        entry_metadata={"fine_id": fine.id},
    )
    _db.session.add(stranded_entry)
    _db.session.commit()
    _db.session.expire_all()

    assert UserAddress.query.get(a.id).address_group_id == group.id
    return f"address-keyed row {row.id} at -3.00 under grouped address {a.id} (hand-built)"


# =========================================================================== #
# 0. THE HARNESS SELF-TEST — everything below is worthless without this
# =========================================================================== #

class TestTheSweepCanActuallyFail:
    def test_reconcile_balance_on_a_drifted_place_is_REPORTED_as_a_violation(self, app, db):
        """The known-bad control: a production writer that moves a balance with
        NO ledger row. If the probe passes here it would pass against anything.

        `reconcile_balance` assigns `balance.balance = ledger_sum` unconditionally,
        writes no ledger entry, and only logs a warning. Plan C never calls it —
        it is still exposed at `POST /admin/bottles/reconcile/<address_id>`.
        """
        admin, owner = _admin(), _user()
        addr = _seed_place(admin, owner, _addr(owner), stored=20, ledger=0)

        with conservation("reconcile on the address-24 shape") as probe:
            result = BottleTrackingService().reconcile_balance(addr.id)

        # The mutation really did destroy 20 bottles with no audit row...
        assert D(result["previous_balance"]) == D(20)
        assert D(result["recalculated_balance"]) == D(0)
        assert result["corrected"] is True
        assert probe.delta_balances == D(-20)
        assert probe.coupled == D(0)
        assert probe.appended == []

        # ...and the probe SAYS SO. This is the proof the harness is not vacuous.
        with pytest.raises(ConservationViolation) as exc:
            probe.assert_conserved()
        assert "ΔΣ(all bottle_balances)" in str(exc.value)
        # The failure message must name the shape that broke it.
        assert "reconcile on the address-24 shape" in str(exc.value)

    def test_the_global_pair_is_BLIND_to_a_pure_ATTRIBUTION_defect(self, app, db):
        """THE SECOND KNOWN-BAD CONTROL, and the reason `assert_scope_deltas`
        exists at all.

        `assert_conserved`/`assert_moved` compare two GLOBAL sums. A defect that
        moves the right total into the WRONG place is invariant under both: the
        bottles are still all there, they are just attributed to a scope no
        address resolves to. This test runs a real production writer that does
        exactly that — `mark_fine_paid` on a fine issued before its address
        joined a place — and pins THREE things in order:

          1. the global oracle stays SILENT (the blindness, demonstrated, not
             asserted away);
          2. the scope oracle REPORTS the violation when handed the correct
             expectation (the proof it is not vacuous);
          3. where the bottles actually went today.

        UPDATED WITH THE FIX, exactly as this docstring said it must be. The
        defect it used to exercise — `mark_fine_paid` on a fine issued before its
        address joined a place, settling into the address's own scope — is closed
        by the join's `bottle_fines` re-stamp, so (3) now reads
        `_group_scope(group): -3` and the `pytest.raises` in (2) is re-pointed at
        `_addr_scope(a)`, which is the scope that is now WRONG. The control keeps
        doing its job: it still demonstrates the global oracle's blindness and
        still proves the scope oracle is not vacuous. It is NOT deleted, because
        it is what keeps every other multi-scope assertion in this file honest.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 6)
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a.id, quantity=Decimal("3"),
            fine_amount=Decimal("50000"), actor_user_id=admin.id,
        )
        _db.session.commit()
        group = _group(admin, [a, b])
        assert BottleBalance.query.one().address_group_id == group.id

        with conservation("pay a pre-join fine: global-conserving, scope-wrong") as probe:
            BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)

        # 1. The global pair is intact. It sees NOTHING.
        probe.assert_moved(-3)
        assert probe.coupled == D(-3)

        # 2. The scope probe SAYS SO when handed an expectation that is wrong —
        #    the departed-scope answer this defect used to produce.
        with pytest.raises(ScopeAttributionViolation) as exc:
            probe.assert_scope_deltas({_addr_scope(a): -3})
        assert f"group {group.id}: -3.00" in str(exc.value)
        assert "pay a pre-join fine" in str(exc.value)

        # 3. The truth: the settlement landed on the place the bottles are at.
        probe.assert_scope_deltas({_group_scope(group): -3})
        assert _place(a.id) == D(3), "the place the bottles are at was not charged"

    def test_an_unkeyed_decoupled_write_would_be_counted_as_COUPLED(self, app, db):
        """The classification fence, from the other side.

        A decoupled entry is identifiable ONLY by its key. If `_is_decoupled`
        ever treated an unkeyed row as decoupled, a write that moves no balance
        and forgets its key would be silently excused. Pin both directions.
        """
        assert _is_decoupled("merge_backfill:9:11") is True
        assert _is_decoupled("merge_correction:9:11") is False
        assert _is_decoupled(None) is False
        assert _is_decoupled("") is False
        assert _is_decoupled("delivery:41") is False
        # And the service enforces the same rule at write time.
        assert BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES == ("merge_backfill:",)

    def test_the_backfill_writer_refuses_a_key_outside_its_namespace(self, app, db):
        """`BOTTLE_DECOUPLED_KEY_REQUIRED` — the runtime half of the fence."""
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        service = BottleTrackingService()

        for bad_key in (None, "", "delivery:1", "merge_correction:1:2"):
            with conservation(f"backfill with key {bad_key!r}") as probe:
                with pytest.raises(ValidationError) as exc:
                    service._create_ledger_backfill_entry(
                        scope=BottleScope.for_address(addr.id),
                        user_id=owner.id,
                        address_id=addr.id,
                        quantity=Decimal("5"),
                        actor_user_id=admin.id,
                        idempotency_key=bad_key,
                    )
                assert exc.value.error_code == "BOTTLE_DECOUPLED_KEY_REQUIRED"
            probe.assert_wrote_nothing()

    # -- THE DETECTION MATRIX ------------------------------------------------ #
    #
    # `reconcile_customer_link_invariants` is the ONLY automated alarm on the
    # place layer, it runs unattended as a Celery beat task, and the documented
    # operator response to a dirty bucket is the DESTRUCTIVE
    # `POST /admin/bottles/reconcile/<address_id>`. Dozens of tests in this tree
    # end with "and the sweep is clean" and treat that as a safety net. Nobody
    # had ever asked what the net can actually catch.
    #
    # The parametrisation below is the answer, as an executable table. Each row
    # produces a corrupt END STATE through REAL service and route paths (the
    # builders live above this class and assert their own damage, so a builder
    # that quietly stopped producing its state cannot make a row vacuously
    # "clean"), then asserts the sweep's ENTIRE answer — which buckets are
    # non-empty, and exactly which ids are in them.
    #
    # FOUR OF THE SEVEN ARE STILL REPORTED COMPLETELY CLEAN, and the reason has
    # narrowed. The sweep is no longer purely balance-driven: ORACLE 2
    # (`stamp_incoherent_ledger_entries`, design §6.2) reads `bottle_ledger`
    # against live membership, and ORACLE 3
    # (`duplicate_rescoped_ledger_entries`, §6.3) replays custody over
    # `customer_link_events`. What remains out of reach is (a) drift — a balance
    # row and a ledger that disagree, with both individually well-formed —
    # (b) `bottle_fines` scopes, which no check queries at all, and (c) the
    # SANCTIONED §7.1 shape, which ORACLE 2 deliberately declines to flag and
    # which is therefore indistinguishable from BUG 22's accidental version of
    # the same on-disk row. Row 1 is exactly that last case and is the most
    # important entry in the table for it.
    #
    # WHAT CHANGED, so nobody re-derives it from the git log: the last row moved
    # from a balance-only finding to a THREE-bucket finding. Its builder writes
    # a stranded BALANCE row and a stranded LEDGER row; the ledger half used to
    # be invisible, and ORACLE 2 now names it. That is a genuine improvement in
    # detection, pinned here so it cannot regress silently.
    #
    # This is a MATRIX, not a wish list: every expectation below is today's real
    # answer, so the test is GREEN and stays green until the sweep changes. When
    # a new violation class ships, the row it closes moves from `set()` to a
    # named bucket and this test fails until the table is updated — which is the
    # point, and which is precisely what happened to the last row. A future
    # `stranded_fine_scopes` has an obvious home; see
    # `TestTheSweepHasNoLedgerOrFineTwin`.

    @pytest.mark.parametrize(
        "builder, expected_dirty",
        [
            pytest.param(
                _state_ledger_anchored_to_a_FORMER_place, set(),
                id="MISSED-ledger_anchored_to_a_place_the_address_has_left",
            ),
            pytest.param(
                _state_memberless_group_row_resurrected_by_mark_fine_paid,
                {"orphaned_place_balances", "negative_place_balances"},
                id="CAUGHT-memberless_group_row_at_minus_3",
            ),
            pytest.param(
                _state_phantom_zero_row_from_waive_fine_after_a_dissolve,
                {"orphaned_place_balances"},
                id="CAUGHT-phantom_0.00_memberless_row",
            ),
            pytest.param(
                _state_order_deletion_drift, set(),
                id="MISSED-order_deletion_drift_stored_6_ledger_0",
            ),
            pytest.param(
                _state_repopulated_dissolved_group, set(),
                id="MISSED-dissolved_group_re_populated_with_a_strangers_residual",
            ),
            pytest.param(
                _state_a_delivery_whose_bottle_write_RAISED, set(),
                id="MISSED-delivered_order_whose_bottle_write_raised",
            ),
            pytest.param(
                _state_stranded_own_scope_row_on_a_GROUPED_address,
                {
                    "stranded_address_balances",
                    "negative_place_balances",
                    # NEW (design §6.2). This row USED to be caught on its
                    # BALANCE half only, and the matching stranded LEDGER row the
                    # builder writes alongside it was reported completely clean —
                    # the gap `TestTheSweepHasNoLedgerOrFineTwin` was named for.
                    # ORACLE 2 closes it: the FINE_PAID entry is stamped to NO
                    # group while address A is a live member of a place, so the
                    # place's history does not show a movement its own member
                    # recorded. Both halves of the same corruption are now
                    # reported, by two different buckets, and the id assertions
                    # below check the sweep names the RIGHT ledger row.
                    "stamp_incoherent_ledger_entries",
                },
                id="CAUGHT-own_scope_row_under_a_grouped_address",
            ),
        ],
    )
    def test_the_sweep_detection_matrix(self, app, db, builder, expected_dirty):
        """Which confirmed corruptions can the ONLY production alarm see?"""
        label = builder(app)
        report = _invariants()
        dirty = _sweep_dirty(report)

        assert set(dirty) == expected_dirty, (
            f"the sweep's answer changed for [{label}]\n"
            f"  expected non-empty : {sorted(expected_dirty)}\n"
            f"  actual non-empty   : {sorted(dirty)}\n"
            f"  full report        : {report}"
        )

        # The ids, not just the bucket names — a bucket that is non-empty for
        # the wrong reason is a false positive, which trains the operator to
        # ignore the alarm and points them at the destructive Reconcile button.
        if "orphaned_place_balances" in expected_dirty:
            live = {
                g for (g,) in _db.session.query(UserAddress.address_group_id)
                .filter(UserAddress.address_group_id.isnot(None)).distinct().all()
            }
            assert dirty["orphaned_place_balances"] and not (
                set(dirty["orphaned_place_balances"]) & live
            ), "the sweep flagged a group that still has members"
        if "stranded_address_balances" in expected_dirty:
            for b_id in dirty["stranded_address_balances"]:
                row = BottleBalance.query.get(b_id)
                assert row.address_id is not None and row.address_group_id is None
                assert UserAddress.query.get(row.address_id).address_group_id is not None
        if "negative_place_balances" in expected_dirty:
            for b_id in dirty["negative_place_balances"]:
                assert D(BottleBalance.query.get(b_id).balance) < 0
        if "stamp_incoherent_ledger_entries" in expected_dirty:
            # Same standard as the balance buckets: the bucket must name a row
            # that really is stamp-incoherent, and must NOT have swept up the
            # sanctioned §7.1 shape (a row stamped with a group its address has
            # LEFT), which is deliberate history and not corruption.
            for l_id in dirty["stamp_incoherent_ledger_entries"]:
                entry = BottleLedger.query.get(l_id)
                assert entry.address_group_id is None
                assert UserAddress.query.get(entry.address_id).address_group_id is not None

    def test_a_CLEAN_world_produces_a_completely_empty_sweep(self, app, db):
        """The matrix's own control — and the FALSE-POSITIVE fence.

        Every `set()` row above claims "the sweep saw nothing". That claim is
        only worth something if the sweep would have said the same thing about a
        world with no corruption in it at all — otherwise a row could be green
        because the sweep is broken rather than because it is blind.

        It is also the only place that tells the LEDGER oracles apart from a
        pair of checks that simply fire on healthy data: this world contains a
        real place group with real absorbed history, so ORACLE 2's join and
        ORACLE 3's custody replay both have something to chew on and must still
        report nothing.

        EVERY bucket, not the ones this test remembered to name: the assertion
        goes through `_sweep_dirty`, which first asserts the report's key set is
        exactly `SWEEP_KEYS` and then returns the non-empty buckets. A future
        SIXTH check therefore cannot slip past this control by being a key
        nobody listed — it fails the key-set assertion until `SWEEP_KEYS` is
        updated, at which point it is automatically covered by the `== {}` below.
        """
        admin, ua, ub, uc = _admin(), _user(), _user(), _user()
        a, b, c = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C")
        _deliver(ua, a, 6)
        _group(admin, [a, b, c])
        _deliver(ub, b, 4)
        _give_back(uc, c, 2)

        report = _invariants()
        # Stated twice on purpose: the shape first (no bucket can escape by
        # being unnamed), then the answer.
        assert set(report) == SWEEP_KEYS
        assert _sweep_dirty(report) == {}


class TestTheSweepHasNoLedgerOrFineTwin:
    """`stranded_address_balances` has a LEDGER twin now; it still has no FINE twin.

    The sweep's own docstring keeps `stranded_address_balances` as a BACKSTOP
    for "a direct DB edit, a restore from a pre-re-scoping dump, or a future
    write path that sets `address_group_id` without going through
    `absorb_address_into_group`". That reasoning is exactly as valid for
    `bottle_ledger` (a row whose `address_group_id` disagrees with its address's
    membership) and for `bottle_fines` (a fine frozen to a scope no address
    resolves to any more) — and BOTH of those are where the confirmed bugs
    actually leave their residue:

      * BUG 22's join race absorbs a concurrent delivery into the place's
        BALANCE but not into its LEDGER, leaving the entry at
        `address_group_id IS NULL` under a grouped address;
      * BUGS 6/7 leave a `bottle_fines` row pointing at a group its address has
        left, or that has dissolved entirely;
      * BUGS 1/2/5 settle such a fine into a memberless group.

    In every one of them the BALANCE side is either correct or already covered,
    so the balance-driven checks stay green over a corrupted ledger — which is
    strictly worse than no sweep, because it is the thing that lets the state
    ship.

    HALF OF THIS CLASS IS NOW HISTORY, AND SAYS SO. The LEDGER twin demanded
    below SHIPPED, as ORACLE 2 of the locking design
    (`.superpowers/sdd/2026-07-29-place-ledger-e2e/DESIGN-locking.md` §6.2). It
    arrived under the name `stamp_incoherent_ledger_entries` rather than the
    `stranded_address_ledger_entries` this class asked for, but it is the same
    query, down to the §7.1 exclusion this class's own "fix shape" spelled out:
    `bottle_ledger` joined to the address, `address_group_id IS NULL` on the
    entry and `IS NOT NULL` on the address. So the first test below is no longer
    an xfail demand — it is a PIN on the delivered check, and it fails if the
    bucket is renamed, narrowed, or dropped.

    THE FINE TWIN HAS NOW SHIPPED TOO, as `stranded_fine_scopes`: a PENDING
    `bottle_fines` row frozen to a place group with no members left. Both tests
    below are therefore PINS on delivered checks, and both fail if their bucket
    is renamed, narrowed, or dropped. What the fine check deliberately does NOT
    flag is the §7.1 sanctioned shape — a departed member's frozen reference to
    a place that still HAS members — for the same reason the ledger check
    excludes it: a check that fires on correct behaviour is muted within a week.
    """

    def test_a_LEDGER_row_stranded_under_a_grouped_address_is_still_a_reachable_shape(
        self, app, db
    ):
        """The precondition, pinned separately so the check below cannot be
        dismissed as firing on a hypothetical shape nobody can reach.

        UPDATED: the PRODUCTION WRITER that used to produce it is closed. It was
        `_fine_scope` returning `BottleScope.for_address(fine.address_id)` for a
        fine issued while the address was UNGROUPED and settled after it JOINED
        a place — a FINE_PAID row stamped `address_group_id = NULL` under an
        address that is, right now, a member of a place group. The join's
        `bottle_fines` re-stamp closed it, and the builder asserts that.

        The SHAPE is still reachable, which is why the check below still earns
        its keep: every row written before the fix carries it, and so does
        anything a restore or a manual SQL repair leaves behind. What has
        changed is that the sweep can now TELL — see the next test.
        """
        label = _state_stranded_own_scope_row_on_a_GROUPED_address(app)
        assert "address-keyed row" in label

        grouped_ids = {
            r.id for r in UserAddress.query.filter(
                UserAddress.address_group_id.isnot(None)
            ).all()
        }
        stranded = [
            e for e in BottleLedger.query.filter(
                BottleLedger.address_group_id.is_(None)
            ).all()
            if e.address_id in grouped_ids
        ]
        assert len(stranded) == 1, "the stranded ledger row was not produced"
        assert stranded[0].event_type == BottleLedgerEventType.FINE_PAID
        assert D(stranded[0].quantity) == D(-3)
        # And it really is unreachable: the place-scoped reader resolves past it.
        # 6 delivered minus the 3 the (now correctly re-stamped) fine settled —
        # the hand-built stranded -3 is NOT in that figure, which is the point.
        assert _place_ledger_sum(stranded[0].address_id) == D(3), (
            "the stranded row is still visible to the place-scoped ledger reader"
        )

    def test_the_sweep_flags_a_LEDGER_row_stranded_under_a_grouped_address(self, app, db):
        """WAS a strict xfail demanding `stranded_address_ledger_entries`. The
        check SHIPPED as `stamp_incoherent_ledger_entries` (design §6.2), so the
        demand is now a PIN.

        The gap this used to describe: a ledger row whose `address_group_id`
        disagrees with its address's current membership was reported COMPLETELY
        CLEAN — the residue of BUG 22 (a delivery absorbed into a join's balance
        but not its ledger) and of BUGS 6/46 (a fine settled into the own scope
        of an address that is grouped). In both, the balance-side checks are
        satisfied and the bottles are simply unreachable.

        The delivered query is the one the old `reason=` prescribed:
        `bottle_ledger` joined to the address, `address_group_id IS NULL` on the
        entry AND `IS NOT NULL` on the address — which excludes the §7.1
        sanctioned case (a row stamped with a group the address has LEFT, which
        is deliberate history). Both halves are asserted below, because a check
        that also flagged §7.1 would be muted within a week and the gap would be
        back with a green test in front of it.
        """
        _state_stranded_own_scope_row_on_a_GROUPED_address(app)
        report = _invariants()

        flagged = report["stamp_incoherent_ledger_entries"]
        assert flagged, (
            "the sweep's ledger-side violation class reported nothing on the "
            f"very state it exists for; its answer was {_sweep_dirty(report)}"
        )
        # It names the right row: the FINE_PAID -3 stamped to no place at an
        # address that IS in a place.
        entries = [BottleLedger.query.get(i) for i in flagged]
        assert [e.event_type for e in entries] == [BottleLedgerEventType.FINE_PAID]
        assert [D(e.quantity) for e in entries] == [D(-3)]
        for entry in entries:
            assert entry.address_group_id is None
            assert UserAddress.query.get(entry.address_id).address_group_id is not None
        # And it did NOT sweep up the §7.1 rows sitting in the same database:
        # the delivery this builder made BEFORE the join is stamped to the group
        # its address is in, which is coherent and must stay unflagged.
        coherent = BottleLedger.query.filter(
            BottleLedger.address_group_id.isnot(None)
        ).all()
        assert coherent, "the builder wrote no coherent rows — the fence is untested"
        assert not ({e.id for e in coherent} & set(flagged))

    def test_the_sweep_flags_a_PENDING_fine_frozen_to_a_DISSOLVED_place(self, app, db):
        """WAS a strict xfail demanding a `bottle_fines` check. It SHIPPED as
        `stranded_fine_scopes`, so the demand is now a PIN.

        The gap it used to describe: nothing in the sweep queried `bottle_fines`
        at all, so a PENDING fine frozen to a group that has since DISSOLVED was
        reported COMPLETELY CLEAN by every balance-driven bucket — invisible
        until somebody SETTLED it, at which point `mark_fine_paid` wrote through
        the frozen scope and minted a balance row for the memberless group. The
        sweep noticed then, as `orphaned_place_balances`, one destructive write
        too late.

        The intermediate assertion below changed with the fix and says so: it
        used to require the WHOLE report to be clean at this point ("the
        balance-driven sweep already sees this — re-read the test"). Its intent
        was that no BALANCE-driven bucket sees the state, and that intent is now
        asserted directly, because the fine bucket is supposed to be dirty here.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 6)
        group = _group(admin, [a, b])
        a_fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a.id, quantity=Decimal("3"),
            fine_amount=Decimal("50000"), actor_user_id=admin.id,
        )
        a_fine_id = a_fine.id
        _db.session.commit()
        # UPDATED: the fine is issued against B, the member that LEAVES. §7.1/§7.3
        # deliberately do NOT re-stamp a departed member's frozen references —
        # only the SURVIVOR's follow the dissolve — so this is the shape that
        # still strands, and it is the one the missing check is about.
        b_fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=b.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        _db.session.commit()
        result = CustomerLinkService().remove_address_from_group(
            b.id, acting_admin_id=admin.id, reason="moved out"
        )
        assert result["dissolved"] is True

        # The precondition, asserted before the demand so a failure here reads as
        # "the state was not produced" rather than "the check is missing".
        _db.session.expire_all()
        fine = BottleFine.query.get(b_fine.id)
        assert fine.status == BottleFineStatus.PENDING
        assert fine.address_group_id == group.id
        assert UserAddress.query.filter_by(address_group_id=group.id).count() == 0

        report = _invariants()
        assert set(_sweep_dirty(report)) == {"stranded_fine_scopes"}, (
            "no BALANCE-driven bucket can see this state — the fine bucket must be "
            f"the only one that fires; got {_sweep_dirty(report)}"
        )
        assert report["stranded_fine_scopes"] == [b_fine.id], (
            f"a PENDING fine is frozen to memberless group {group.id}; the sweep's "
            f"answer was {_sweep_dirty(report)}"
        )
        # And it did NOT sweep up the SURVIVOR's fine, which is just as PENDING
        # but whose frozen reference the dissolve released along with the rest of
        # the place's history — a reachable scope, so not a stranded one.
        assert BottleFine.query.get(a_fine_id).status == BottleFineStatus.PENDING
        assert a_fine_id not in report["stranded_fine_scopes"]


class TestOracle3CustodyReplayOverREALEpisodes:
    """ORACLE 3 (`duplicate_rescoped_ledger_entries`, design §6.3), driven by the
    metadata the SERVICES actually write.

    `tests/unit/test_customer_link_reconciliation.py` already controls the
    replay's logic with hand-built events carrying invented entry ids (4242,
    7001…). That proves the ALGORITHM. It cannot prove the algorithm is reading
    the keys the production write paths emit, on the ids they emit them for —
    and an oracle keyed on a metadata field nobody writes is silent forever
    while reporting nothing wrong.

    So both tests below go through `CustomerLinkService`, and both assert the
    episodes really carried the claim/release lists before drawing any
    conclusion from the sweep's silence.
    """

    @staticmethod
    def _claims_and_releases() -> tuple:
        claimed, released = [], []
        for (metadata,) in (
            _db.session.query(CustomerLinkEvent.event_metadata)
            .order_by(CustomerLinkEvent.id.asc())
            .all()
        ):
            if isinstance(metadata, dict):
                claimed += list(metadata.get("rescoped_ledger_entry_ids") or [])
                released += list(metadata.get("dissolved_rescoped_ledger_entry_ids") or [])
        return claimed, released

    def test_a_REAL_dissolve_then_REGROUP_reclaims_the_same_ids_and_is_NOT_flagged(
        self, app, db
    ):
        """The FALSE-POSITIVE fence, on a sequence the product performs daily.

        The design's literal §6.3 wording ("the union of
        `rescoped_ledger_entry_ids` must contain no duplicates") reports this
        round trip. If the sweep did that, the first admin who dissolved a place
        and re-made it would get a nightly alarm on correct behaviour — and the
        alarm would be off within a week.
        """
        admin, ua, ub, uc = _admin(), _user(), _user(), _user()
        a, b, c = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C")
        entry = _deliver(ua, a, 6)

        g1 = _group(admin, [a, b])
        svc = CustomerLinkService()
        assert svc.remove_address_from_group(
            b.id, acting_admin_id=admin.id, reason="moved out"
        )["dissolved"] is True
        g2 = svc.create_place_group(
            [a.id, c.id], acting_admin_id=admin.id, reason="re-made at the same address"
        )
        _db.session.expire_all()
        assert g2.id != g1.id

        claimed, released = self._claims_and_releases()
        # NON-VACUITY. Without these three the test passes just as happily
        # against services that write no custody metadata at all, which is the
        # exact way this oracle would rot.
        assert claimed.count(entry.id) == 2, (
            "the real join/re-join did not claim the same ledger entry twice — "
            f"claims were {claimed}, so the duplicate-scan the replay has to "
            "beat was never even set up"
        )
        assert entry.id in released, (
            "the dissolve wrote no `dissolved_rescoped_ledger_entry_ids` for the "
            f"entry it handed back; releases were {released}"
        )

        report = _invariants()
        assert report["duplicate_rescoped_ledger_entries"] == [], (
            "ORACLE 3 reported a legal dissolve-then-regroup. That is a FALSE "
            "POSITIVE in the oracle, not a state to accept: "
            f"{report['duplicate_rescoped_ledger_entries']}"
        )
        assert _sweep_dirty(report) == {}

    def test_a_SECOND_episode_claiming_a_HELD_entry_IS_flagged(self, app, db):
        """ORACLE 3's known-bad control on a REAL ledger id and a REAL claim.

        The contradiction: place g1 is live and holds `entry`, nothing released
        it, and a second episode claims it anyway — "two joins absorbed one
        address". Both places are internally consistent, so the reachability
        buckets and ORACLE 2 are all silent; this replay is the only thing that
        can see it, and here it does.
        """
        admin, ua, ub, uc, ud = _admin(), _user(), _user(), _user(), _user()
        a, b, c, d = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C"), _addr(ud, "D")
        entry = _deliver(ua, a, 6)
        g1 = _group(admin, [a, b])
        # A second, unrelated live place: its claims must NOT be swept up.
        other = _deliver(uc, c, 4)
        g2 = _group(admin, [c, d])
        _db.session.expire_all()

        claimed, released = self._claims_and_releases()
        assert claimed.count(entry.id) == 1 and entry.id not in released, (
            f"setup: entry {entry.id} must be claimed exactly once and still held"
        )
        assert other.id in claimed

        # The corruption: a second live claim on an entry place g1 still holds.
        # Hand-written because no production path can be made to do this any
        # more — which is the point of keeping a committed known-bad control.
        _db.session.add(
            CustomerLinkEvent(
                event_type="add_to_place_group",
                canonical_customer_id=None,
                acting_admin_id=admin.id,
                member_user_ids=[ua.id],
                reason=f"[group {g2.id}] second claim on an entry {g1.id} holds",
                event_metadata={"rescoped_ledger_entry_ids": [entry.id]},
            )
        )
        _db.session.commit()

        report = _invariants()
        assert report["duplicate_rescoped_ledger_entries"] == [entry.id], (
            "ORACLE 3 did not report two live episodes claiming one ledger entry. "
            f"Its answer was {_sweep_dirty(report)}"
        )
        assert other.id not in report["duplicate_rescoped_ledger_entries"]
        # And it is the ONLY bucket that can see this: both places are
        # internally consistent, so nothing else fires.
        assert set(_sweep_dirty(report)) == {"duplicate_rescoped_ledger_entries"}


# =========================================================================== #
# 1. THE PRIMITIVES — delivery, return, collection
# =========================================================================== #

class TestDeliveryConservation:
    def test_delivery_to_a_clean_solo_place_moves_balance_by_exactly_the_entry(self, app, db):
        owner = _user()
        addr = _addr(owner)

        with conservation("clean solo + delivery 6") as probe:
            entry = _deliver(owner, addr, 6)

        probe.assert_moved(6)
        assert probe.coupled == D(6)
        assert probe.delta_ledger == D(6)
        assert probe.decoupled == D(0)

        # Exactly ONE row, keyed by the ADDRESS with no group key.
        rows = BottleBalance.query.all()
        assert len(rows) == 1
        assert rows[0].address_id == addr.id and rows[0].address_group_id is None
        assert D(rows[0].balance) == D(6)
        # The running snapshot, not a re-read of the balance row.
        assert D(entry.balance_after) == D(6)
        assert entry.idempotency_key == f"delivery:{entry.order_id}"
        _assert_sweep_clean(_invariants())

    def test_delivery_to_a_drifted_place_leaves_the_DRIFT_INVARIANT(self, app, db):
        """The load-bearing claim of the whole design.

        "The drift is invariant under any balance-coupled append" is WHY
        `_create_ledger_backfill_entry` has to exist. If anyone ever re-derives
        `balance_after` from the balance row, or re-derives the balance from the
        ledger inside `_update_balance`, the drift silently changes and every
        merge-review number downstream is wrong.
        """
        admin, owner = _admin(), _user()
        addr = _seed_place(admin, owner, _addr(owner), stored=20, ledger=0)
        assert _drift(addr.id) == D(20)

        with conservation("drift +20 solo + delivery 6") as probe:
            _deliver(owner, addr, 6)

        probe.assert_moved(6)
        assert _place(addr.id) == D(26)
        assert _place_ledger_sum(addr.id) == D(6)
        assert _drift(addr.id) == D(20), "the drift MOVED under a coupled append"

    def test_delivery_to_a_two_member_place_pools_on_ONE_row_not_two(self, app, db):
        """A regression that routed the delivery through
        `BottleScope.for_address` would still satisfy a naive Δ==Σ check while
        creating a SECOND, address-keyed row. Assert the row count and the scope
        keys, not just the sum.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 7)
        group = _group(admin, [a, b])
        assert BottleBalance.query.count() == 1

        with conservation("2-member place + delivery via member B") as probe:
            _deliver(ub, b, 5)

        probe.assert_moved(5)
        assert BottleBalance.query.count() == 1, "a second balance row appeared"
        row = BottleBalance.query.one()
        assert row.address_group_id == group.id and row.address_id is None
        assert D(row.balance) == D(12)
        assert _place(a.id) == _place(b.id) == D(12)
        # No address-keyed row for the member the delivery went through.
        assert BottleBalance.query.filter_by(address_id=b.id).count() == 0
        _assert_sweep_clean(_invariants())

    def test_delivery_via_each_of_three_members_lands_on_the_same_single_row(self, app, db):
        admin = _admin()
        owners = [_user(), _user(), _user()]
        addrs = [_addr(o, f"M{i}") for i, o in enumerate(owners)]
        group = _group(admin, addrs)
        quantities = [Decimal("1"), Decimal("2.5"), Decimal("4")]

        with conservation("3-member place + 1 / 2.5 / 4 through each member") as probe:
            for owner, addr, qty in zip(owners, addrs, quantities):
                _deliver(owner, addr, qty)

        probe.assert_moved(Decimal("7.5"))
        # ONE scope moved, by the whole 7.5. Without this, a regression that
        # routed member M1's 2.5 to its own address scope still satisfies
        # `assert_moved(7.5)` — the total is global.
        probe.assert_scope_deltas({_group_scope(group): Decimal("7.5")})
        assert BottleBalance.query.count() == 1
        assert _place(addrs[0].id) == _place(addrs[1].id) == _place(addrs[2].id) == D("7.5")
        assert _place_ledger_sum(addrs[2].id) == D("7.5")

        # Each ledger row keeps its OWN attribution while all carry the group.
        entries = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.DELIVERY
        ).order_by(BottleLedger.id).all()
        assert [e.user_id for e in entries] == [o.id for o in owners]
        assert [e.address_id for e in entries] == [a.id for a in addrs]
        assert {e.address_group_id for e in entries} == {group.id}

    def test_a_duplicate_delivery_for_the_same_order_moves_nothing(self, app, db):
        """The idempotency short-circuit returns BEFORE `_update_balance`."""
        owner = _user()
        addr = _addr(owner)
        order = _order(owner, addr)
        service = BottleTrackingService()
        first = service.record_bottles_delivered(order.id, owner.id, addr.id, Decimal("6"))
        _db.session.commit()

        with conservation("re-fire delivery:{order} with a different quantity") as probe:
            again = service.record_bottles_delivered(order.id, owner.id, addr.id, Decimal("9"))
            _db.session.commit()

        probe.assert_wrote_nothing()
        assert again.id == first.id
        assert D(again.quantity) == D(6), "the stored quantity was rewritten"
        assert _place(addr.id) == D(6)


class TestReturnConservation:
    def test_return_reduces_the_place_by_exactly_the_negated_quantity(self, app, db):
        owner = _user()
        addr = _addr(owner)
        _deliver(owner, addr, 10)

        with conservation("place 10 + return 4") as probe:
            entry = _give_back(owner, addr, 4)

        probe.assert_moved(-4)
        # The sign is applied at the CALL SITE (`quantity=-qty`). A caller that
        # pre-negates would produce +4 and a one-sided Δ==Σ test would still
        # pass — so assert the resulting BALANCE too.
        assert D(entry.quantity) == D(-4)
        assert _place(addr.id) == D(6)
        assert D(entry.balance_after) == D(6)

    def test_returning_more_than_the_place_holds_goes_negative_and_still_conserves(self, app, db):
        """No `max(0, ...)` clamp may ever be added inside `_update_balance`:
        it would destroy 5 bottles with no ledger entry, and this is the only
        shape that catches it.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 3)
        group = _group(admin, [a, b])
        assert _place(a.id) == D(3)

        with conservation("2-member place 3 + return 8 through member A") as probe:
            _give_back(ua, a, 8)

        probe.assert_moved(-8)
        # A clamp that stopped the group at 0 and parked the surplus -5 on A's
        # own address scope would satisfy `assert_moved(-8)` exactly.
        probe.assert_scope_deltas({_group_scope(group): -8})
        assert _place(a.id) == _place(b.id) == D(-5)

        report = _invariants()
        row = BottleBalance.query.one()
        assert report["negative_place_balances"] == [row.id]
        for key in _clean_sweep_keys():
            assert report[key] == []

    @pytest.mark.parametrize("bad_qty", [Decimal("0"), Decimal("-3")])
    def test_a_non_positive_return_is_rejected_and_writes_absolutely_nothing(
        self, app, db, bad_qty
    ):
        owner = _user()
        addr = _addr(owner)
        _deliver(owner, addr, 10)

        with conservation(f"return {bad_qty}") as probe:
            with pytest.raises(ValidationError):
                BottleTrackingService().record_bottles_returned(
                    owner.id, addr.id, bad_qty, order_id=_order(owner, addr).id
                )
        probe.assert_wrote_nothing()
        assert _place(addr.id) == D(10)

    @pytest.mark.parametrize("bad_qty", [Decimal("0"), Decimal("-3")])
    def test_a_rejected_return_leaves_NO_row_on_a_place_that_had_none(self, app, db, bad_qty):
        """The guard sits before `_create_ledger_entry`, so no 0.00 row is left
        behind. Any reorder that locks the balance row first would create one —
        a silent row-count change the orphan sweep later trips over.
        """
        owner = _user()
        addr = _addr(owner)
        assert BottleBalance.query.count() == 0

        with conservation(f"return {bad_qty} on a virgin place") as probe:
            with pytest.raises(ValidationError):
                BottleTrackingService().record_bottles_returned(
                    owner.id, addr.id, bad_qty, order_id=_order(owner, addr).id
                )
        probe.assert_wrote_nothing()
        assert BottleBalance.query.count() == 0

    def test_the_return_idempotency_key_STRINGIFIES_a_None_delivery_id(self, app, db):
        """PINS A PRE-EXISTING KEY DEFECT — both of its directions.

        `record_bottles_returned` builds `f"return:{order_id}:{delivery_id}"`, so
        a `None` delivery id stringifies INTO the key as the literal `"None"`.
        The key therefore does not identify a collection, it identifies
        "(order, whatever delivery row happened to exist at the time)", and that
        has TWO symmetric failure directions:

          A. SWALLOW — two calls with `delivery_id=None` collapse onto
             `return:{id}:None`; the second moves no balance and returns success.
          B. DOUBLE-COUNT — the same order, once before its `Delivery` row exists
             and once after, produces `return:{id}:None` and `return:{id}:7`,
             two DIFFERENT keys, so the same physical pickup is recorded twice
             and the place is debited twice.

        PROVENANCE — this is NOT a regression from the place re-key. The line is
        unchanged since commit b917006 ("Returnable bottle tracking system"),
        which introduced the file; `git diff` against HEAD shows no edit to it.

        REACHABILITY, stated honestly. The only production caller is
        `OrderService.update_order_status`'s DELIVERED edge, which passes
        `order.delivery.id if order.delivery else None` and is itself fronted by
        the `delivery:{order_id}` key on the paired `record_bottles_delivered`.
        The driver's *standalone* pickup goes through
        `record_standalone_collection`, which since 2026-08-03 takes an OPTIONAL
        client token that defaults to `None` and is composed server-side into
        `collect:client:{actor}:{token}` — a namespace disjoint from
        `return:{order}:{delivery}`, so it cannot collide with this key and every
        internal (token-less) caller is still never swallowed. So this is a
        latent defect in a service contract, not
        a bottle currently being lost on the road — pinned at service level for
        exactly that reason, and both directions asserted so a future caller
        (a partial-collection endpoint, a retry wrapper) cannot adopt the key
        believing it means "this collection".
        """
        owner = _user()
        addr = _addr(owner)
        _deliver(owner, addr, 10)
        order = _order(owner, addr)
        service = BottleTrackingService()
        first = service.record_bottles_returned(
            owner.id, addr.id, Decimal("2"), order_id=order.id, delivery_id=None
        )
        _db.session.commit()
        assert first.idempotency_key == f"return:{order.id}:None"
        assert _place(addr.id) == D(8)

        # -- direction A: the second collection is discarded ------------------
        with conservation("second collection on an order with no delivery row") as probe:
            second = service.record_bottles_returned(
                owner.id, addr.id, Decimal("5"), order_id=order.id, delivery_id=None
            )
            _db.session.commit()

        probe.assert_wrote_nothing()
        assert second.id == first.id
        assert D(second.quantity) == D(-2)
        assert _place(addr.id) == D(8), (
            "BUG PINNED (A): the second collection of 5 was discarded"
        )

        # -- direction B: naming a delivery row defeats the same key ----------
        with conservation("re-record the SAME order once a delivery row exists") as probe:
            third = service.record_bottles_returned(
                owner.id, addr.id, Decimal("2"), order_id=order.id, delivery_id=7
            )
            _db.session.commit()

        probe.assert_moved(-2)
        assert third.id != first.id
        assert third.idempotency_key == f"return:{order.id}:7"
        assert _place(addr.id) == D(6), (
            "BUG PINNED (B): the same 2 bottles were collected once and debited twice"
        )


class TestStandaloneCollectionConservation:
    def _driver_with_session(self):
        driver = _driver()
        BottleTrackingService().open_bottle_session(driver.id, bottles_loaded=50)
        _db.session.commit()
        return driver

    def test_standalone_collection_conserves_and_tallies_the_session_SEPARATELY(self, app, db):
        """Two domains that must never pay each other: the place's bottle
        balance, and the driver session's inventory counters.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 9)
        group = _group(admin, [a, b])
        driver = self._driver_with_session()
        session = DriverBottleSession.query.filter_by(
            driver_user_id=driver.id, status=DriverBottleSessionStatus.OPEN
        ).one()
        collected_before = session.bottles_collected_from_customers or 0

        with conservation("grouped place 9 + standalone collection 4") as probe:
            BottleTrackingService().record_standalone_collection(
                user_id=ua.id, address_id=a.id, quantity=Decimal("4"), actor_user_id=driver.id
            )

        probe.assert_moved(-4)
        probe.assert_scope_deltas({_group_scope(group): -4})
        assert _place(a.id) == _place(b.id) == D(5)
        entry = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.STANDALONE_COLLECTION
        ).one()
        assert D(entry.quantity) == D(-4)
        assert entry.address_group_id == group.id and entry.address_id == a.id

        _db.session.expire_all()
        session = DriverBottleSession.query.get(session.id)
        assert (session.bottles_collected_from_customers or 0) == collected_before + 4

    def test_standalone_collection_beyond_the_place_balance_is_allowed(self, app, db):
        """A driver collecting a whole shared office's empties through one door
        is the exact production incident the place re-key was built for. A clamp
        here silently loses the surplus.
        """
        owner = _user()
        addr = _addr(owner)
        _deliver(owner, addr, 2)
        driver = self._driver_with_session()

        with conservation("place 2 + collect 5") as probe:
            BottleTrackingService().record_standalone_collection(
                user_id=owner.id, address_id=addr.id, quantity=Decimal("5"),
                actor_user_id=driver.id,
            )

        probe.assert_moved(-5)
        assert _place(addr.id) == D(-3)

    def test_standalone_collection_by_a_NON_member_is_rejected_and_moves_nothing(self, app, db):
        """`_assert_user_in_scope` is the only membership fence on a
        driver-facing write. If the grouped arm ever loses its
        `address_group_id == scope.group_id` filter, any customer id passes and
        bottles move out of a stranger's place.
        """
        admin, ua, ub, uc = _admin(), _user(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _addr(uc, "C")            # C's own, unrelated address
        _deliver(ua, a, 9)
        _group(admin, [a, b])
        driver = self._driver_with_session()

        with conservation("collection by a stranger") as probe:
            with pytest.raises(ValidationError) as exc:
                BottleTrackingService().record_standalone_collection(
                    user_id=uc.id, address_id=a.id, quantity=Decimal("3"),
                    actor_user_id=driver.id,
                )
            assert exc.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"

        probe.assert_wrote_nothing()
        assert _place(a.id) == D(9)


# =========================================================================== #
# 2. ADMIN WRITES — adjustment, initial balance, fines
# =========================================================================== #

class TestAdminAdjustmentConservation:
    def test_positive_negative_and_ZERO_each_move_exactly_their_quantity(self, app, db):
        """Zero is the boundary nobody tests: a short-circuit `if adjustment:`
        added for "efficiency" drops the audit row while the API still reports
        success, so the admin believes a correction was recorded.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 5)
        group = _group(admin, [a, b])

        for delta in (Decimal("3"), Decimal("-8"), Decimal("0")):
            with conservation(f"grouped place, admin adjust {delta}") as probe:
                entry = _adjust(admin, a, delta, notes=f"count {delta}")
            probe.assert_moved(delta)
            # The group's row is the ONLY one that may move — including for the
            # zero case, where "no scope moved" is the correct exhaustive answer
            # and an accidental 0.00 row on A's own address would show up.
            probe.assert_scope_deltas({_group_scope(group): delta} if delta else {})
            assert probe.delta_rows == 0
            assert D(entry.quantity) == D(delta)
            assert entry.address_group_id == group.id

        assert _place(a.id) == _place(b.id) == D(0)
        zero_rows = [
            e for e in BottleLedger.query.filter_by(
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
            ).all()
            if D(e.quantity) == D(0)
        ]
        assert len(zero_rows) == 1, "the 0-quantity audit row was dropped"

    @pytest.mark.parametrize("notes", ["", None])
    def test_an_adjustment_without_notes_is_rejected_and_writes_nothing(self, app, db, notes):
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        _deliver(owner, addr, 4)

        with conservation(f"adjust with notes={notes!r}") as probe:
            with pytest.raises(ValidationError):
                BottleTrackingService().admin_adjust_balance(
                    user_id=None, address_id=addr.id, adjustment=Decimal("3"),
                    actor_user_id=admin.id, notes=notes,
                )
        probe.assert_wrote_nothing()
        assert _place(addr.id) == D(4)

    # -- non-finite quantities must never reach the stored balance (FIXED) --- #
    #
    # WAS: `_as_decimal` was `Decimal(str(value or 0))`, so a bare float off
    # `json.loads` became a NON-FINITE Decimal and `_update_balance` computed
    # `balance + inf`. Nothing on the way in checked `is_finite()`, even though
    # the SAME service family already did: `_validated_bottles_leaving` and
    # `_coerce_resulting_balance` in `CustomerLinkService` both guard it and both
    # document exactly this trap. `admin_adjust_balance`, `set_initial_balance`
    # and `issue_fine` did not. It was reachable from the admin route —
    # `BottleAdjustmentRequest.adjustment` was a plain pydantic `float` with no
    # `allow_inf_nan=False`, and Flask's `request.get_json()` accepts the bare
    # `NaN` / `Infinity` / `-Infinity` JSON literals.
    #
    # NOW `_as_decimal` itself refuses a non-finite value — the SSOT coercion
    # every bottle write already funnels through, so no write path can be
    # missed — and `allow_inf_nan=False` on the three admin request models stops
    # it a layer earlier at the HTTP boundary.
    #
    # The two literals still land in separate tests rather than one parametrize,
    # because they used to defeat DIFFERENT guards and the distinction is what
    # keeps a future regression legible.

    @pytest.mark.parametrize("poison", ["Infinity", "-Infinity"])
    def test_an_INFINITE_adjustment_never_reaches_the_stored_balance(
        self, app, db, client, admin_auth_headers, poison
    ):
        """FIXED — the xfail is gone.

        WAS: `POST /api/v1/admin/bottles/adjustment` with the raw body
        `{"addressId": N, "adjustment": Infinity, "notes": "..."}` returned HTTP
        200 with `{'quantity': inf, 'balance_after': inf}` — a response body
        that is not even valid JSON for a strict client — and the place read
        back as `Decimal('Infinity')` / `Decimal('-Infinity')`. Every
        conservation sum involving that place was then infinite, and no later
        operation could repair it: a correction of any finite size leaves it
        infinite, and `reconcile_balance` re-wrote the same poison.

        This is the one state the conservation probe itself cannot evaluate.
        Every other test in this file asserts a PAIR of sums; infinity is not a
        quantity you can conserve (`inf - inf` is undefined), so once it was
        stored neither the probe nor any human could say whether anything had
        been conserved. The assertion is therefore the weakest thing that still
        means something — the stored figure must stay a FINITE number — now
        joined by the refusal itself, which is safe to assert once the fix
        exists.

        Driven through the HTTP route rather than the service so it proves
        REACHABILITY, not just that a Python method can be handed a bad float:
        the poison is sent as the bare `Infinity` JSON literal, which is exactly
        what an operator's tooling emits and what Flask's `request.get_json()`
        (stdlib `json.loads`, non-strict) happily parses.
        """
        owner = _user()
        addr = _addr(owner)
        _deliver(owner, addr, 6)
        assert _place(addr.id) == D(6)

        resp = client.post(
            "/api/v1/admin/bottles/adjustment",
            data='{"addressId": %d, "adjustment": %s, "notes": "miscounted the pallet"}'
                 % (addr.id, poison),
            content_type="application/json",
            headers=admin_auth_headers,
        )

        stored = _place(addr.id)
        assert stored.is_finite(), (
            f"POST /admin/bottles/adjustment answered {resp.status_code} to a bare "
            f"`{poison}` literal and the place's stored balance is now {stored} — "
            "permanently unrecoverable"
        )
        assert resp.status_code == 400, resp.get_data(as_text=True)
        assert stored == D(6), "the place moved despite the refusal"

    def test_a_NaN_adjustment_is_REFUSED_at_the_door(self, app, db):
        """FIXED — the xfail is gone.

        WAS: a NaN admin adjustment was not refused by the service. It got as
        far as the UPDATE, where the two backends diverged and BOTH were wrong:
        on the SQLite suite sqlite3 binds NaN as NULL and the write died with
        `IntegrityError: NOT NULL constraint failed: bottle_balances.balance`
        (an unhandled 500 that aborted the admin's transaction), while Postgres
        `numeric` accepts 'NaN' and STORED it — after which every conservation
        sum touching that place was NaN, `NaN != NaN` made the invariant
        unevaluatable, and no correction could bring it back.

        NOW `_as_decimal` raises a `ValidationError` at the door, the way
        `CustomerLinkService._validated_bottles_leaving` already did. Asserted
        at the SERVICE level deliberately — the HTTP boundary has its own
        `allow_inf_nan=False`, and this test must fail if only that outer layer
        exists, because the service is what every non-HTTP caller reaches.
        """
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        _deliver(owner, addr, 6)

        with pytest.raises(ValidationError):
            BottleTrackingService().admin_adjust_balance(
                user_id=None, address_id=addr.id, adjustment=float(json.loads("NaN")),
                actor_user_id=admin.id, notes="miscounted the pallet",
            )
        assert _place(addr.id) == D(6)

    def test_an_adjustment_on_a_virgin_place_without_notes_creates_no_row(self, app, db):
        admin, owner = _admin(), _user()
        addr = _addr(owner)

        with conservation("adjust with no notes, virgin place") as probe:
            with pytest.raises(ValidationError):
                BottleTrackingService().admin_adjust_balance(
                    user_id=None, address_id=addr.id, adjustment=Decimal("3"),
                    actor_user_id=admin.id, notes="",
                )
        probe.assert_wrote_nothing()
        assert BottleBalance.query.count() == 0

    def test_no_user_id_derives_the_REPRESENTATIVE_regardless_of_which_member(self, app, db):
        """`resolve_place_attribution_user_id` and `_place_member_address_ids`
        must agree on one ordering rule (lowest member ADDRESS id). If one sorted
        by address and the other by user, two identical admin actions attribute
        to two different coworkers — and `suggested_bottles_leaving` then
        inflates the wrong person's departure pre-fill.

        Owners are created so user ids DESCEND as address ids ascend, so a
        user-id ordering would pick a different member and fail loudly.
        """
        admin = _admin()
        u_high, u_mid, u_low = _user(), _user(), _user()   # ascending user ids
        a_low = _addr(u_low, "lowest-address")             # ascending address ids
        a_mid = _addr(u_mid, "middle-address")
        a_high = _addr(u_high, "highest-address")
        assert a_low.id < a_mid.id < a_high.id
        assert u_low.id > u_mid.id > u_high.id
        _group(admin, [a_low, a_mid, a_high])

        with conservation("adjust +4 through the HIGHEST member address") as probe:
            entry = _adjust(admin, a_high, 4, notes="place count")

        probe.assert_moved(4)
        assert entry.user_id == u_low.id, (
            "attribution did not resolve to the owner of the LOWEST member address"
        )
        # And the same answer from every member address.
        for addr in (a_low, a_mid, a_high):
            assert BottleTrackingService.resolve_place_attribution_user_id(addr.id) == u_low.id


class TestInitialBalanceConservation:
    def test_set_initial_balance_on_a_virgin_PLACE_moves_exactly_the_quantity(self, app, db):
        """UPDATED: there is NO idempotency key on this entry any more.

        This used to assert `initial:place:{group.id}`, on the reasoning that a
        scope-derived key stops a grouped place keying itself `initial:addr:{id}`
        and colliding across coworkers. The key turned out to be the problem
        rather than the fix: `uq_bottle_ledger_idempotency` is UNIQUE on the KEY
        ALONE, so `_create_ledger_entry`'s duplicate lookup carries no scope
        predicate and a key left behind by a DISSOLVED place — or one that
        survived an address's join re-stamp — swallowed a later legitimate seed
        for a different place behind a 200 echoing another customer's row.

        The real guard was always structural ("this place has no history yet"),
        it also stops two coworkers each seeding the same office, and it now runs
        under rung 1 plus the balance row's FOR UPDATE.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _group(admin, [a, b])
        assert BottleBalance.query.count() == 0

        with conservation("virgin grouped place + initial balance 12") as probe:
            entry = BottleTrackingService().set_initial_balance(
                user_id=None, address_id=b.id, quantity=Decimal("12"), actor_user_id=admin.id
            )

        probe.assert_moved(12)
        assert entry.idempotency_key is None
        assert entry.event_type == BottleLedgerEventType.INITIAL_BALANCE
        assert _place(a.id) == _place_ledger_sum(a.id) == D(12)

    def test_set_initial_balance_is_refused_on_a_place_whose_history_NETS_TO_ZERO(self, app, db):
        """The guard is `has_history OR balance != 0`. A place whose entries net
        to zero has balance 0 — dropping the `has_history` half would let a
        second seed land on top of real history and MINT 15 bottles.
        """
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        _deliver(owner, addr, 5)
        _give_back(owner, addr, 5)
        assert _place(addr.id) == D(0)

        with conservation("initial balance on a 0-but-not-virgin place") as probe:
            with pytest.raises(ValidationError) as exc:
                BottleTrackingService().set_initial_balance(
                    user_id=None, address_id=addr.id, quantity=Decimal("15"),
                    actor_user_id=admin.id,
                )
            assert exc.value.error_code == "BOTTLE_INITIAL_BALANCE_EXISTS"

        probe.assert_wrote_nothing()
        assert _place(addr.id) == D(0)


class TestFineConservation:
    def _fine(self, admin, addr, qty=Decimal("3"), amount=Decimal("50000")):
        return BottleTrackingService().issue_fine(
            user_id=None, address_id=addr.id, quantity=qty,
            fine_amount=amount, actor_user_id=admin.id,
        )

    def test_issuing_a_fine_moves_NO_bottles_at_all(self, app, db):
        """A fine is a money event. Someone reading `fine.quantity=3` and
        "fixing" the ledger entry to carry 3 would double-charge the customer
        the moment the fine is paid.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 6)
        group = _group(admin, [a, b])

        with conservation("grouped place 6 + issue fine qty 3") as probe:
            fine = self._fine(admin, a)

        probe.assert_moved(0)
        assert probe.coupled == D(0)
        # Stronger than Σ==0: NO place's figure moved at all. A +3/-3 pair split
        # across the group and A's own address nets to zero globally.
        probe.assert_scope_deltas({})
        assert len(probe.appended) == 1, "issuing a fine appended more than one row"
        issued = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_ISSUED
        ).one()
        assert D(issued.quantity) == D(0)
        assert issued.address_group_id == group.id
        assert D(fine.quantity) == D(3)
        assert _place(a.id) == D(6)

    def test_marking_a_fine_paid_reduces_the_place_by_the_FINE_quantity(self, app, db):
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 6)
        group = _group(admin, [a, b])
        fine = self._fine(admin, a)

        with conservation("pay fine 3 on a grouped place at 6") as probe:
            BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)

        probe.assert_moved(-3)
        # The CORRECT counterpart of the two bugs below: the settlement lands on
        # the GROUP's existing row and creates no second scope. Without this the
        # test passes against the buggy shape too, because -3 into a brand-new
        # address-keyed row moves ΔΣbalances by exactly -3 as well.
        probe.assert_scope_deltas({_group_scope(group): -3})
        assert len(probe.appended) == 1
        paid = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_PAID
        ).one()
        assert paid.idempotency_key == f"fine_paid:{fine.id}"
        assert paid.address_group_id == group.id
        assert _place(a.id) == _place(b.id) == D(3)
        _assert_sweep_clean(_invariants())

    def test_waiving_moves_nothing_and_paying_a_waived_fine_is_refused(self, app, db):
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        _deliver(owner, addr, 6)
        fine = self._fine(admin, addr)
        service = BottleTrackingService()

        with conservation("waive fine 3") as probe:
            service.waive_fine(fine.id, actor_user_id=admin.id)
        probe.assert_moved(0)
        probe.assert_scope_deltas({})
        assert len(probe.appended) == 1
        reversed_row = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_REVERSED
        ).one()
        assert D(reversed_row.quantity) == D(0)

        with conservation("pay an already-waived fine") as probe:
            with pytest.raises(ConflictError):
                service.mark_fine_paid(fine.id, actor_user_id=admin.id)
        probe.assert_wrote_nothing()
        assert _place(addr.id) == D(6)

    def test_waiving_an_already_PAID_fine_is_refused_too(self, app, db):
        """The mirror order. A guard that only checked PAID would let a paid
        fine be waived, appending a 0-quantity reversal that makes the audit
        trail claim the -3 was undone when it was not.
        """
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        _deliver(owner, addr, 6)
        fine = self._fine(admin, addr)
        service = BottleTrackingService()
        service.mark_fine_paid(fine.id, actor_user_id=admin.id)
        assert _place(addr.id) == D(3)

        with conservation("waive an already-paid fine") as probe:
            with pytest.raises(ConflictError):
                service.waive_fine(fine.id, actor_user_id=admin.id)
        probe.assert_wrote_nothing()
        assert _place(addr.id) == D(3)

    # -- the place lifecycle does NOT re-scope a pending fine ---------------- #
    #
    # Both tests below are GLOBALLY conserving — Σbalances moves by exactly the
    # appended -3 — which is precisely why the pair alone cannot see them. What
    # breaks is WHICH place pays: the settlement lands in a scope that no
    # address resolves to any more, so the place the bottles are physically at
    # never gets the credit and the nightly sweep goes dirty.

    def test_a_PENDING_fine_follows_its_address_INTO_a_place(self, app, db):
        """FIXED — the xfail is gone.

        WAS: neither `absorb_address_into_group` nor `_absorb_joiners_into_group`
        re-scoped `bottle_fines`, while `_fine_scope` read the FROZEN
        `fine.address_group_id`. A fine issued while an address was UNGROUPED and
        paid after that address JOINED a place settled into the address's own
        scope: `mark_fine_paid` RE-CREATED an address-keyed `bottle_balances` row
        at -quantity (a `stranded_address_balances` violation, invisible to every
        place-scoped reader) while the group's figure was untouched.

        NOW: the join carries the frozen reference. `absorb_address_into_group`
        re-stamps `bottle_fines` with the same selector it uses for
        `bottle_ledger`, so freeze and lifecycle agree — which is what makes
        FREEZE the coherent policy for `OrderEditService._cascade_bottle` too,
        rather than a second inconsistency.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 6)
        fine = self._fine(admin, a)                    # issued while UNGROUPED
        assert fine.address_group_id is None
        group = _group(admin, [a, b])
        assert _place(a.id) == D(6)
        assert BottleBalance.query.count() == 1

        with conservation("pay a pre-join fine after the address joined a place") as probe:
            BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)

        probe.assert_moved(-3)                         # global pair: intact
        # The place the bottles are physically at is the ONLY scope that may
        # move. Asserted before the reader below so the failure names the scope
        # that actually took the hit instead of just a wrong total.
        probe.assert_scope_deltas({_group_scope(group): -3})
        assert BottleFine.query.get(fine.id).status == BottleFineStatus.PAID
        assert _place(a.id) == D(3), (
            "the settlement landed in a scope the place cannot reach"
        )
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 1
        assert BottleBalance.query.filter(BottleBalance.address_id.isnot(None)).count() == 0
        _assert_sweep_clean(_invariants())

    def test_a_PENDING_fine_follows_the_place_ONTO_the_survivor_when_it_dissolves(
        self, app, db
    ):
        """FIXED — the xfail is gone.

        WAS: `release_group_history_to_address` did not re-scope `bottle_fines`
        either. A fine issued while GROUPED and paid after the place DISSOLVED
        resolved through the frozen group id, so `get_or_create_balance` CREATED
        a brand-new balance row for the MEMBERLESS group at -quantity (an
        `orphaned_place_balances` violation) while the surviving address's figure
        was untouched — the orphan class the dissolve exists to close.

        NOW: the dissolve carries the survivor's frozen fine scopes out of the
        group alongside its ledger rows, and `assert_reachable` refuses to mint a
        balance row for a memberless group as a second layer if anything ever
        gets past that.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 6)
        group = _group(admin, [a, b])
        fine = self._fine(admin, a)                    # issued while GROUPED
        assert fine.address_group_id == group.id

        result = CustomerLinkService().remove_address_from_group(
            b.id, acting_admin_id=admin.id, reason="moved out"
        )
        assert result["dissolved"] is True
        assert _place(a.id) == D(6)
        assert BottleBalance.query.count() == 1

        with conservation("pay a fine issued before the place dissolved") as probe:
            BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)

        probe.assert_moved(-3)                         # global pair: intact
        # After a dissolve the survivor's OWN address scope is the place; the
        # memberless group is not a place any more and must not be credited.
        probe.assert_scope_deltas({_addr_scope(a): -3})
        assert _place(a.id) == D(3), (
            "the settlement landed on the memberless group, not on the survivor"
        )
        assert BottleBalance.query.count() == 1
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        _assert_sweep_clean(_invariants())


# =========================================================================== #
# 3. FRACTIONS
# =========================================================================== #

class TestFractionalQuantities:
    def test_fractions_survive_every_arm_of_the_pair(self, app, db):
        """`Numeric(12,2)` permits fractions and `returnable_bottles_per_unit` is
        not integral, so any `int()` on the balance path silently drops a
        quarter-bottle per operation.
        """
        admin, ua, ub, uc = _admin(), _user(), _user(), _user()
        a, b, c = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C")

        with conservation("solo + deliver 1.25") as probe:
            _deliver(ua, a, Decimal("1.25"))
        probe.assert_moved(Decimal("1.25"))

        with conservation("solo + return 0.75") as probe:
            _give_back(ua, a, Decimal("0.75"))
        probe.assert_moved(Decimal("-0.75"))

        with conservation("solo + adjust -0.30") as probe:
            _adjust(admin, a, Decimal("-0.30"), notes="quarter bottle")
        probe.assert_moved(Decimal("-0.30"))
        assert _place(a.id) == D("0.20")

        group = _group(admin, [a, b, c])
        assert _place(a.id) == D("0.20")

        with conservation("3-member place + split-remove 0.20") as probe:
            result = CustomerLinkService().remove_address_from_group(
                a.id, acting_admin_id=admin.id, reason="moving out",
                bottles_leaving=Decimal("0.20"),
            )
        probe.assert_moved(0)
        # The whole point of a split is that the total does not move but the
        # ATTRIBUTION does: -0.20 off the group, +0.20 onto the leaver. A split
        # that took 0.20 off the group and gave it to the wrong ex-member also
        # satisfies `assert_moved(0)`.
        probe.assert_scope_deltas({
            _group_scope(group): Decimal("-0.20"),
            _addr_scope(a): Decimal("0.20"),
        })
        assert D(result["bottles_leaving"]) == D("0.20")
        assert result["dissolved"] is False
        assert _place(a.id) == D("0.20")     # a is now solo, holding its share
        assert _place(b.id) == _place(c.id) == D(0)   # the place is empty
        assert BottleTrackingService.get_place_balance(b.id) == Decimal("0.00")

    def test_fractional_bottles_per_unit_is_a_supported_CONFIGURATION(self, app, db):
        """The precondition for the class below, pinned on its own.

        `Product.returnable_bottles_per_unit` is `Numeric(precision=12, scale=2)`
        (models/product.py) and nothing anywhere validates it to be integral, so
        a 1.5-crate product is a CONFIGURATION an admin can enter — not a corrupt
        row somebody has to imagine. Every claim in
        `TestFractionalBottlesVersusTheDriverSessionTally` rests on this.
        """
        product = _bottle_product(per_unit="1.50")
        assert D(product.returnable_bottles_per_unit) == D("1.50")
        assert product.tracks_returnable_bottles is True
        owner = _user()
        addr = _addr(owner)
        order = _order_with_item(
            owner, product, addr, quantity=3, status=OrderStatus.OUT_FOR_DELIVERY
        )
        assert D(BottleTrackingService().calculate_bottles_for_order(order)) == D("4.50")

    def test_format_bottle_quantity_normalizes_without_int_truncation(self, app, db):
        assert format_bottle_quantity(Decimal("1.50")) == "1.5"
        assert format_bottle_quantity(Decimal("4.00")) == "4"
        assert format_bottle_quantity(Decimal("0.25")) == "0.25"
        assert format_bottle_quantity(None) == "0"
        assert format_bottle_quantity(Decimal("-1.50")) == "-1.5"


class TestFractionalBottlesVersusTheDriverSessionTally:
    """The PLACE ledger is exact Decimal; the DRIVER SESSION tally is `int()`.

    `DriverBottleSession.discrepancy` is the company's only driver-accountability
    number — the model says so in as many words: "Zero = perfect accountability.
    Positive = bottles unaccounted for" (models/bottle.py). It is what a manager
    confronts a driver with.

    FOUR sites truncate a Decimal bottle quantity toward zero on its way into
    that number, while the place ledger beside them keeps every cent:

      1. `order_service.py`   `bottles_delivered += int(bottles_in_order)`
      2. `order_service.py`   `bottles_collected_from_customers += int(bottles_returned_qty)`
      3. `order_edit_service.py` `delivered_delta = int(bottle_delta)`
      4. `bottle_tracking_service.py` `update_session_delivery_tally(bottles_collected=int(qty))`

    Section 3 above pinned that fractions survive every arm of the BALANCE pair,
    and the staff-bot axis pinned that a collection tallies the driver's open
    session. Nobody crossed the two. The module docstring's exemption (e) is
    honest about the two domains not being summed together — but "not summed"
    is not "may disagree by an arbitrary amount and call the difference theft".

    The invariant nobody had stated, and which the xfail below demands: for a
    sequence of bottle writes bound to ONE session, `discrepancy` computed at
    close must be ZERO when the driver returned exactly what they physically
    had. That is the ONLY thing that makes the number mean what the model says
    it means.
    """

    def _driver_with_session(self, *, loaded):
        driver = _driver()
        session = BottleTrackingService().open_bottle_session(driver.id, bottles_loaded=loaded)
        _db.session.commit()
        return driver, session

    def _bound_delivery(self, admin, driver, session, owner, address, product,
                        *, quantity, bottles_returned):
        order = _order_with_item(
            owner, product, address, quantity=quantity, status=OrderStatus.OUT_FOR_DELIVERY
        )
        BottleTrackingService().bind_order_to_session(
            session.id, order.id, accepted_by_driver_id=driver.id
        )
        _db.session.commit()
        OrderService().update_order_status(
            order.id, OrderStatus.DELIVERED, updated_by=admin.id,
            bottles_returned=bottles_returned,
        )
        return order

    @staticmethod
    def _edit(order_id, product, *, quantity, admin):
        from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

        return OrderEditService().apply_edit(
            order_id=order_id,
            items=[OrderEditItemSpec(
                product_id=product.id, quantity=quantity,
                order_item_id=Order.query.get(order_id).order_items[0].id,
            )],
            reason="driver recount",
            actor_user_id=admin.id,
        )

    @staticmethod
    def _tally(session_id):
        _db.session.expire_all()
        s = DriverBottleSession.query.get(session_id)
        return (s.bottles_delivered or 0, s.bottles_collected_from_customers or 0)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: a single fractional `returnable_bottles_per_unit` makes an HONEST "
            "driver close with a POSITIVE discrepancy, which is arithmetically "
            "indistinguishable from stealing crates. Driver loads 10, delivers a "
            "1x1.50-crate order (place ledger +1.50, session tally +int(1.50)=+1), "
            "takes 2 empties back at the door, an admin corrects the order up by one "
            "unit (place ledger +1.50, session tally +int(1.50)=+1) and the driver "
            "makes a standalone pickup of 1. Physically the van holds exactly 10 again "
            "and the place ledger agrees to the cent (it lands on 0.00); the session "
            "says delivered=2 against a real 3.00, so `compute_discrepancy` returns +1 "
            "— one crate 'unaccounted for' that never existed. It ACCUMULATES: every "
            "trip carrying that product adds another residue, and no operation ever "
            "clears it. Fix shape: make the session counters Decimal (they are plain "
            "Integer columns today), or round explicitly and carry the residue in an "
            "audited field — never `int()` at four independent call sites."
        ),
    )
    def test_an_HONEST_driver_closes_a_fractional_trip_at_ZERO_discrepancy(self, app, db):
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        group = _group(admin, [a, b])
        product = _bottle_product(per_unit="1.50")
        driver, session = self._driver_with_session(loaded=10)

        order = self._bound_delivery(
            admin, driver, session, ua, a, product,
            quantity=1, bottles_returned=Decimal("2"),
        )
        self._edit(order.id, product, quantity=2, admin=admin)
        BottleTrackingService().record_standalone_collection(
            user_id=ua.id, address_id=a.id, quantity=Decimal("1"), actor_user_id=driver.id
        )
        _db.session.commit()

        # The PLACE side is exact to the cent, and the two figures agree — so
        # every conservation and equality check in this file passes over the
        # state the assertion below rejects.
        assert _place(a.id) == _place_ledger_sum(a.id) == D(0)
        assert _place(b.id) == D(0)
        assert BottleBalance.query.one().address_group_id == group.id

        # Physically: loaded 10, handed over 3.00, took back 3.00 -> 10 in the van.
        closed = BottleTrackingService().close_bottle_session(
            driver.id, bottles_returned_to_warehouse=10, actor_user_id=driver.id
        )
        _db.session.commit()
        _db.session.expire_all()

        assert closed.discrepancy == 0, (
            f"an honest driver closed at discrepancy {closed.discrepancy}: the session "
            f"tallied delivered={closed.bottles_delivered} / "
            f"collected={closed.bottles_collected_from_customers} against a real "
            f"3.00 / 3.00, because four `int()` sites truncate what the place ledger "
            f"records exactly"
        )

    def test_TODAYS_TRUNCATION_at_each_of_the_four_sites_with_its_exact_residue(
        self, app, db
    ):
        """The passing pin, site by site.

        Each step below moves the PLACE by an exact Decimal and the SESSION by a
        truncated integer, and the residue is named at every one. DELETE OR
        REWRITE THIS TEST when the counters stop truncating — it is a record of
        the current arithmetic, not a specification of it.

        Note site 3's sign: `int()` truncates toward ZERO, so a -1.50 correction
        reverses only -1 from the session. Truncation is therefore not even
        consistent in direction — it under-counts deliveries AND under-reverses
        them, and the residues do not cancel over a round trip.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _group(admin, [a, b])
        _deliver(ub, b, 4)                      # a coworker funds the place first
        product = _bottle_product(per_unit="1.50")
        driver, session = self._driver_with_session(loaded=20)
        assert self._tally(session.id) == (0, 0)

        # -- sites 1 and 2: the DELIVERED edge -------------------------------- #
        with conservation("fractional delivery 1x1.50 with a 2.50 door return") as probe:
            order = self._bound_delivery(
                admin, driver, session, ua, a, product,
                quantity=1, bottles_returned=Decimal("2.5"),
            )
        probe.assert_moved(Decimal("-1.00"))    # +1.50 - 2.50, to the cent
        assert sorted(D(q) for _i, q, _k in probe.appended) == [D("-2.50"), D("1.50")]
        assert self._tally(session.id) == (1, 2), (
            "site 1 dropped 0.50 of a delivery; site 2 dropped 0.50 of a collection"
        )

        # -- site 3, positive delta: the order-edit cascade ------------------- #
        with conservation("edit the fractional order UP by one unit") as probe:
            self._edit(order.id, product, quantity=2, admin=admin)
        probe.assert_moved(Decimal("1.50"))
        assert self._tally(session.id) == (2, 2), "site 3 dropped 0.50 of a correction"

        # -- site 3, negative delta: truncation toward ZERO ------------------- #
        with conservation("edit the fractional order back DOWN by one unit") as probe:
            self._edit(order.id, product, quantity=1, admin=admin)
        probe.assert_moved(Decimal("-1.50"))
        assert self._tally(session.id) == (1, 2), (
            "site 3 reversed only 1 of a 1.50 correction — the round trip is not "
            "even self-cancelling on the session side"
        )

        # -- site 4: the standalone collection -------------------------------- #
        with conservation("standalone collection of half a crate") as probe:
            BottleTrackingService().record_standalone_collection(
                user_id=ua.id, address_id=a.id, quantity=Decimal("0.50"),
                actor_user_id=driver.id,
            )
            _db.session.commit()
        probe.assert_moved(Decimal("-0.50"))
        assert self._tally(session.id) == (1, 2), (
            "site 4 dropped the ENTIRE collection: int(0.50) == 0, so a driver can "
            "pick up half a crate all day and the session never hears about it"
        )

        # The place, meanwhile, is exact and internally consistent throughout —
        # which is precisely why no place-scoped invariant can see any of this.
        assert _place(a.id) == _place_ledger_sum(a.id) == D("2.50")
        _assert_sweep_clean(_invariants())


# =========================================================================== #
# 4. ORDER FLOW — the DELIVERED edge and post-delivery edits
# =========================================================================== #

class TestOrderFlowConservation:
    def test_the_DELIVERED_transition_records_delivery_and_return_as_TWO_entries(self, app, db):
        """Netting the two into one +2 entry would still satisfy Δ==Σ but
        destroys the delivered/collected split every driver and customer surface
        reads (`get_order_bottle_summary` keys on the separate rows).
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        group = _group(admin, [a, b])
        product = _bottle_product(per_unit="3")
        order = _order_with_item(
            ua, product, a, quantity=1, status=OrderStatus.OUT_FOR_DELIVERY
        )

        with conservation("OUT_FOR_DELIVERY -> DELIVERED with 1 returned") as probe:
            OrderService().update_order_status(
                order.id, OrderStatus.DELIVERED, updated_by=admin.id, bottles_returned=1
            )

        probe.assert_moved(2)
        # Both halves must land on the SAME scope. A delivery routed to the
        # group and a return routed to the order's own address would satisfy
        # `assert_moved(2)` and leave the place reading 3.
        probe.assert_scope_deltas({_group_scope(group): 2})
        assert len(probe.appended) == 2, "the two movements were netted into one row"
        assert sorted(D(q) for _i, q, _k in probe.appended) == [D(-1), D(3)]
        assert _place(a.id) == _place(b.id) == D(2)

        summary = BottleTrackingService.get_order_bottle_summary(Order.query.get(order.id))
        assert D(summary["bottles_delivered"]) == D(3)
        assert D(summary["bottles_collected"]) == D(1)
        assert D(summary["balance"]) == D(2)
        # One row for a two-member place, keyed by the group.
        assert BottleBalance.query.one().address_group_id == group.id

    def test_an_edit_on_a_NON_delivered_order_moves_nothing(self, app, db):
        """The gate that fixed a real double-debit: the delivery flow reads the
        LIVE (already-edited) item quantities, so a pre-delivery cascade would
        count the delta twice.
        """
        admin, owner = _admin(), _user()
        addr = _addr(owner)
        _deliver(owner, addr, 4)
        product = _bottle_product(per_unit="1")
        order = _order_with_item(owner, product, addr, quantity=2, status=OrderStatus.CONFIRMED)

        from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

        with conservation("edit a CONFIRMED order 2 -> 5") as probe:
            result = OrderEditService().apply_edit(
                order_id=order.id,
                items=[OrderEditItemSpec(
                    product_id=product.id, quantity=5,
                    order_item_id=order.order_items[0].id,
                )],
                reason="customer added three",
                actor_user_id=admin.id,
            )
        probe.assert_wrote_nothing()
        assert result.cascade_summary["bottle"]["skipped"] == "not_delivered"

        with conservation("edit the same CONFIRMED order back down 5 -> 1") as probe:
            OrderEditService().apply_edit(
                order_id=order.id,
                items=[OrderEditItemSpec(
                    product_id=product.id, quantity=1,
                    order_item_id=Order.query.get(order.id).order_items[0].id,
                )],
                reason="customer changed their mind",
                actor_user_id=admin.id,
            )
        probe.assert_wrote_nothing()
        assert _place(addr.id) == D(4)

    def test_an_edit_on_a_DELIVERED_order_moves_the_place_by_the_item_delta(self, app, db):
        """`_cascade_bottle` calls the PRIVATE `_create_ledger_entry` to stay
        inside the orchestrator's transaction and passes no scope, so it relies
        on `resolve_scope(order.delivery_address_id)` — an order whose delivery
        address has since JOINED a place must move the GROUP.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        product = _bottle_product(per_unit="1")
        order = _order_with_item(ua, product, a, quantity=2, status=OrderStatus.OUT_FOR_DELIVERY)
        OrderService().update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)
        assert _place(a.id) == D(2)
        group = _group(admin, [a, b])
        assert _place(b.id) == D(2)

        from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

        with conservation("edit a DELIVERED order at a grouped place 2 -> 5") as probe:
            OrderEditService().apply_edit(
                order_id=order.id,
                items=[OrderEditItemSpec(
                    product_id=product.id, quantity=5,
                    order_item_id=Order.query.get(order.id).order_items[0].id,
                )],
                reason="driver left three more",
                actor_user_id=admin.id,
            )

        probe.assert_moved(3)
        # The docstring's actual claim: the +3 goes to the GROUP, not to the
        # order's own delivery address. Only this can say which.
        probe.assert_scope_deltas({_group_scope(group): 3})
        assert _place(a.id) == _place(b.id) == D(5)
        assert BottleBalance.query.count() == 1
        assert len(probe.appended) == 1, "the cascade appended more than one row"
        cascade = [
            e for e in BottleLedger.query.filter_by(
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
            ).all()
            if (e.entry_metadata or {}).get("source") == "order_edit"
        ]
        assert len(cascade) == 1 and D(cascade[0].quantity) == D(3)
        # The cascade row itself must carry the place, while keeping the order's
        # own address as its attribution — the pair the ledger readers join on.
        assert cascade[0].address_group_id == group.id
        assert cascade[0].address_id == a.id
        assert cascade[0].id == probe.appended[0][0]
        assert _place_ledger_sum(b.id) == D(5)


class TestPostDeliveryEditAfterTheAddressLEFTThePlace:
    """The MIRROR of `test_an_edit_on_a_DELIVERED_order_moves_the_place_by_the_
    item_delta` — and the commoner direction operationally.

    That test pins an address that JOINED a place after its order was delivered.
    Nobody wrote the opposite: an address that LEFT. Both are the same defect
    family as the root cause at the top of this file — `_cascade_bottle`
    (order_edit_service.py) passes NO scope to `_create_ledger_entry`, so
    `resolve_scope(order.delivery_address_id)` is evaluated LIVE, days after the
    delivery, against whatever place the address belongs to NOW. The correction
    lands wherever the ADDRESS is today, not where the CRATES are.

    The story is completely ordinary: an employee leaves the shared office; a
    week later an admin corrects last week's order because the customer says one
    19L was never delivered. The correction hits the ex-employee's home.

    WHY THE GLOBAL ORACLE IS BLIND. One coupled write, one delta, one balance
    row moved by exactly the appended quantity. `assert_conserved`,
    `assert_moved`, the money boundary and per-place `stored == ledger_sum` all
    pass on BOTH scopes. The damage is visible ONLY per scope, which is why
    every test below asserts `assert_scope_deltas` and reads both places.
    """

    @staticmethod
    def _edit(order_id, product, *, quantity, reason, admin):
        from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

        return OrderEditService().apply_edit(
            order_id=order_id,
            items=[OrderEditItemSpec(
                product_id=product.id, quantity=quantity,
                order_item_id=Order.query.get(order_id).order_items[0].id,
            )],
            reason=reason,
            actor_user_id=admin.id,
        )

    def _delivered_at_a_place_the_address_then_LEAVES(self, *, members=3):
        """+5 delivered onto place G through a1, then a1 leaves with nothing.

        `members=3` keeps G alive after the removal (the ordinary offboarding);
        `members=2` makes the removal DISSOLVE G onto the other address, which
        is the sharper shape — every crate ends up somewhere a1 cannot reach.
        """
        admin = _admin()
        owners = [_user() for _ in range(members)]
        addrs = [_addr(o, f"M{i}") for i, o in enumerate(owners)]
        a1, a2 = addrs[0], addrs[1]
        group = _group(admin, addrs)
        product = _bottle_product(per_unit="1")
        order = _order_with_item(
            owners[0], product, a1, quantity=5, status=OrderStatus.OUT_FOR_DELIVERY
        )
        OrderService().update_order_status(
            order.id, OrderStatus.DELIVERED, updated_by=admin.id
        )
        assert _place(a1.id) == _place(a2.id) == D(5)
        assert BottleBalance.query.count() == 1

        result = CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left the company"
        )
        _db.session.expire_all()
        assert result["dissolved"] is (members == 2)
        # The crates stayed with the place; a1 left with nothing and has no row.
        assert _place(a2.id) == D(5)
        assert _place(a1.id) == D(0)
        assert BottleBalance.query.filter_by(address_id=a1.id).count() == 0
        assert BottleBalance.query.count() == 1
        return admin, owners, addrs, group, product, order

    def test_a_correction_to_a_DELIVERED_order_lands_on_the_place_that_HOLDS_the_crates(
        self, app, db
    ):
        """FIXED — the xfail is gone.

        WAS: a post-delivery order edit booked its bottle correction to whatever
        place the DELIVERY ADDRESS belonged to at EDIT time, not to the place the
        delivery it corrects actually moved. `_cascade_bottle` called
        `_create_ledger_entry(address_id=order.delivery_address_id)` with NO
        `scope=`, so `resolve_scope` ran live. Correcting last week's delivery
        down by 2 after the address had LEFT the shared place minted a brand-new
        address-keyed balance row for the departed customer at -2.00 (a
        `negative_place_balances` violation; their /bottles screen read "you
        over-returned 2") while the office kept the full +5 for crates that came
        back. No single `get_place_ledger` showed both halves of one order.

        NOW the scope is FROZEN to the episode: `_cascade_bottle` reads the
        `delivery:{order_id}` ledger row's own stamp and passes it as `scope=`.
        A correction belongs to the episode it corrects, not to today's
        geography — the same policy `bottle_fines` has always had, and now the
        lifecycle carries both frozen references.
        """
        admin, owners, addrs, group, product, order = (
            self._delivered_at_a_place_the_address_then_LEAVES(members=3)
        )
        a1, a2 = addrs[0], addrs[1]

        with conservation("correct a delivered order DOWN after its address left") as probe:
            self._edit(order.id, product, quantity=3, reason="driver miscounted", admin=admin)

        probe.assert_moved(-2)                      # global pair: intact
        # The crates are at G. G is the only scope that may move.
        probe.assert_scope_deltas({_group_scope(group): -2})
        assert _place(a2.id) == D(3), "the place holding the crates was not corrected"
        assert _place(a1.id) == D(0), "the departed customer was charged for the office"
        assert BottleBalance.query.count() == 1, "a second place appeared out of an edit"
        _assert_sweep_clean(_invariants())

    def test_ONE_ORDER_ONE_PLACE_LEDGER_after_the_correction(self, app, db):
        """UPDATED: every figure below changed when `_cascade_bottle` froze its scope.

        This used to be the passing pin of the damage — a second
        `bottle_balances` row minted for the DEPARTED customer at -2.00, the
        office keeping crates that were physically returned, both scopes
        internally consistent so every per-place equality check passed over the
        corruption, and the nightly sweep seeing only the SYMPTOM (a negative
        place) whose documented response is the destructive Reconcile.

        It is kept, at the same granularity, as the ATTRIBUTION pin for the fixed
        behaviour: one order, ONE place ledger, and each assertion still names a
        consequence an operator or a customer actually sees. Global conservation
        held before and holds now, which is exactly why it can never be the
        assertion that matters here.
        """
        admin, owners, addrs, group, product, order = (
            self._delivered_at_a_place_the_address_then_LEAVES(members=3)
        )
        a1, a2 = addrs[0], addrs[1]

        with conservation("correct a delivered order DOWN after its address left") as probe:
            self._edit(order.id, product, quantity=3, reason="driver miscounted", admin=admin)

        probe.assert_moved(-2)
        assert probe.delta_rows == 0, "the correction minted a second place"
        probe.assert_scope_deltas({_group_scope(group): -2})

        # 1. The office is corrected for the crates that came back to it.
        assert _place(a2.id) == D(3)
        # 2. The departed customer is handed no debt at all.
        assert _place(a1.id) == D(0)
        assert BottleBalance.query.filter_by(address_id=a1.id).count() == 0
        # 3. Every scope stays internally consistent...
        assert _place(a2.id) == _place_ledger_sum(a2.id) == D(3)
        # 4. ...and the sweep is clean, including the `negative_place_balances`
        #    bucket that used to be the only visible symptom.
        report = _invariants()
        assert report["negative_place_balances"] == []
        _assert_sweep_clean(report)
        # 5. One order, ONE place ledger: both halves of it are in the same view.
        delivery_row = BottleLedger.query.filter_by(
            order_id=order.id, event_type=BottleLedgerEventType.DELIVERY
        ).one()
        cascade_row = BottleLedger.query.filter_by(
            order_id=order.id, event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
        ).one()
        assert delivery_row.address_group_id == group.id
        assert cascade_row.address_group_id == group.id
        assert cascade_row.address_id == a1.id, (
            "the ATTRIBUTION stamp still names the door the order went through — "
            "only the SCOPE is frozen"
        )
        at_a1 = {e.id for e in BottleTrackingService.get_place_ledger(a1.id)["items"]}
        at_a2 = {e.id for e in BottleTrackingService.get_place_ledger(a2.id)["items"]}
        assert at_a1 == set(), "the departed address has a scope of its own and it is empty"
        assert {delivery_row.id, cascade_row.id} <= at_a2

    def test_an_upward_correction_does_not_invoice_the_DEPARTED_customer(self, app, db):
        """FIXED — the xfail is gone.

        WAS: the same `_cascade_bottle` scope defect in the edit-UP direction.
        Correcting a delivered order upward after the delivery address had LEFT
        the place charged the DEPARTED customer for bottles physically left at
        the office: a brand-new address-keyed balance row at +3 for them, and the
        office short by the same 3. Strictly worse than the DOWN direction
        because the phantom debt is POSITIVE — collectable, feeding
        `suggested_bottles_leaving`, and exactly what a `BottleFine` is issued
        against.
        """
        admin, owners, addrs, group, product, order = (
            self._delivered_at_a_place_the_address_then_LEAVES(members=3)
        )
        a1, a2 = addrs[0], addrs[1]

        with conservation("correct a delivered order UP after its address left") as probe:
            self._edit(
                order.id, product, quantity=8, reason="driver left three more", admin=admin
            )

        probe.assert_moved(3)
        probe.assert_scope_deltas({_group_scope(group): 3})
        assert _place(a2.id) == D(8)
        assert _place(a1.id) == D(0), (
            "the departed customer was invoiced for bottles left at the office"
        )
        assert BottleBalance.query.filter_by(address_id=a1.id).count() == 0

    def test_a_dissolve_ONTO_the_delivery_address_leaves_the_correction_CORRECT(
        self, app, db
    ):
        """The control that proves the two tests above are about SCOPE, not about
        editing delivered orders.

        Same setup, one difference: the OTHER address leaves, so the place
        dissolves onto the delivery address and the crates end up in exactly the
        scope `resolve_scope` will return at edit time. The identical edit is
        then correct — which is the whole point. The defect is not "post-delivery
        edits are broken"; it is "post-delivery edits follow the ADDRESS, and
        that only coincides with the CRATES when nothing moved."
        """
        admin = _admin()
        ua, ub = _user(), _user()
        a1, a2 = _addr(ua, "M0"), _addr(ub, "M1")
        _group(admin, [a1, a2])
        product = _bottle_product(per_unit="1")
        order = _order_with_item(
            ua, product, a1, quantity=5, status=OrderStatus.OUT_FOR_DELIVERY
        )
        OrderService().update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)
        result = CustomerLinkService().remove_address_from_group(
            a2.id, acting_admin_id=admin.id, reason="left the company"
        )
        assert result["dissolved"] is True
        _db.session.expire_all()
        assert _place(a1.id) == D(5), "the dissolve did not release onto the survivor"

        with conservation("correct a delivered order after the place dissolved ONTO it") as probe:
            self._edit(order.id, product, quantity=3, reason="driver miscounted", admin=admin)

        probe.assert_moved(-2)
        probe.assert_scope_deltas({_addr_scope(a1): -2})
        assert _place(a1.id) == _place_ledger_sum(a1.id) == D(3)
        assert BottleBalance.query.count() == 1
        _assert_sweep_clean(_invariants())

    def test_a_dissolve_onto_the_OTHER_address_still_corrects_where_the_crates_are(
        self, app, db
    ):
        """FIXED — the strict xfail is gone, and this is the arm it was kept for.

        The hardest shape of the `_cascade_bottle` scope question: a two-member
        place where the DELIVERY address is the one that LEAVES, so the place
        dissolves onto the OTHER address and the `delivery:{order}` row is left
        carrying a group whose `bottle_balances` row was DELETED.

        FREEZE alone could not answer it — booking to the frozen scope would
        re-mint precisely the orphan §7.3's dissolve exists to eliminate — so it
        was REFUSED by name (`BOTTLE_CORRECTION_SCOPE_NOT_LIVE`). Silent
        corruption -> visible refusal was the honest trade, and a dead end.

        `address_groups.dissolved_onto_address_id` is the way out: the dissolve
        records which address it released the place's history onto, so the
        correction FOLLOWS it. `assert_scope_deltas` is the load-bearing
        assertion — global conservation nets to zero even when the attribution is
        wrong, so only a per-scope delta can tell "corrected the right customer"
        from "corrected someone".
        """
        admin, owners, addrs, group, product, order = (
            self._delivered_at_a_place_the_address_then_LEAVES(members=2)
        )
        a1, a2 = addrs[0], addrs[1]
        # The place dissolved onto a2 — every crate is in a2's OWN scope now.
        assert BottleBalance.query.filter_by(address_id=a2.id).one() is not None
        # ...and the dissolve left the pointer that makes the correction findable.
        assert AddressGroup.query.get(group.id).dissolved_onto_address_id == a2.id

        with conservation("correct a delivered order after the place dissolved AWAY") as probe:
            self._edit(order.id, product, quantity=3, reason="driver miscounted", admin=admin)

        probe.assert_moved(-2)
        probe.assert_scope_deltas({_addr_scope(a2): -2})
        assert _place(a2.id) == D(3), "the customer holding the crates was not corrected"
        assert _place(a1.id) == D(0)
        # THE REASON THE REFUSAL EXISTED: exactly one balance row survives, so
        # the forwarded write re-minted no orphan for the dissolved group and no
        # phantom scope for the departed address.
        assert BottleBalance.query.count() == 1
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        _assert_sweep_clean(_invariants())


# =========================================================================== #
# 5. JOINING A PLACE (§7.2)
# =========================================================================== #

class TestJoinConservation:
    def test_creating_a_group_from_two_funded_solos_collapses_to_ONE_row(self, app, db):
        """`absorb_address_into_group` DELETES each address row after reading its
        figure off the LOCKED row; `_absorb_joiners_into_group` credits the sum
        onto the group in ONE later write. Delete-without-credit destroys the
        bottles with no ledger entry at all.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 4)
        _deliver(ub, b, 3)
        assert BottleBalance.query.count() == 2

        with conservation("join two funded solos 4 + 3") as probe:
            group = _group(admin, [a, b])

        # A join MINTS NOTHING and APPENDS NOTHING — it re-keys: both address
        # rows go, one group row arrives.
        probe.assert_rekeyed(row_delta=-1)
        # `row_delta` alone cannot say WHICH row went. Name every scope: both
        # joiners drained to zero, the group credited with exactly their sum.
        probe.assert_scope_deltas({
            _addr_scope(a): -4, _addr_scope(b): -3, _group_scope(group): 7,
        })
        row = BottleBalance.query.one()
        assert row.address_group_id == group.id and row.address_id is None
        assert D(row.balance) == D(7)
        assert BottleBalance.query.filter(BottleBalance.address_id.isnot(None)).count() == 0
        moved = BottleLedger.query.all()
        assert {e.address_group_id for e in moved} == {group.id}
        _assert_sweep_clean(_invariants())

    def test_joining_a_DRIFTED_solo_carries_its_STORED_figure_not_its_ledger_sum(self, app, db):
        """THE single most important join scenario on this axis.

        This is the exact bug §7.2 closed. Re-deriving the group figure from the
        merged ledger (which is what `reconcile_balance` would do) zeroes those
        20 bottles.
        """
        admin, ux, uy = _admin(), _user(), _user()
        x = _seed_place(admin, ux, _addr(ux, "X"), stored=20, ledger=0)
        y = _seed_place(admin, uy, _addr(uy, "Y"), stored=3, ledger=3)

        with conservation("join drift+20 with clean 3") as probe:
            group = _group(admin, [x, y])

        probe.assert_rekeyed(row_delta=-1)
        probe.assert_scope_deltas({
            _addr_scope(x): -20, _addr_scope(y): -3, _group_scope(group): 23,
        })
        assert _place(x.id) == _place(y.id) == D(23), (
            "the carried figure was re-derived from the ledger"
        )
        assert _place_ledger_sum(x.id) == D(3)
        assert _drift(x.id) == D(20), "drift did not move to the group intact"
        assert BottleBalance.query.one().address_group_id == group.id

    def test_joining_two_places_whose_balances_CANCEL_credits_nothing(self, app, db):
        """The `absorbed != 0` short-circuit means the group row is never
        created for this shape — visible only here.
        """
        admin, ux, uy = _admin(), _user(), _user()
        x = _seed_place(admin, ux, _addr(ux, "X"), stored=5, ledger=5)
        y = _seed_place(admin, uy, _addr(uy, "Y"), stored=-5, ledger=-5)
        assert BottleBalance.query.count() == 2

        with conservation("join +5 with -5") as probe:
            group = _group(admin, [x, y])

        # -2, not -1: `absorbed == 0` short-circuits, so the group row is never
        # created at all.
        probe.assert_rekeyed(row_delta=-2)
        # Both joiners drained, and the group scope is ABSENT from the map
        # entirely — never created, not created-then-zeroed. `row_delta` cannot
        # tell those apart from a swap that also nets to -2 rows.
        probe.assert_scope_deltas({_addr_scope(x): -5, _addr_scope(y): 5})
        assert _group_scope(group) not in probe.by_scope_after
        assert BottleBalance.query.count() == 0
        assert _place(x.id) == Decimal("0.00")     # coalesce, not a row
        assert BottleTrackingService.get_place_balance_row(x.id) is None
        assert _scope_ledger_sum(BottleScope.for_group(group.id)) == D(0)

    def test_joining_addresses_that_never_moved_a_bottle_creates_NO_row(self, app, db):
        """`_absorb_joiners_into_group` deliberately locks-but-does-not-create.
        A `get_or_create_balance` there would manufacture rows for every group
        ever created, which the `orphaned_place_balances` sweep then flags the
        moment the group dissolves.
        """
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")

        with conservation("join two virgin addresses") as probe:
            _group(admin, [a, b])

        probe.assert_wrote_nothing()
        assert BottleBalance.query.count() == 0
        assert _place(a.id) == Decimal("0.00")
        _assert_sweep_clean(_invariants())

    def test_adding_a_funded_address_to_an_EXISTING_funded_group_conserves(self, app, db):
        """The credit is `place_row.balance += absorbed` AFTER an `expire_all()`.
        If the expire happens with pending changes unflushed the credit is
        discarded and the joiner's bottles disappear.
        """
        admin, ua, ub, uz = _admin(), _user(), _user(), _user()
        a, b, z = _addr(ua, "A"), _addr(ub, "B"), _addr(uz, "Z")
        _deliver(ua, a, 4)
        _deliver(ub, b, 3)
        group = _group(admin, [a, b])
        _deliver(uz, z, 5)
        assert _place(a.id) == D(7)

        with conservation("add a funded address to a funded group") as probe:
            CustomerLinkService().add_addresses_to_group(
                group.id, [z.id], acting_admin_id=admin.id, reason="third coworker"
            )

        # z's own row goes; the group's existing row is credited in place.
        probe.assert_rekeyed(row_delta=-1)
        probe.assert_scope_deltas({_addr_scope(z): -5, _group_scope(group): 5})
        assert _place(a.id) == _place(b.id) == _place(z.id) == D(12)
        assert BottleBalance.query.count() == 1
        assert BottleBalance.query.filter_by(address_id=z.id).count() == 0
        # Running snapshots rebuilt over the merged timeline: monotonic and
        # ending at the LEDGER sum (which equals stored here — no drift).
        rows = BottleLedger.query.filter_by(address_group_id=group.id).order_by(
            BottleLedger.occurred_at.asc(), BottleLedger.id.asc()
        ).all()
        running = Decimal("0.00")
        for row in rows:
            running += D(row.quantity)
            assert D(row.balance_after) == D(running)
        assert D(running) == _place_ledger_sum(a.id) == D(12)
        _assert_sweep_clean(_invariants())

    def test_adding_to_a_group_with_NO_balance_row_exercises_the_late_create_branch(self, app, db):
        """The documented single exception to group-row-first lock ordering, and
        the only path where `get_or_create_balance` is called with an
        `anchor_address_id` that is not `addresses[0]` after sorting.
        """
        admin, ua, ub, uz = _admin(), _user(), _user(), _user()
        a, b, z = _addr(ua, "A"), _addr(ub, "B"), _addr(uz, "Z")
        group = _group(admin, [a, b])
        _deliver(uz, z, 5)
        assert BottleBalance.query.filter(BottleBalance.address_group_id.isnot(None)).count() == 0

        with conservation("add a funded address to a group with no row") as probe:
            CustomerLinkService().add_addresses_to_group(
                group.id, [z.id], acting_admin_id=admin.id, reason="joins the empty place"
            )

        # z's address row is swapped for a freshly created group row: net 0.
        probe.assert_rekeyed(row_delta=0)
        # `row_delta=0` is the WEAKEST possible row assertion — a create paired
        # with an unrelated delete also nets 0. Name both scopes.
        probe.assert_scope_deltas({_addr_scope(z): -5, _group_scope(group): 5})
        assert BottleBalance.query.filter_by(address_id=z.id).count() == 0
        row = BottleBalance.query.one()
        assert row.address_group_id == group.id and row.address_id is None
        assert D(row.balance) == D(5)
        # z's ledger history re-stamped onto the place: Σ(quantity) is invariant
        # under a re-stamp, so only a per-scope ledger read can see a row left
        # behind under the old key.
        assert _place_ledger_sum(a.id) == _place_ledger_sum(z.id) == D(5)
        assert BottleLedger.query.filter(BottleLedger.address_group_id.is_(None)).count() == 0
        _assert_sweep_clean(_invariants())

    def test_joining_three_addresses_in_ONE_call_credits_the_sum_ONCE(self, app, db):
        """A two-address test cannot distinguish "credited once with the sum"
        from "credited per joiner". Three can.
        """
        admin = _admin()
        owners = [_user(), _user(), _user()]
        addrs = [_addr(o, f"M{i}") for i, o in enumerate(owners)]
        for owner, addr, qty in zip(owners, addrs, (2, 3, 4)):
            _deliver(owner, addr, qty)

        with conservation("join 2 + 3 + 4 in one call") as probe:
            group = _group(admin, addrs)

        probe.assert_rekeyed(row_delta=-2)      # three address rows -> one group row
        # "Credited per joiner" and "credited once with the sum" both end at 9
        # on the group row; what tells them apart is that EVERY joiner drained by
        # exactly its own figure and NO fourth scope appeared.
        probe.assert_scope_deltas({
            _addr_scope(addrs[0]): -2,
            _addr_scope(addrs[1]): -3,
            _addr_scope(addrs[2]): -4,
            _group_scope(group): 9,
        })
        assert _place(addrs[0].id) == _place(addrs[1].id) == _place(addrs[2].id) == D(9)
        assert _place_ledger_sum(addrs[1].id) == D(9)
        assert BottleBalance.query.count() == 1
        _assert_sweep_clean(_invariants())

    def test_the_same_address_passed_twice_is_absorbed_ONCE(self, app, db):
        admin, ua, ub = _admin(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 5)

        with conservation("create_place_group([a, a, b])") as probe:
            group = CustomerLinkService().create_place_group(
                [a.id, a.id, b.id], acting_admin_id=admin.id, reason="dedup"
            )

        # a's address row is swapped for the group's: net 0.
        probe.assert_rekeyed(row_delta=0)
        # Absorbed TWICE would credit the group +10 and drain a by -10 — still
        # ΔΣ 0 and still row_delta 0. Only the per-scope figures say otherwise.
        probe.assert_scope_deltas({_addr_scope(a): -5, _group_scope(group): 5})
        assert BottleBalance.query.filter_by(address_id=a.id).count() == 0
        assert _place(a.id) == D(5), "the duplicate joiner was absorbed twice"
        assert _place_ledger_sum(a.id) == D(5), "a's history was re-stamped twice"
        assert set(CustomerLinkService().get_place_group_address_ids(group.id)) == {a.id, b.id}

    def test_a_rejoin_after_a_split_brings_back_only_the_SPLIT_bottles(self, app, db):
        """`absorb_address_into_group`'s selector is
        `address_id = a AND address_group_id IS NULL`.

        SCOPE OF THIS TEST, stated honestly: it re-joins the SAME group, so
        re-stamping a's pre-split rows would move them from G1 to G1 — a no-op.
        It therefore pins the BALANCE arithmetic of a re-join and CANNOT detect
        the loss of the `IS NULL` arm at all. The test below it does that.
        """
        admin, ua, ub, uc = _admin(), _user(), _user(), _user()
        a, b, c = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C")
        _deliver(ua, a, 10)
        group = _group(admin, [a, b, c])
        assert _place(a.id) == D(10)

        svc = CustomerLinkService()
        svc.remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="moved out", bottles_leaving=Decimal("4")
        )
        assert _place(a.id) == D(4)
        assert _place(b.id) == D(6)
        group_ledger_before = _scope_ledger_sum(BottleScope.for_group(group.id))

        with conservation("re-add the split-out address") as probe:
            svc.add_addresses_to_group(
                group.id, [a.id], acting_admin_id=admin.id, reason="came back"
            )

        # a's split-out address row goes back into the group's existing row.
        probe.assert_rekeyed(row_delta=-1)
        probe.assert_scope_deltas({_addr_scope(a): -4, _group_scope(group): 4})
        assert _place(a.id) == _place(b.id) == _place(c.id) == D(10)
        # ONLY the `:in` half re-scoped: the group ledger gains exactly +4, not
        # the whole pre-split history a second time.
        assert _scope_ledger_sum(BottleScope.for_group(group.id)) == D(
            group_ledger_before + Decimal("4")
        )
        assert _place_ledger_sum(b.id) == D(10)
        _assert_sweep_clean(_invariants())

    def test_joining_a_DIFFERENT_place_carries_only_the_addresss_OWN_scope(self, app, db):
        """THE test that actually exercises the `IS NULL` arm of
        `absorb_address_into_group`'s selector — and the only shape in this file
        whose defect is invisible to EVERY global oracle it owns.

        An address that has left one place and joins ANOTHER still carries ledger
        rows stamped `address_id = a, address_group_id = G1`: its own deliveries,
        made while it was a member of G1, which the split left with G1 because
        the departing share was settled as a paired `place_leave:...:out/:in`.
        Only the `:in` row is a's own. A bare `address_id = a` selector would
        drag G1's `+10` delivery and `-4` departure into G2.

        WHY NOTHING ELSE CATCHES IT. Re-stamping is an UPDATE of
        `bottle_ledger.address_group_id`. It appends no row, removes none and
        changes no quantity, so Σledger is unchanged; it touches no
        `bottle_balances` row, so Σbalances, the row count and even
        `assert_scope_deltas` are all unchanged; and the nightly sweep stays
        clean because no balance row is orphaned or stranded. The ONLY visible
        symptom is per-scope: G1's ledger sum collapses to 0 against a stored 6
        (drift +6) while G2's rises to 10 against a stored 4 (drift -6) — and
        spec §7.4's merge review reads exactly that drift, so the next review
        would mint two fabricated `merge_backfill:` corrections out of one join.
        Hence the assertions below are per-scope LEDGER sums, not totals.
        """
        admin = _admin()
        ua, ub, uc, ud = _user(), _user(), _user(), _user()
        a, b, c, d = _addr(ua, "A"), _addr(ub, "B"), _addr(uc, "C"), _addr(ud, "D")
        _deliver(ua, a, 10)
        g1 = _group(admin, [a, b, c])          # three members: survives a's exit
        svc = CustomerLinkService()
        svc.remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="moved out",
            bottles_leaving=Decimal("4"),
        )
        _db.session.expire_all()
        assert _place(b.id) == _place_ledger_sum(b.id) == D(6)
        assert _place(a.id) == _place_ledger_sum(a.id) == D(4)

        with conservation("the departed address joins a DIFFERENT place") as probe:
            g2 = svc.create_place_group(
                [a.id, d.id], acting_admin_id=admin.id, reason="new office"
            )

        # Balances: a clean re-key, and every global oracle is happy here even
        # when the ledger has been robbed — that is the point.
        probe.assert_rekeyed(row_delta=0)
        probe.assert_scope_deltas({_addr_scope(a): -4, _group_scope(g2): 4})

        # The ledger, per scope. THIS is what can fail.
        assert _scope_ledger_sum(BottleScope.for_group(g1.id)) == D(6), (
            "the old place's ledger history was dragged into the new place"
        )
        assert _scope_ledger_sum(BottleScope.for_group(g2.id)) == D(4), (
            "the new place inherited history that happened somewhere else"
        )
        # And therefore neither place acquired a drift out of nothing.
        assert _drift(b.id) == D(0) and _drift(a.id) == D(0)
        assert _place(a.id) == _place(d.id) == D(4)
        assert _place(b.id) == _place(c.id) == D(6)
        _assert_sweep_clean(_invariants())

    def test_every_join_fence_writes_absolutely_nothing(self, app, db):
        """`create_place_group` flushes the `AddressGroup` before absorbing; a
        fence that moved after that flush would leave an orphan group row on the
        session for the NEXT commit to adopt.
        """
        admin = _admin()
        svc = CustomerLinkService()
        # `is_grocery_store` is a DERIVED read-only property (user_type ENTITY +
        # entity_subtype GROCERY_STORE); there is no such column to set, and
        # `UserType` has no BUSINESS member — the two fences are reached through
        # the real shapes.
        grocery_owner = _user(
            user_type=UserType.ENTITY,
            entity_subtype=EntitySubtype.GROCERY_STORE,
            company_name="Corner Shop",
        )
        entity_owner = _user(
            user_type=UserType.ENTITY,
            entity_subtype=EntitySubtype.WORKPLACE,
            company_name="Acme LLC",
        )
        ua, ub, uc = _user(), _user(), _user()
        a, b = _addr(ua, "A"), _addr(ub, "B")
        _deliver(ua, a, 4)
        _deliver(ub, b, 3)
        grocery_addr = _addr(grocery_owner, "grocery")
        entity_addr = _addr(entity_owner, "entity")
        already = _addr(uc, "already")
        _group(admin, [already, _addr(_user(), "partner")])

        groups_before = AddressGroup.query.count()
        events_before = CustomerLinkEvent.query.count()
        pointers_before = {
            r.id: r.address_group_id for r in UserAddress.query.all()
        }

        cases = [
            ([a.id, grocery_addr.id], "PLACE_GROUP_GROCERY_MEMBER"),
            ([a.id, entity_addr.id], "PLACE_GROUP_ENTITY_MEMBER"),
            ([a.id, already.id], "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"),
            ([a.id, 10_000_001], "CUSTOMER_LINK_ADDRESS_NOT_FOUND"),
            ([a.id], "PLACE_GROUP_MIN_ADDRESSES"),
        ]
        for address_ids, code in cases:
            with conservation(f"rejected join {code}") as probe:
                with pytest.raises(ValidationError) as exc:
                    svc.create_place_group(
                        address_ids, acting_admin_id=admin.id, reason="nope"
                    )
                assert exc.value.error_code == code, code
            probe.assert_wrote_nothing()

        _db.session.expire_all()
        assert AddressGroup.query.count() == groups_before, "an orphan group row survived"
        assert CustomerLinkEvent.query.count() == events_before
        assert {r.id: r.address_group_id for r in UserAddress.query.all()} == pointers_before
        assert _place(a.id) == D(4) and _place(b.id) == D(3)
