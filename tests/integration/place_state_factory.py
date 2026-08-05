"""THE state generator for place / cluster / COD money tests. Not a test module.

WHY THIS EXISTS
---------------
A 4-lens review of the place-groups work found five instances of ONE defect: *a
monetary figure shown to a human and the amount or scope posted to the engine
decided by different code*. None was caught by 8 000+ tests. The first reason
given was not "nobody wrote the test" — it was:

    **The fixtures agreed by construction.**

The single fixture exercising the place screen gave Alice debt ONLY inside the
place. In that world ``max(own, cluster, place)`` and ``union(own, coworkers)``
return the IDENTICAL number, so the suite exhaustively tested a world in which
the bug is invisible. The divergent state — debt both INSIDE and OUTSIDE a
place — existed nowhere.

This module makes that state, and every other shape the defects hid in, a
*named, importable, one-line* thing. Ask for the shape; do not hand-pick it.

WHAT MAKES IT DIFFERENT FROM ``tests/unit/_scope_money_helpers.py``
------------------------------------------------------------------
That module is a row *emitter* — ``make_user``, ``delivered_cod_order``. Useful,
and this module does the same job underneath. What it cannot do is state what
the world it just built is WORTH, so every caller re-derives the expected money
by hand — usually by reasoning about the production code, which is precisely how
a test comes to agree with a bug.

Every scenario here ships an ORACLE: :class:`PersonExpectation` /
:class:`PlaceExpectation`, computed by plain arithmetic **over the declarative
spec**, never by calling the code under test. A test asserts production against
the scenario. If production and the scenario disagree, exactly one of them is
wrong and the test says which numbers.

USAGE
-----
::

    from tests.integration.place_state_factory import build

    def test_the_row_equals_the_ceiling(db):
        s = build(db, "a6_canonical")
        alice = s.expect("alice")
        assert alice.collectible_total == Decimal("45000.00")
        # ... now drive a real screen and a real post, and compare to `alice`.

Handles are by KEY, never by id: ``s.user("alice")``, ``s.address("office_a")``,
``s.place("g")``, ``s.order("alice_office")``, ``s.payment("alice_office")``.

GUARANTEES
----------
1. **Deterministic.** No ``random``, no wall-clock. Every identity string is
   derived from the scenario's *namespace*; every timestamp from
   :data:`BASE_TIME`. Rebuilding a scenario writes byte-identical column values
   (autoincrement PKs excepted — they belong to the database, not to us).
   Declaration order IS age order: debt *i* is created one minute after debt
   *i-1*, so oldest-first allocation ranking follows the spec's own order.
2. **Unique-constraint safe.** ``users.phone`` / ``users.email`` /
   ``orders.order_number`` / ``payments.payment_id`` /
   ``cash_collection_events.event_id`` / ``bottle_ledger.idempotency_key`` are
   all UNIQUE. Each scenario owns a numeric *namespace* that is baked into all
   of them, and the phone block is disjoint from every fixture already in the
   tree: conftest owns ``+99890123456x``, ``_scope_money_helpers`` owns
   ``+99890…``, ``test_place_money_boundary_e2e`` owns ``+99877`` + SIX digits;
   ours is ``+99877`` + SEVEN, so it cannot collide with any of them even by
   accident of counter value. Building into a database that already holds one
   of our phones raises
   :class:`ScenarioNamespaceCollision` with the fix in the message, rather than
   an opaque ``IntegrityError`` three frames deep.
3. **Postgres-safe.** Rows satisfy the migration-only CHECK constraints that
   SQLite silently ignores: an order past PENDING carries a delivery address
   (``ck_orders_address_required_after_pending``) and a COMPLETED cash payment
   carries a collector (``ck_payments_cash_completed_requires_collector``). The
   same scenario therefore builds under ``db`` and under ``pg_db``.
4. **Independent.** Building scenario B does not perturb scenario A's
   expectations — different namespaces, disjoint rows, no shared mutable state
   beyond the (pure) preset table.

THE DIMENSIONS — the axes the five defects actually hid on
----------------------------------------------------------
=========================================  =========================================
dimension                                  reach it with
=========================================  =========================================
debt inside a place only                   ``debt_inside_place_only``
debt outside a place only                  ``debt_outside_place_only``
debt BOTH inside and outside  (defect #1)  ``a6_canonical``
PENDING alongside DELIVERED   (defects     ``a6_with_pending``,
#3/#5)                                     ``solo_ungrouped_debtor``
linked accounts (canonical siblings)       ``sibling_owns_place_address``,
                                           ``cod_exempt_cluster``
sibling owns the place address but owes    ``sibling_owns_place_address``
nothing  (the rule-3 gap)
debt-free coworker at an indebted place    ``debt_free_coworker``,
                                           ``three_member_place``
1 / 2 / 3+ members at a place              ``solo_ungrouped_debtor`` (0),
                                           ``a6_canonical`` (2),
                                           ``three_member_place`` (3)
one cluster, several places (E7)           ``two_places_one_cluster``
dissolved place + forwarding pointer       ``dissolved_place``
grocery member (forced personal scope)     ``grocery_at_place``
zero debt / zero balance participants      ``zero_everything``,
                                           ``debt_free_coworker``
=========================================  =========================================

ADDING A DIMENSION — do it in ONE place
---------------------------------------
The module is five sections and a new axis touches exactly one of them:

* §1 VOCABULARY — a new *input* axis is a field on a ``*Spec`` dataclass.
* §2 IDENTITY   — a new UNIQUE column gets one ``_ns_*`` deriver.
* §3 WRITERS    — a new table gets one ``_write_*`` function.
* §4 ORACLE     — a new *figure* is one property computed from the spec.
* §5 PRESETS    — a new shape is one entry in :data:`SCENARIOS`.

Never compute an expectation by calling ``business_app.services`` — that is the
failure mode this module exists to end.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup, CanonicalCustomer
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, Payment
from business_app.models.user import User, UserAddress
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    CashCollectionSource,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)

__all__ = [
    "AddressSpec",
    "BottleSpec",
    "BuiltScenario",
    "CreditSpec",
    "DebtSpec",
    "INHERIT",
    "PersonExpectation",
    "PersonSpec",
    "PlaceExpectation",
    "PlaceSpec",
    "SCENARIOS",
    "ScenarioNamespaceCollision",
    "ScenarioSpec",
    "build",
    "build_scenario",
]


# --------------------------------------------------------------------------- #
# §0  CONSTANTS
# --------------------------------------------------------------------------- #

#: Every timestamp the factory writes is derived from this. Nothing reads the
#: wall clock, so a scenario built today and a scenario built next year are the
#: same rows — and `created_at`-ordered readers (oldest-first allocation) are
#: reproducible.
BASE_TIME = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)

#: Password behind every generated user's hash, for tests that need to log in.
FACTORY_PASSWORD = "TestPassword123!"

_MONEY = Decimal("0.01")

#: Mirrors ``CashCollectionService._TERMINAL_ORDER_STATUSES``. Restated rather
#: than imported ON PURPOSE: the oracle must not import its definition of
#: "collectible" from the code it is used to judge.
_TERMINAL_ORDER_STATUSES = frozenset({OrderStatus.CANCELLED, OrderStatus.RETURNED})

#: Statuses that ``ck_orders_address_required_after_pending`` (Postgres) forbids
#: without a ``delivery_address_id``.
_STATUSES_REQUIRING_ADDRESS = frozenset(
    {
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.DELIVERED,
        OrderStatus.RETURNED,
    }
)

#: Inside TASHKENT_POLYGON — ``UserAddress`` has a before_insert zone guard, so
#: an out-of-zone coordinate is a hard failure, not a soft one.
_LAT0, _LNG0 = 41.3100, 69.2800

#: Namespaces 1..99 are reserved for the presets in §5; ad-hoc specs derive
#: theirs from a stable CRC of the name inside 100..999.
_PRESET_NS_MAX = 99
_ADHOC_NS_LO, _ADHOC_NS_HI = 100, 999


class ScenarioNamespaceCollision(RuntimeError):
    """Raised when a scenario's generated identities already exist in the DB."""


class _Inherit:
    """Sentinel: 'use the address's own live place group'."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "INHERIT"


INHERIT = _Inherit()


def _money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(_MONEY)


@lru_cache(maxsize=1)
def _factory_password_hash() -> str:
    """One bcrypt call per process. 12 rounds is ~250 ms; a scenario with five
    users would otherwise spend a second hashing identical passwords."""
    return hash_password(FACTORY_PASSWORD)


# --------------------------------------------------------------------------- #
# §1  VOCABULARY — the declarative spec. A new INPUT axis is a field here.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PersonSpec:
    """One phone account.

    ``linked_to`` names another person key; the two (and anything transitively
    linked to either) become ONE canonical customer — one real person holding
    several phone accounts. That is the cluster every "own debt" figure sums
    over.
    """

    key: str
    linked_to: Optional[str] = None
    cod_exempt: bool = False
    grocery: bool = False
    first_name: Optional[str] = None
    last_name: Optional[str] = None


@dataclass(frozen=True)
class AddressSpec:
    """One delivery address.

    ``place`` names a :class:`PlaceSpec` key, or ``None`` for an ungrouped
    address. Ungrouped-vs-grouped is the single input that decides whether the
    bottle scope is the address or the group, and whether the COD cap's place
    arm is evaluated at all — so it is the axis, not a detail.
    """

    key: str
    owner: str
    place: Optional[str] = None
    title: str = "home"


@dataclass(frozen=True)
class PlaceSpec:
    """One ``address_groups`` row — a shared physical place.

    ``dissolved_onto`` names the address key this place's history was released
    onto; setting it writes ``dissolved_onto_address_id`` and marks the place
    dissolved. A dissolved place keeps its ledger rows for ever and has (in the
    ordinary case) no live member addresses.
    """

    key: str
    label: str = "office"
    dissolved_onto: Optional[str] = None


@dataclass(frozen=True)
class DebtSpec:
    """One order + its cash payment.

    ``status`` is the axis that widened defects #3 and #5: the allocation engine
    settles DELIVERED orders only, while several *display* paths summed every
    non-terminal order. A scenario that contains both shapes is the only one
    that can tell those two numbers apart.

    ``outstanding`` defaults to ``amount`` (nothing collected). ``outstanding=0``
    is a fully-settled debt, which the factory writes as a COMPLETED payment
    with a collector attached so the Postgres CHECK holds.
    """

    key: str
    owner: str
    amount: Any
    at: Optional[str] = None
    status: OrderStatus = OrderStatus.DELIVERED
    outstanding: Any = None

    def resolved_outstanding(self) -> Decimal:
        return _money(self.amount if self.outstanding is None else self.outstanding)

    def resolved_amount(self) -> Decimal:
        return _money(self.amount)


@dataclass(frozen=True)
class BottleSpec:
    """One ``bottle_ledger`` entry, and its contribution to a place's balance.

    The balance row is DERIVED from these, per SCOPE — the group when the
    address is grouped, else the address — exactly as ``BottleScope`` defines
    it. Two coworkers' bottles at one place therefore land in ONE balance row.

    ``frozen_group`` overrides the ledger row's ``address_group_id`` (the scope
    AT ISSUE). Use it to stamp history onto a place that has since dissolved —
    the shape ``dissolved_onto_address_id`` exists for. ``None`` forces an
    address-scoped entry even for a grouped address.
    """

    key: str
    at: str
    quantity: Any
    event_type: BottleLedgerEventType = BottleLedgerEventType.DELIVERY
    frozen_group: Any = INHERIT


@dataclass(frozen=True)
class CreditSpec:
    """Unapplied COD over-collection credit — one person's prepaid wallet.

    Credit is per-USER and pooled across a canonical cluster; a place NEVER
    pools credit (coworkers are different people). A grocery account's wallet is
    its own, even when linked.
    """

    key: str
    owner: str
    unapplied: Any
    source: CashCollectionSource = CashCollectionSource.STANDALONE_MEETING


@dataclass(frozen=True)
class ScenarioSpec:
    """A whole world, declared. Order of ``debts`` is order of age."""

    name: str
    people: Tuple[PersonSpec, ...] = ()
    places: Tuple[PlaceSpec, ...] = ()
    addresses: Tuple[AddressSpec, ...] = ()
    debts: Tuple[DebtSpec, ...] = ()
    bottles: Tuple[BottleSpec, ...] = ()
    credits: Tuple[CreditSpec, ...] = ()
    namespace: Optional[int] = None
    doc: str = ""


# --------------------------------------------------------------------------- #
# §2  IDENTITY — every UNIQUE column gets exactly one deriver here.
# --------------------------------------------------------------------------- #


def _derive_namespace(name: str) -> int:
    span = _ADHOC_NS_HI - _ADHOC_NS_LO + 1
    return _ADHOC_NS_LO + (zlib.crc32(name.encode("utf-8")) % span)


def _ns_phone(ns: int, index: int) -> str:
    # +998 77 <ns:3> <index:4> — 9 national digits, operator block 77, disjoint
    # from conftest's +99890123456x and _scope_money_helpers' +99890xxxxxxx.
    return f"+99877{ns:03d}{index:04d}"


def _ns_email(ns: int, key: str) -> str:
    return f"pf{ns:03d}.{key}@place-factory.test"


def _ns_order_number(ns: int, index: int) -> str:
    return f"PF{ns:03d}-ORD-{index:03d}"


def _ns_payment_id(ns: int, index: int) -> str:
    return f"pf{ns:03d}-pay-{index:03d}"


def _ns_event_id(ns: int, index: int) -> str:
    return f"pf{ns:03d}-evt-{index:03d}"


def _ns_ledger_key(ns: int, index: int) -> str:
    return f"pf{ns:03d}-bl-{index:03d}"


def _ns_coords(index: int) -> Tuple[float, float]:
    # A few metres apart, all comfortably inside TASHKENT_POLYGON.
    return round(_LAT0 + index * 0.0002, 6), round(_LNG0 + index * 0.0002, 6)


# --------------------------------------------------------------------------- #
# §3  WRITERS — one function per table. A new table is one function here.
# --------------------------------------------------------------------------- #


def _write_places(db, spec: ScenarioSpec) -> Dict[str, AddressGroup]:
    groups: Dict[str, AddressGroup] = {}
    for place in spec.places:
        group = AddressGroup(
            label=place.label,
            canonical_customer_id=None,  # deprecated + nullable since f7c3b9e1d5a2
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        db.session.add(group)
        groups[place.key] = group
    db.session.flush()
    return groups


def _write_people(db, spec: ScenarioSpec, ns: int) -> Dict[str, User]:
    users: Dict[str, User] = {}
    for index, person in enumerate(spec.people, start=1):
        user = User(
            email=_ns_email(ns, person.key),
            phone=_ns_phone(ns, index),
            password_hash=_factory_password_hash(),
            first_name=person.first_name or person.key.replace("_", " ").title(),
            last_name=f"NS{ns:03d}",
            user_type=UserType.ENTITY if person.grocery else UserType.INDIVIDUAL,
            role=UserRole.CUSTOMER,
            is_verified=True,
            cod_debt_check_exempt=bool(person.cod_exempt),
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        if person.grocery:
            user.entity_subtype = EntitySubtype.GROCERY_STORE
        db.session.add(user)
        users[person.key] = user
    db.session.flush()
    return users


def _write_clusters(db, spec: ScenarioSpec, users: Mapping[str, User]) -> Dict[str, CanonicalCustomer]:
    """One ``canonical_customers`` row per multi-account person."""
    canonicals: Dict[str, CanonicalCustomer] = {}
    for cluster_keys in _cluster_key_sets(spec):
        if len(cluster_keys) < 2:
            continue
        ordered = _in_spec_order(spec, cluster_keys)
        canonical = CanonicalCustomer(
            primary_user_id=users[ordered[0]].id,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        db.session.add(canonical)
        db.session.flush()
        for key in ordered:
            users[key].canonical_customer_id = canonical.id
            canonicals[key] = canonical
    db.session.flush()
    return canonicals


def _write_addresses(
    db, spec: ScenarioSpec, users: Mapping[str, User], groups: Mapping[str, AddressGroup]
) -> Dict[str, UserAddress]:
    addresses: Dict[str, UserAddress] = {}
    for index, addr in enumerate(spec.addresses, start=1):
        latitude, longitude = _ns_coords(index)
        row = UserAddress(
            user_id=users[addr.owner].id,
            address_group_id=groups[addr.place].id if addr.place else None,
            title=addr.title,
            full_address=f"{index} {addr.key.replace('_', ' ').title()} St, Tashkent",
            street_address=f"{index} {addr.key.replace('_', ' ').title()} St",
            city="Tashkent",
            latitude=latitude,
            longitude=longitude,
            is_default=(index == 1),
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        db.session.add(row)
        addresses[addr.key] = row
    db.session.flush()
    return addresses


def _write_dissolve_pointers(
    db, spec: ScenarioSpec, groups: Mapping[str, AddressGroup], addresses: Mapping[str, UserAddress]
) -> None:
    for place in spec.places:
        if place.dissolved_onto is None:
            continue
        groups[place.key].dissolved_onto_address_id = addresses[place.dissolved_onto].id
    db.session.flush()


def _write_collector(db, ns: int) -> User:
    """A driver, created only when a scenario has a settled cash payment.

    ``ck_payments_cash_completed_requires_collector`` (Postgres) rejects a
    COMPLETED cash payment with a NULL ``collected_by``; SQLite would let it
    through and the scenario would stop being portable.
    """
    collector = User(
        email=_ns_email(ns, "collector"),
        phone=_ns_phone(ns, 9000),
        password_hash=_factory_password_hash(),
        first_name="Collector",
        last_name=f"NS{ns:03d}",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    db.session.add(collector)
    db.session.flush()
    return collector


def _write_debts(
    db,
    spec: ScenarioSpec,
    ns: int,
    users: Mapping[str, User],
    addresses: Mapping[str, UserAddress],
) -> Tuple[Dict[str, Order], Dict[str, Payment], Optional[User]]:
    orders: Dict[str, Order] = {}
    payments: Dict[str, Payment] = {}
    collector: Optional[User] = None

    for index, debt in enumerate(spec.debts, start=1):
        amount = debt.resolved_amount()
        outstanding = debt.resolved_outstanding()
        collected = amount - outstanding
        occurred = BASE_TIME + timedelta(minutes=index)

        if debt.status in _STATUSES_REQUIRING_ADDRESS and debt.at is None:
            raise ValueError(
                f"{spec.name}/{debt.key}: status {debt.status.value} requires an address "
                "(ck_orders_address_required_after_pending)"
            )

        order = Order(
            order_number=_ns_order_number(ns, index),
            user_id=users[debt.owner].id,
            status=debt.status,
            subtotal=amount,
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=amount,
            payment_method=PaymentMethod.CASH,
            delivery_address_id=addresses[debt.at].id if debt.at else None,
            order_source="web",
            is_paid=outstanding == Decimal("0.00"),
            created_at=occurred,
            updated_at=occurred,
        )
        db.session.add(order)
        db.session.flush()

        if outstanding == Decimal("0.00"):
            status = PaymentStatus.COMPLETED
            if collector is None:
                collector = _write_collector(db, ns)
            collected_by = collector.id
        elif collected > Decimal("0.00"):
            status, collected_by = PaymentStatus.PARTIALLY_PAID, None
        else:
            status, collected_by = PaymentStatus.PENDING, None

        payment = Payment(
            order_id=order.id,
            user_id=users[debt.owner].id,
            payment_method=PaymentMethod.CASH,
            amount=amount,
            currency="UZS",
            status=status,
            payment_id=_ns_payment_id(ns, index),
            amount_collected=collected,
            outstanding_amount=outstanding,
            collected_by=collected_by,
            created_at=occurred,
            updated_at=occurred,
        )
        db.session.add(payment)
        orders[debt.key] = order
        payments[debt.key] = payment

    db.session.flush()
    return orders, payments, collector


def _bottle_scope_key(
    spec: ScenarioSpec, bottle: BottleSpec, addr_by_key: Mapping[str, AddressSpec]
) -> Tuple[str, str]:
    """``("group", place_key)`` or ``("address", address_key)`` for one entry."""
    if isinstance(bottle.frozen_group, _Inherit):
        place = addr_by_key[bottle.at].place
    else:
        place = bottle.frozen_group
    return ("group", place) if place else ("address", bottle.at)


def _write_bottles(
    db,
    spec: ScenarioSpec,
    ns: int,
    users: Mapping[str, User],
    addresses: Mapping[str, UserAddress],
    groups: Mapping[str, AddressGroup],
) -> Tuple[Dict[str, BottleLedger], Dict[Tuple[str, str], BottleBalance]]:
    addr_by_key = {a.key: a for a in spec.addresses}
    ledger_rows: Dict[str, BottleLedger] = {}
    running: Dict[Tuple[str, str], Decimal] = {}

    for index, bottle in enumerate(spec.bottles, start=1):
        scope = _bottle_scope_key(spec, bottle, addr_by_key)
        quantity = _money(bottle.quantity)
        running[scope] = running.get(scope, Decimal("0.00")) + quantity
        occurred = BASE_TIME + timedelta(hours=1, minutes=index)
        row = BottleLedger(
            user_id=users[addr_by_key[bottle.at].owner].id,
            address_id=addresses[bottle.at].id,
            address_group_id=groups[scope[1]].id if scope[0] == "group" else None,
            event_type=bottle.event_type,
            quantity=quantity,
            balance_after=running[scope],
            occurred_at=occurred,
            idempotency_key=_ns_ledger_key(ns, index),
            entry_metadata={"place_state_factory": spec.name, "bottle_key": bottle.key},
            created_at=occurred,
            updated_at=occurred,
        )
        db.session.add(row)
        ledger_rows[bottle.key] = row

    balances: Dict[Tuple[str, str], BottleBalance] = {}
    for scope, total in running.items():
        balance = BottleBalance(
            address_group_id=groups[scope[1]].id if scope[0] == "group" else None,
            address_id=addresses[scope[1]].id if scope[0] == "address" else None,
            balance=total,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        db.session.add(balance)
        balances[scope] = balance

    db.session.flush()
    return ledger_rows, balances


def _write_credits(
    db, spec: ScenarioSpec, ns: int, users: Mapping[str, User]
) -> Dict[str, CashCollectionEvent]:
    events: Dict[str, CashCollectionEvent] = {}
    for index, credit in enumerate(spec.credits, start=1):
        unapplied = _money(credit.unapplied)
        occurred = BASE_TIME + timedelta(hours=2, minutes=index)
        event = CashCollectionEvent(
            event_id=_ns_event_id(ns, index),
            customer_id=users[credit.owner].id,
            amount=unapplied,
            currency="UZS",
            source=credit.source,
            occurred_at=occurred,
            unapplied_amount=unapplied,
            idempotency_key=f"{_ns_event_id(ns, index)}:idem",
            scope_type="personal",
            scope_snapshot=None,
            entry_metadata={"place_state_factory": spec.name, "credit_key": credit.key},
            created_at=occurred,
            updated_at=occurred,
        )
        db.session.add(event)
        events[credit.key] = event
    db.session.flush()
    return events


# --------------------------------------------------------------------------- #
# §4  ORACLE — the expected money, computed from the SPEC ALONE.
#
#     Nothing below may import or call business_app.services. Every figure is
#     restated from its DEFINITION, so a test comparing production to these
#     numbers is comparing two independent derivations rather than one
#     derivation to itself.
# --------------------------------------------------------------------------- #


def _cluster_key_sets(spec: ScenarioSpec) -> List[FrozenSet[str]]:
    """Union-find over ``PersonSpec.linked_to`` → the canonical clusters."""
    parent: Dict[str, str] = {p.key: p.key for p in spec.people}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for person in spec.people:
        if person.linked_to:
            a, b = find(person.key), find(person.linked_to)
            if a != b:
                parent[b] = a

    buckets: Dict[str, Set[str]] = {}
    for person in spec.people:
        buckets.setdefault(find(person.key), set()).add(person.key)
    return [frozenset(v) for v in buckets.values()]


def _in_spec_order(spec: ScenarioSpec, keys: Iterable[str]) -> List[str]:
    order = {p.key: i for i, p in enumerate(spec.people)}
    return sorted(keys, key=lambda k: order[k])


def _cluster_of(spec: ScenarioSpec, person_key: str) -> FrozenSet[str]:
    for cluster in _cluster_key_sets(spec):
        if person_key in cluster:
            return cluster
    return frozenset({person_key})


def _is_open_delivered(debt: DebtSpec) -> bool:
    """The engine's ring definition: CASH, DELIVERED, outstanding > 0."""
    return debt.status == OrderStatus.DELIVERED and debt.resolved_outstanding() > Decimal("0.00")


@dataclass(frozen=True)
class PlaceExpectation:
    """What a place is worth, declared by its construction."""

    place_key: str
    group_id: int
    label: str
    is_dissolved: bool
    dissolved_onto_address_id: Optional[int]
    address_ids: Tuple[int, ...]
    member_user_ids: Tuple[int, ...]
    #: Distinct address OWNERS — the definition both the place statement and the
    #: place debtor row use. Two grouped addresses of one person is ONE member.
    member_count: int
    #: Σ outstanding over open DELIVERED cash debts delivered to member
    #: addresses, ANY owner. This is what a place OWES; it is NOT what any one
    #: person can collect (see :attr:`PersonExpectation.collectible_total`).
    open_cod_total: Decimal
    open_cod_debt_count: int
    #: One pool per place, never per coworker (BottleScope).
    bottle_balance: Decimal


@dataclass(frozen=True)
class PersonExpectation:
    """What one real person is worth — every figure a surface might show.

    The five defects all lived in the gap between two of these. They are
    published side by side so a test can name which one a screen is showing and
    which one the engine will settle.
    """

    person_key: str
    user_id: int
    cluster_user_ids: Tuple[int, ...]
    is_cod_exempt: bool
    is_grocery: bool

    #: ⚠️ THE HEADLINE THAT MISLEADS. Per-ACCOUNT, every non-terminal order —
    #: PENDING included. ``get_customer_cod_statement["total_outstanding_amount"]``.
    #: Cash offered against a PENDING order settles nothing, so a surface that
    #: shows this and posts a collection is advertising money it cannot take.
    account_outstanding_amount: Decimal

    #: Per-ACCOUNT, DELIVERED-only, outstanding > 0 — the personal ring.
    account_delivered_outstanding: Decimal
    account_delivered_debt_count: int

    #: The person's OWN debt: cluster-wide, DELIVERED-only, outstanding > 0.
    cluster_delivered_outstanding: Decimal
    cluster_delivered_debt_count: int

    #: Open DELIVERED debt at this person's place(s) owned by SOMEONE ELSE.
    #: Zero unless a coworker owes something — which is exactly why a fixture
    #: without an indebted coworker cannot tell a union from a max.
    foreign_place_outstanding: Decimal
    foreign_place_debt_count: int

    #: 🔴 THE NUMBER. own ∪ coworkers' — what ONE place-scoped collection posted
    #: by this person actually settles (ring 1 ∪ ring 2). Equals the debtor row
    #: AND the collect ceiling, which must be one calculation and not two.
    #:
    #: ⚠️ This is a fact about the PLACE's topology, so it is stated for every
    #: member of one — including an account the engine refuses a place scope to
    #: (a grocery). For "what may this person be shown and offered" read
    #: :attr:`collect_scope_amount`; for "what will a post settle" read
    #: :attr:`engine_settleable_total`. Those two must agree; this one need not.
    collectible_total: Decimal
    collectible_debt_count: int
    #: The same union restricted to one place, for a cluster spanning several.
    collectible_by_place: Mapping[str, Tuple[Decimal, int]]

    place_keys: Tuple[str, ...]
    place_group_ids: Tuple[int, ...]
    grouped_address_ids: Tuple[int, ...]

    #: What the surface SHOWS and POSTS (``resolve_collect_scope``): PLACE only
    #: when the cluster owns exactly one place AND the engine grants a place
    #: scope for it, else CLUSTER — and the address is dropped together with the
    #: figure, never one without the other.
    collect_scope_type: str
    collect_scope_amount: Decimal
    collect_scope_debt_count: int
    collect_scope_address_ids: Tuple[int, ...]

    #: What the ENGINE resolves for door cash posted at that address
    #: (``resolve_allocation_scope``). A grocery account is forced PERSONAL
    #: however it is grouped, because its money is mirrored onto a corporate
    #: contract; :attr:`collect_scope_type` is DERIVED from this rather than
    #: racing it, so the display can no longer offer a place the engine refuses.
    engine_scope_type: str

    #: 🔴 WHERE THE MONEY ACTUALLY LANDS. The debt a real collection posted by
    #: this person settles, under :attr:`engine_scope_type`:
    #: personal → this account's own delivered debt; cluster → the cluster's;
    #: place → the union. This EQUALS :attr:`collect_scope_amount` for every
    #: person of every preset — no exceptions, and
    #: ``test_the_engine_settles_what_the_surface_offers`` asserts it across the
    #: whole catalogue. Where the two ever diverge, a surface is showing a number
    #: the engine will not honour, which is the whole subject of this effort.
    #: Assert money against THIS.
    engine_settleable_total: Decimal
    engine_settleable_debt_count: int

    #: Is this PERSON on the staff COD debtors list, and at what figure?
    #: ``expected_row_total`` is ``collectible_total`` when the place is
    #: unambiguous, the un-widened cluster figure when it is not (decision E7),
    #: and the foreign half alone for a coworker who owes nothing personally.
    #:
    #: ⚠️ ONE PERSON, ONE ROW. For a linked cluster the list carries a single
    #: row identified by ONE of :attr:`cluster_user_ids` — the account with the
    #: largest own debt — so a debt-free sibling reports
    #: ``expected_row_present=True`` and yet has no row keyed on HER id. Assert
    #: ``row["id"] in expected.cluster_user_ids``, never ``== user_id``.
    expected_row_present: bool
    expected_row_total: Decimal
    expected_row_debt_count: int
    expected_row_is_synthesised: bool

    #: Cluster-fungible unapplied over-collection credit. A place never pools
    #: credit; a grocery account's wallet is its own.
    prepaid_credit: Decimal


def _person_expectation(
    spec: ScenarioSpec,
    person: PersonSpec,
    users: Mapping[str, User],
    addresses: Mapping[str, UserAddress],
    groups: Mapping[str, AddressGroup],
) -> PersonExpectation:
    cluster_keys = _cluster_of(spec, person.key)
    addr_by_key = {a.key: a for a in spec.addresses}
    people_by_key = {p.key: p for p in spec.people}

    # --- own debt -----------------------------------------------------------
    account_outstanding = sum(
        (d.resolved_outstanding() for d in spec.debts if d.owner == person.key and d.status not in _TERMINAL_ORDER_STATUSES),
        Decimal("0.00"),
    )
    own_open = [d for d in spec.debts if d.owner in cluster_keys and _is_open_delivered(d)]
    cluster_delivered = sum((d.resolved_outstanding() for d in own_open), Decimal("0.00"))
    account_open = [d for d in own_open if d.owner == person.key]
    account_delivered = sum((d.resolved_outstanding() for d in account_open), Decimal("0.00"))

    # --- the places this cluster OWNS an address in -------------------------
    place_keys = []
    for addr in spec.addresses:
        if addr.owner in cluster_keys and addr.place and addr.place not in place_keys:
            place_keys.append(addr.place)

    def _open_at_place(place_key: str) -> List[DebtSpec]:
        return [
            d
            for d in spec.debts
            if _is_open_delivered(d) and d.at is not None and addr_by_key[d.at].place == place_key
        ]

    # --- the foreign half, deduped across places ----------------------------
    foreign_keys: List[str] = []
    per_place: Dict[str, Tuple[Decimal, int]] = {}
    for place_key in place_keys:
        place_foreign = [d for d in _open_at_place(place_key) if d.owner not in cluster_keys]
        per_place[place_key] = (
            cluster_delivered + sum((d.resolved_outstanding() for d in place_foreign), Decimal("0.00")),
            len(own_open) + len(place_foreign),
        )
        for debt in place_foreign:
            if debt.key not in foreign_keys:
                foreign_keys.append(debt.key)
    debt_by_key = {d.key: d for d in spec.debts}
    foreign_total = sum((debt_by_key[k].resolved_outstanding() for k in foreign_keys), Decimal("0.00"))

    collectible = cluster_delivered + foreign_total
    collectible_count = len(own_open) + len(foreign_keys)

    # --- what the ENGINE resolves, decided FIRST ----------------------------
    # ``resolve_allocation_scope``: grocery is forced PERSONAL (layer-3 backstop
    # — contract-mirrored cash must never co-mingle), then place when the post
    # names a grouped member address, then cluster, then personal.
    #
    # ⚠️ ORDER MATTERS, AND IT IS THE SUBJECT OF INSTANCE #4. This block used to
    # come SECOND and read `collect_scope_type` — the display deciding what the
    # engine would do. It is now the other way round: the engine decides, and the
    # display below derives. That is the same inversion the production fix makes
    # (`place_widening_applies` asks `resolve_allocation_scope` instead of
    # mirroring its rules), so the oracle cannot drift from it by construction.
    unambiguous = len(place_keys) == 1
    engine_grants_place = unambiguous and not person.grocery
    if person.grocery:
        engine_scope_type = "personal"
    elif engine_grants_place:
        engine_scope_type = "place"
    elif len(cluster_keys) > 1:
        engine_scope_type = "cluster"
    else:
        engine_scope_type = "personal"

    # --- the show-and-post scope (E7: two places is ambiguity) --------------
    # A place is SHOWN and POSTED only where the engine grants one. A grocery
    # therefore degrades exactly like an ambiguous cluster: no widening, no
    # address — the figure and the address drop together, which is the rule the
    # whole degradation contract is built on.
    grouped_address_ids = tuple(
        sorted(addresses[a.key].id for a in spec.addresses if a.owner in cluster_keys and a.place)
    )
    if engine_grants_place:
        collect_scope_type = "place"
        collect_amount, collect_count = per_place[place_keys[0]]
        collect_addresses = tuple(
            sorted(
                addresses[a.key].id
                for a in spec.addresses
                if a.owner in cluster_keys and a.place == place_keys[0]
            )
        )
    else:
        collect_scope_type = "cluster"
        collect_amount, collect_count = cluster_delivered, len(own_open)
        collect_addresses = ()

    if engine_scope_type == "place":
        settleable, settleable_count = per_place[place_keys[0]]
    elif engine_scope_type == "cluster":
        settleable, settleable_count = cluster_delivered, len(own_open)
    else:
        settleable, settleable_count = account_delivered, len(account_open)

    # --- the debtor row ------------------------------------------------------
    has_engine_row = len(own_open) > 0
    place_has_open_debt = any(_open_at_place(k) for k in place_keys)
    if has_engine_row:
        row_present, row_synthesised = True, False
        row_total, row_count = (
            (collect_amount, collect_count) if engine_grants_place else (cluster_delivered, len(own_open))
        )
    elif engine_grants_place and place_has_open_debt and foreign_total > Decimal("0.00"):
        row_present, row_synthesised = True, True
        row_total, row_count = foreign_total, len(foreign_keys)
    else:
        row_present, row_synthesised = False, False
        row_total, row_count = Decimal("0.00"), 0

    # --- the wallet ----------------------------------------------------------
    credit_keys = {person.key} if person.grocery else set(cluster_keys)
    credit_keys -= {k for k in credit_keys if k != person.key and people_by_key[k].grocery}
    prepaid = sum(
        (_money(c.unapplied) for c in spec.credits if c.owner in credit_keys), Decimal("0.00")
    )

    return PersonExpectation(
        person_key=person.key,
        user_id=users[person.key].id,
        cluster_user_ids=tuple(sorted(users[k].id for k in cluster_keys)),
        is_cod_exempt=bool(person.cod_exempt),
        is_grocery=bool(person.grocery),
        account_outstanding_amount=_money(account_outstanding),
        account_delivered_outstanding=_money(account_delivered),
        account_delivered_debt_count=len(account_open),
        cluster_delivered_outstanding=_money(cluster_delivered),
        cluster_delivered_debt_count=len(own_open),
        foreign_place_outstanding=_money(foreign_total),
        foreign_place_debt_count=len(foreign_keys),
        collectible_total=_money(collectible),
        collectible_debt_count=collectible_count,
        collectible_by_place={k: (_money(v[0]), v[1]) for k, v in per_place.items()},
        place_keys=tuple(place_keys),
        place_group_ids=tuple(sorted(groups[k].id for k in place_keys)),
        grouped_address_ids=grouped_address_ids,
        collect_scope_type=collect_scope_type,
        collect_scope_amount=_money(collect_amount),
        collect_scope_debt_count=collect_count,
        collect_scope_address_ids=collect_addresses,
        engine_scope_type=engine_scope_type,
        engine_settleable_total=_money(settleable),
        engine_settleable_debt_count=settleable_count,
        expected_row_present=row_present,
        expected_row_total=_money(row_total),
        expected_row_debt_count=row_count,
        expected_row_is_synthesised=row_synthesised,
        prepaid_credit=_money(prepaid),
    )


def _place_expectation(
    spec: ScenarioSpec,
    place: PlaceSpec,
    users: Mapping[str, User],
    addresses: Mapping[str, UserAddress],
    groups: Mapping[str, AddressGroup],
) -> PlaceExpectation:
    addr_by_key = {a.key: a for a in spec.addresses}
    members = [a for a in spec.addresses if a.place == place.key]
    open_debts = [
        d
        for d in spec.debts
        if _is_open_delivered(d) and d.at is not None and addr_by_key[d.at].place == place.key
    ]
    bottle_total = sum(
        (
            _money(b.quantity)
            for b in spec.bottles
            if _bottle_scope_key(spec, b, addr_by_key) == ("group", place.key)
        ),
        Decimal("0.00"),
    )
    return PlaceExpectation(
        place_key=place.key,
        group_id=groups[place.key].id,
        label=place.label,
        is_dissolved=place.dissolved_onto is not None,
        dissolved_onto_address_id=(
            addresses[place.dissolved_onto].id if place.dissolved_onto else None
        ),
        address_ids=tuple(sorted(addresses[a.key].id for a in members)),
        member_user_ids=tuple(sorted({users[a.owner].id for a in members})),
        member_count=len({a.owner for a in members}),
        open_cod_total=_money(sum((d.resolved_outstanding() for d in open_debts), Decimal("0.00"))),
        open_cod_debt_count=len(open_debts),
        bottle_balance=_money(bottle_total),
    )


# --------------------------------------------------------------------------- #
# §5  PRESETS — a new SHAPE is one entry here.
# --------------------------------------------------------------------------- #

_PRESETS: Tuple[ScenarioSpec, ...] = (
    # ---- 11 ----------------------------------------------------------------
    ScenarioSpec(
        name="a6_canonical",
        namespace=11,
        doc=(
            "THE canonical A6 world, verbatim from the owner ruling: Alice owes "
            "10 000 at an ungrouped home AND 15 000 at office G; Bob owes 20 000 "
            "at G. Alice's collectible total is 45 000 and Bob's is 35 000 — the "
            "state in which max(own, cluster, place) = 35 000 finally diverges "
            "from the union. Bottles: 6 pooled at G, 2 at Alice's home."
        ),
        people=(PersonSpec("alice"), PersonSpec("bob")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("alice_home", owner="alice", title="home"),
            AddressSpec("alice_office", owner="alice", place="g", title="work"),
            AddressSpec("bob_office", owner="bob", place="g", title="work"),
        ),
        debts=(
            DebtSpec("alice_home", owner="alice", at="alice_home", amount="10000"),
            DebtSpec("alice_office", owner="alice", at="alice_office", amount="15000"),
            DebtSpec("bob_office", owner="bob", at="bob_office", amount="20000"),
        ),
        bottles=(
            BottleSpec("g_alice", at="alice_office", quantity="4"),
            BottleSpec("g_bob", at="bob_office", quantity="2"),
            BottleSpec("home", at="alice_home", quantity="2"),
        ),
    ),
    # ---- 12 ----------------------------------------------------------------
    ScenarioSpec(
        name="a6_with_pending",
        namespace=12,
        doc=(
            "A6 plus one PENDING 70 000 order for Alice. Her per-account headline "
            "becomes 95 000 while a collection can still settle only 45 000 — the "
            "measured admin-modal defect. The status axis, in one scenario."
        ),
        people=(PersonSpec("alice"), PersonSpec("bob")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("alice_home", owner="alice", title="home"),
            AddressSpec("alice_office", owner="alice", place="g", title="work"),
            AddressSpec("bob_office", owner="bob", place="g", title="work"),
        ),
        debts=(
            DebtSpec("alice_home", owner="alice", at="alice_home", amount="10000"),
            DebtSpec("alice_office", owner="alice", at="alice_office", amount="15000"),
            DebtSpec("bob_office", owner="bob", at="bob_office", amount="20000"),
            DebtSpec(
                "alice_pending",
                owner="alice",
                at="alice_home",
                amount="70000",
                status=OrderStatus.PENDING,
            ),
        ),
    ),
    # ---- 13 ----------------------------------------------------------------
    ScenarioSpec(
        name="debt_inside_place_only",
        namespace=13,
        doc=(
            "THE FIXTURE THAT AGREED BY CONSTRUCTION, kept deliberately. All debt "
            "is inside place G, so union == place total == max(...) == 35 000. A "
            "test that passes here and fails on a6_canonical has found the seam; "
            "one that only ever ran here proves nothing."
        ),
        people=(PersonSpec("alice"), PersonSpec("bob")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("alice_office", owner="alice", place="g", title="work"),
            AddressSpec("bob_office", owner="bob", place="g", title="work"),
        ),
        debts=(
            DebtSpec("alice_office", owner="alice", at="alice_office", amount="15000"),
            DebtSpec("bob_office", owner="bob", at="bob_office", amount="20000"),
        ),
    ),
    # ---- 14 ----------------------------------------------------------------
    ScenarioSpec(
        name="debt_outside_place_only",
        namespace=14,
        doc=(
            "The mirror image: Alice belongs to place G but owes only at her "
            "ungrouped home; the place itself owes nothing. Place total 0, "
            "Alice collectible 10 000, and Bob — a member of a debt-free place — "
            "is on no list at all."
        ),
        people=(PersonSpec("alice"), PersonSpec("bob")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("alice_home", owner="alice", title="home"),
            AddressSpec("alice_office", owner="alice", place="g", title="work"),
            AddressSpec("bob_office", owner="bob", place="g", title="work"),
        ),
        debts=(DebtSpec("alice_home", owner="alice", at="alice_home", amount="10000"),),
        bottles=(BottleSpec("g", at="bob_office", quantity="3"),),
    ),
    # ---- 15 ----------------------------------------------------------------
    ScenarioSpec(
        name="debt_free_coworker",
        namespace=15,
        doc=(
            "Alice owes 15 000 at office G. Bob is a member of G, owes nothing, "
            "has no orders — and must still be collectible-through, at 15 000 "
            "(the synthesised row). The shape rule 3 exists for."
        ),
        people=(PersonSpec("alice"), PersonSpec("bob")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("alice_office", owner="alice", place="g", title="work"),
            AddressSpec("bob_office", owner="bob", place="g", title="work"),
        ),
        debts=(DebtSpec("alice_office", owner="alice", at="alice_office", amount="15000"),),
        bottles=(BottleSpec("g", at="alice_office", quantity="5"),),
    ),
    # ---- 16 ----------------------------------------------------------------
    ScenarioSpec(
        name="sibling_owns_place_address",
        namespace=16,
        doc=(
            "THE RULE-3 GAP. One person, two phone accounts: alice_a owes 10 000 "
            "at her ungrouped home, alice_b owns the office address in G and owes "
            "NOTHING. Bob owes 20 000 at G. A composition that discovers places "
            "through the accounts that carry debt never finds G for this person "
            "and loses the 20 000; the cluster's collectible total is 30 000."
        ),
        people=(PersonSpec("alice_a"), PersonSpec("alice_b", linked_to="alice_a"), PersonSpec("bob")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("alice_home", owner="alice_a", title="home"),
            AddressSpec("alice_office", owner="alice_b", place="g", title="work"),
            AddressSpec("bob_office", owner="bob", place="g", title="work"),
        ),
        debts=(
            DebtSpec("alice_home", owner="alice_a", at="alice_home", amount="10000"),
            DebtSpec("bob_office", owner="bob", at="bob_office", amount="20000"),
        ),
        credits=(CreditSpec("alice_b_credit", owner="alice_b", unapplied="2500"),),
    ),
    # ---- 17 ----------------------------------------------------------------
    ScenarioSpec(
        name="three_member_place",
        namespace=17,
        doc=(
            "Three coworkers at one place: Ann owes 5 000 there plus 3 000 at her "
            "ungrouped home, Ben owes nothing, Cara owes 7 000 there. Place total "
            "12 000; collectibles 15 000 / 12 000 / 12 000 — three different "
            "numbers over ONE place, which a place-total-only surface cannot "
            "produce."
        ),
        people=(PersonSpec("ann"), PersonSpec("ben"), PersonSpec("cara")),
        places=(PlaceSpec("g", label="office"),),
        addresses=(
            AddressSpec("ann_home", owner="ann", title="home"),
            AddressSpec("ann_office", owner="ann", place="g", title="work"),
            AddressSpec("ben_office", owner="ben", place="g", title="work"),
            AddressSpec("cara_office", owner="cara", place="g", title="work"),
        ),
        debts=(
            DebtSpec("ann_home", owner="ann", at="ann_home", amount="3000"),
            DebtSpec("ann_office", owner="ann", at="ann_office", amount="5000"),
            DebtSpec("cara_office", owner="cara", at="cara_office", amount="7000"),
        ),
        bottles=(
            BottleSpec("g_ann", at="ann_office", quantity="3"),
            BottleSpec("g_cara", at="cara_office", quantity="4"),
        ),
    ),
    # ---- 18 ----------------------------------------------------------------
    ScenarioSpec(
        name="two_places_one_cluster",
        namespace=18,
        doc=(
            "Decision E7. Alice owns an address in G1 AND in G2, so no surface can "
            "name which place a collection is for. Her row and her ceiling must "
            "degrade TOGETHER to the un-widened cluster figure (10 000) with NO "
            "address — never keep the place scope while falling back on the "
            "number. The union across both places is 35 000; that number must "
            "appear nowhere."
        ),
        people=(PersonSpec("alice"), PersonSpec("bob"), PersonSpec("carol")),
        places=(PlaceSpec("g1", label="office one"), PlaceSpec("g2", label="office two")),
        addresses=(
            AddressSpec("alice_g1", owner="alice", place="g1", title="work"),
            AddressSpec("alice_g2", owner="alice", place="g2", title="second job"),
            AddressSpec("bob_g1", owner="bob", place="g1", title="work"),
            AddressSpec("carol_g2", owner="carol", place="g2", title="work"),
        ),
        debts=(
            DebtSpec("alice_g1", owner="alice", at="alice_g1", amount="10000"),
            DebtSpec("bob_g1", owner="bob", at="bob_g1", amount="20000"),
            DebtSpec("carol_g2", owner="carol", at="carol_g2", amount="5000"),
        ),
    ),
    # ---- 19 ----------------------------------------------------------------
    ScenarioSpec(
        name="dissolved_place",
        namespace=19,
        doc=(
            "A place that dissolved. G_old has NO live member addresses, carries "
            "the forwarding pointer to the survivor address, and still owns "
            "frozen ledger rows that net to zero (+6 delivered, -6 released). The "
            "survivor is ungrouped, holds the 6 bottles and owes 12 000."
        ),
        people=(PersonSpec("dana"),),
        places=(PlaceSpec("g_old", label="closed office", dissolved_onto="survivor"),),
        addresses=(AddressSpec("survivor", owner="dana", title="home"),),
        debts=(DebtSpec("survivor_debt", owner="dana", at="survivor", amount="12000"),),
        bottles=(
            BottleSpec("old_delivery", at="survivor", quantity="6", frozen_group="g_old"),
            BottleSpec(
                "old_release",
                at="survivor",
                quantity="-6",
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                frozen_group="g_old",
            ),
            BottleSpec("survivor_holds", at="survivor", quantity="6"),
        ),
    ),
    # ---- 20 ----------------------------------------------------------------
    ScenarioSpec(
        name="zero_everything",
        namespace=20,
        doc=(
            "One person, one ungrouped address, no debt, no bottles, no credit. "
            "Every figure zero and every list empty — the control that catches an "
            "assertion which passes because it is measuring nothing."
        ),
        people=(PersonSpec("nora"),),
        addresses=(AddressSpec("nora_home", owner="nora", title="home"),),
    ),
    # ---- 21 ----------------------------------------------------------------
    ScenarioSpec(
        name="solo_ungrouped_debtor",
        namespace=21,
        doc=(
            "The pre-place baseline: unlinked, ungrouped, one DELIVERED 15 000, "
            "one PENDING 5 000 and one fully SETTLED 9 000. Headline 20 000, "
            "collectible 15 000. Every place-aware figure must be byte-identical "
            "to the person-only one here."
        ),
        people=(PersonSpec("sam"),),
        addresses=(AddressSpec("sam_home", owner="sam", title="home"),),
        debts=(
            DebtSpec("sam_delivered", owner="sam", at="sam_home", amount="15000"),
            DebtSpec("sam_settled", owner="sam", at="sam_home", amount="9000", outstanding="0"),
            DebtSpec(
                "sam_pending",
                owner="sam",
                at="sam_home",
                amount="5000",
                status=OrderStatus.PENDING,
            ),
        ),
        credits=(CreditSpec("sam_credit", owner="sam", unapplied="1200"),),
    ),
    # ---- 22 ----------------------------------------------------------------
    ScenarioSpec(
        name="grocery_at_place",
        namespace=22,
        doc=(
            "A grocery entity sharing a place with an individual. The engine "
            "FORCES personal scope for the grocery account (its cash is mirrored "
            "onto a corporate contract and must never co-mingle), so Mart may be "
            "shown and offered only Mart's own 8 000 — never the place's 18 000 "
            "— while Alice, an ordinary individual at the SAME place, still gets "
            "the whole 18 000 union. That contrast is the point of the preset: "
            "the refusal is per-ACCOUNT, not per-place, so a blanket 'no widening "
            "at a place with a grocery in it' would pass the grocery half and "
            "silently break the coworker. "
            "HISTORY: the display did not know the rule and widened Mart to "
            "18 000 anyway; collecting that settled 8 000, parked 10 000 as "
            "Mart's prepaid credit, and re-offered the coworker's untouchable "
            "debt on every later lap. This preset was the ONE declared exception "
            "to collect_scope_amount == engine_settleable_total; the exception is "
            "gone and the invariant now holds catalogue-wide with no carve-out."
        ),
        people=(PersonSpec("alice"), PersonSpec("mart", grocery=True)),
        places=(PlaceSpec("g", label="plaza"),),
        addresses=(
            AddressSpec("alice_office", owner="alice", place="g", title="work"),
            AddressSpec("mart_shop", owner="mart", place="g", title="shop"),
        ),
        debts=(
            DebtSpec("alice_office", owner="alice", at="alice_office", amount="10000"),
            DebtSpec("mart_shop", owner="mart", at="mart_shop", amount="8000"),
        ),
    ),
    # ---- 23 ----------------------------------------------------------------
    ScenarioSpec(
        name="cod_exempt_cluster",
        namespace=23,
        doc=(
            "Two phone accounts, one person, two open DELIVERED debts — at the COD "
            "cap — but one member is admin-exempt, and exemption is OR-ed across "
            "the cluster. No place. Credit is pooled across the two accounts."
        ),
        people=(
            PersonSpec("vip_a", cod_exempt=True),
            PersonSpec("vip_b", linked_to="vip_a"),
        ),
        addresses=(
            AddressSpec("vip_a_home", owner="vip_a", title="home"),
            AddressSpec("vip_b_home", owner="vip_b", title="flat"),
        ),
        debts=(
            DebtSpec("vip_a_debt", owner="vip_a", at="vip_a_home", amount="30000"),
            DebtSpec("vip_b_debt", owner="vip_b", at="vip_b_home", amount="40000"),
        ),
        credits=(
            CreditSpec("vip_a_credit", owner="vip_a", unapplied="1000"),
            CreditSpec("vip_b_credit", owner="vip_b", unapplied="500"),
        ),
    ),
)

SCENARIOS: Mapping[str, ScenarioSpec] = {s.name: s for s in _PRESETS}


# --------------------------------------------------------------------------- #
# §6  BUILD
# --------------------------------------------------------------------------- #


@dataclass
class BuiltScenario:
    """Real rows, plus the oracle that says what they are worth."""

    spec: ScenarioSpec
    namespace: int
    users: Dict[str, User]
    addresses: Dict[str, UserAddress]
    groups: Dict[str, AddressGroup]
    orders: Dict[str, Order]
    payments: Dict[str, Payment]
    ledger: Dict[str, BottleLedger]
    balances: Dict[Tuple[str, str], BottleBalance]
    credits: Dict[str, CashCollectionEvent]
    canonicals: Dict[str, CanonicalCustomer]
    collector: Optional[User]
    people: Dict[str, PersonExpectation] = field(default_factory=dict)
    places: Dict[str, PlaceExpectation] = field(default_factory=dict)

    # -- handles ------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.spec.name

    def user(self, key: str) -> User:
        return self.users[key]

    def address(self, key: str) -> UserAddress:
        return self.addresses[key]

    def place(self, key: str) -> AddressGroup:
        return self.groups[key]

    def order(self, key: str) -> Order:
        return self.orders[key]

    def payment(self, key: str) -> Payment:
        return self.payments[key]

    def credit(self, key: str) -> CashCollectionEvent:
        return self.credits[key]

    # -- oracle -------------------------------------------------------------
    def expect(self, person_key: str) -> PersonExpectation:
        """What this person is worth, per the scenario's own construction."""
        return self.people[person_key]

    def place_expect(self, place_key: str) -> PlaceExpectation:
        """What this place is worth, per the scenario's own construction."""
        return self.places[place_key]

    def bottle_balance_row(self, *, place: Optional[str] = None, address: Optional[str] = None) -> Optional[BottleBalance]:
        """The single ``bottle_balances`` row for a scope, or None if untouched."""
        if (place is None) == (address is None):
            raise ValueError("pass exactly one of place= / address=")
        return self.balances.get(("group", place) if place else ("address", address))


def _assert_spec_is_well_formed(spec: ScenarioSpec) -> None:
    people = {p.key for p in spec.people}
    places = {p.key for p in spec.places}
    addresses = {a.key for a in spec.addresses}
    _unique(spec, "people", [p.key for p in spec.people])
    _unique(spec, "places", [p.key for p in spec.places])
    _unique(spec, "addresses", [a.key for a in spec.addresses])
    _unique(spec, "debts", [d.key for d in spec.debts])
    _unique(spec, "bottles", [b.key for b in spec.bottles])
    _unique(spec, "credits", [c.key for c in spec.credits])
    for person in spec.people:
        if person.linked_to and person.linked_to not in people:
            raise ValueError(f"{spec.name}: person {person.key!r} links to unknown {person.linked_to!r}")
    for place in spec.places:
        if place.dissolved_onto and place.dissolved_onto not in addresses:
            raise ValueError(f"{spec.name}: place {place.key!r} dissolves onto unknown {place.dissolved_onto!r}")
    for addr in spec.addresses:
        if addr.owner not in people:
            raise ValueError(f"{spec.name}: address {addr.key!r} owned by unknown {addr.owner!r}")
        if addr.place and addr.place not in places:
            raise ValueError(f"{spec.name}: address {addr.key!r} in unknown place {addr.place!r}")
    for debt in spec.debts:
        if debt.owner not in people:
            raise ValueError(f"{spec.name}: debt {debt.key!r} owned by unknown {debt.owner!r}")
        if debt.at and debt.at not in addresses:
            raise ValueError(f"{spec.name}: debt {debt.key!r} at unknown address {debt.at!r}")
    for bottle in spec.bottles:
        if bottle.at not in addresses:
            raise ValueError(f"{spec.name}: bottle {bottle.key!r} at unknown address {bottle.at!r}")
        if not isinstance(bottle.frozen_group, _Inherit) and bottle.frozen_group is not None:
            if bottle.frozen_group not in places:
                raise ValueError(
                    f"{spec.name}: bottle {bottle.key!r} frozen to unknown place {bottle.frozen_group!r}"
                )
    for credit in spec.credits:
        if credit.owner not in people:
            raise ValueError(f"{spec.name}: credit {credit.key!r} owned by unknown {credit.owner!r}")


def _unique(spec: ScenarioSpec, what: str, keys: Sequence[str]) -> None:
    seen: Set[str] = set()
    dupes: Set[str] = set()
    for key in keys:
        (dupes if key in seen else seen).add(key)
    if dupes:
        raise ValueError(f"{spec.name}: duplicate {what} keys {sorted(dupes)}")


def _assert_namespace_is_free(db, spec: ScenarioSpec, ns: int) -> None:
    phones = [_ns_phone(ns, i) for i in range(1, len(spec.people) + 1)] + [_ns_phone(ns, 9000)]
    clash = db.session.query(User.phone).filter(User.phone.in_(phones)).first()
    if clash is not None:
        raise ScenarioNamespaceCollision(
            f"namespace {ns} is already populated in this database (phone {clash[0]} exists). "
            f"Build {spec.name!r} once per database, or pass an explicit distinct "
            f"`namespace=` to build a second copy."
        )


def build_scenario(db, spec: ScenarioSpec, *, namespace: Optional[int] = None) -> BuiltScenario:
    """Write ``spec`` as real rows and return it with its oracle attached."""
    _assert_spec_is_well_formed(spec)
    ns = namespace if namespace is not None else spec.namespace
    if ns is None:
        ns = _derive_namespace(spec.name)
    if not 0 <= ns <= _ADHOC_NS_HI:
        raise ValueError(f"namespace must be 0..{_ADHOC_NS_HI}, got {ns}")
    _assert_namespace_is_free(db, spec, ns)

    groups = _write_places(db, spec)
    users = _write_people(db, spec, ns)
    canonicals = _write_clusters(db, spec, users)
    addresses = _write_addresses(db, spec, users, groups)
    _write_dissolve_pointers(db, spec, groups, addresses)
    orders, payments, collector = _write_debts(db, spec, ns, users, addresses)
    ledger, balances = _write_bottles(db, spec, ns, users, addresses, groups)
    credits = _write_credits(db, spec, ns, users)
    db.session.commit()

    built = BuiltScenario(
        spec=spec,
        namespace=ns,
        users=users,
        addresses=addresses,
        groups=groups,
        orders=orders,
        payments=payments,
        ledger=ledger,
        balances=balances,
        credits=credits,
        canonicals=canonicals,
        collector=collector,
    )
    built.people = {
        p.key: _person_expectation(spec, p, users, addresses, groups) for p in spec.people
    }
    built.places = {
        p.key: _place_expectation(spec, p, users, addresses, groups) for p in spec.places
    }
    return built


def build(db, name: str, *, namespace: Optional[int] = None) -> BuiltScenario:
    """Build a named preset from :data:`SCENARIOS`."""
    try:
        spec = SCENARIOS[name]
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}") from None
    return build_scenario(db, spec, namespace=namespace)
