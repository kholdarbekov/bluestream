"""REAL POSTGRES: FK integrity, DDL-declared constraints, and LOCK ORDERING
for the place-keyed bottle machinery (Plans A / D / C).

WHY THIS FILE EXISTS AT ALL
---------------------------
Every other test in this feature runs on in-memory SQLite, where

  * ``PRAGMA foreign_keys`` is OFF — a dangling FK writes happily;
  * ``with_for_update()`` compiles to nothing — a "locking" test proves nothing;
  * ``NUMERIC(12,2)`` round-trips through a C double;
  * the schema comes from ``db.create_all()`` on the MODELS, not from the
    migration chain that production actually ran.

So a green SQLite suite is silent about exactly the four things the place
lifecycle rests on. This file runs against a fresh database created by
``flask db upgrade head`` — the real migrations — and every claim below is one
SQLite structurally cannot make.

WHAT IS PROVEN HERE
-------------------
1. DDL truth: ``ck_bottle_balance_scope``, ``uq_bottle_balance_group`` and
   ``uq_bottle_balance_addr`` exist *in the migrated schema* and fire; the
   two-column scope design depends on Postgres treating NULLs as DISTINCT in a
   UNIQUE index, and that is asserted directly.
2. FK integrity: which constraint blocks which delete. The §7.3 "keep the
   memberless AddressGroup row" decision is not a preference — three real FKs
   (``bottle_ledger``, ``bottle_fines``, and the balance row's own) decide it.
   The §7.3 application-level DELETE FENCE is proven to be the ONLY thing
   standing between a grouped address and deletion, by showing the raw SQL
   delete SUCCEEDS in a savepoint.
3. Locking, deterministically: an independent psycopg2 connection holds one row
   ``FOR UPDATE``, the session under test runs with ``SET LOCAL lock_timeout``,
   and the service call is expected to time out. No polling, no sleeps, no
   thread scheduling assumptions. One test (the group-before-address ORDER
   proof) additionally needs a thread plus a ``FOR UPDATE NOWAIT`` prober,
   because it is the only construction that can distinguish the two orders.
4. Real concurrency invariants: two sessions, real transactions, real
   READ COMMITTED. This is where the unlocked membership count in
   ``_dissolve_if_last_member`` and the unlocked split-cap read in
   ``_validated_bottles_leaving`` are demonstrated.
5. NUMERIC exactness: the merge-review convergence guarantee
   (``get_place_balance == SUM(bottle_ledger.quantity)``) on real
   ``NUMERIC(12,2)``, where 0.33 + 0.01 cannot hide behind a float.

TEST-INFRA CHOICE, STATED PLAINLY
---------------------------------
``tests/integration/conftest.py``'s ``pg_app`` is FUNCTION-scoped: every test
pays a full ``alembic upgrade head``. This file needs ~50 Postgres tests, and
50 migration runs is minutes of wall clock for zero extra signal. It therefore
declares its OWN MODULE-scoped app over the same ephemeral-database helpers
from that conftest (``_resolve_database_url`` / ``_admin_engine_for``), and
pushes a FRESH app context per test — which, under Flask-SQLAlchemy 3, means a
fresh session per test, so no ORM identity state leaks. ``pytest.ini`` pins
``--dist=loadfile``, so the whole module lands on one xdist worker and the
module-scoped database is never shared across processes.

The consequence is that COMMITTED rows survive between tests in this module.
Every assertion below is therefore SCOPED to the entities the test itself
created (a group id, an address id, a scope filter) — there is no global
``count(*)`` or global Σ anywhere. Two tests deliberately break that rule and
both scope it: they compare a Σ over an explicit list of scope ids.

NOTHING HERE HAND-BUILDS A ``BottleBalance`` ROW except where the row itself is
the subject (the three constraint-violation tests, which is the only way to
attempt an illegal row at all). Every balance figure comes from
``record_bottles_delivered`` / ``record_bottles_returned`` /
``admin_adjust_balance`` / the real ``CustomerLinkService`` lifecycle / real
HTTP routes with real JWTs.

DEFECTS THIS FILE CONFIRMED (each kept as a strict xfail so it stays visible)
-----------------------------------------------------------------------------
1. **A real 40P01 deadlock** between two concurrent place-group removals. The
   lifecycle takes a THIRD lock resource — ``addresses`` row write-locks — on
   BOTH sides of the ``bottle_balances`` locks, which no ordering docstring
   mentions. Two removals from a two-member place form a textbook ABBA.
2. A concurrent delivery during a join is absorbed into the BALANCE but not into
   the LEDGER, manufacturing a permanent drift and hiding a real delivery from
   the place's history.
3. Two concurrent removals from a THREE-member place MISS the §7.3 dissolve
   entirely, leaving a one-member place group behind.
4. The §7.1 split cap is validated outside the lock, so a concurrent return
   drives a place negative through a "validated" split.
5. An out-of-range ``resultingBalance`` is a 500, not a 400.
6. A dissolved group id can be re-populated through a live route, which inherits
   a departed customer's residual ledger (and leaks their delivery history to the
   new members).
7. Order deletion cascades away ``bottle_ledger`` rows without reversing the
   balance.
8. A derived place-level adjustment writes an incoherent
   ``(user_id, address_id)`` pair, so a customer sees a coworker credited with a
   correction they did not cause.
9. **The unlocked ``resolve_scope`` read, on the WRITE path.** A delivery that
   resolves to a place group and then blocks on that place's balance row while a
   removal DISSOLVES the place wakes to a deleted row, MINTS a new one keyed to
   the now-memberless group, and stamps its ledger row there too. Physically
   delivered bottles become unreachable by customer, driver and admin — the
   ``orphaned_place_balances`` class §7.3's dissolve exists to eliminate.
10. The same defect on the JOIN path, with NOTHING lost: a delivery blocking on
    the absorb's address-row lock INSERTs a brand-new own-scope balance row AND
    an own-scope ledger row for an address whose live pointer is now the group.
    An entirely new invisible scope, perfectly self-consistent inside itself.
11. ``reconcile_balance`` evaluates ``SUM(bottle_ledger.quantity)`` BEFORE it
    takes the balance row ``FOR UPDATE``, so a delivery committing while it
    waits is destroyed: a CLEAN place ends up drifted, and the operation is not
    idempotent.
12. The nightly sweep reads no consistent snapshot, so a ``create_place_group``
    committing between two of its statements makes it report a fully populated
    place as an ORPHANED place balance — a false alarm whose documented operator
    response is the DESTRUCTIVE reconcile route.
13. Two concurrent DELIVERED transitions of one order dedupe perfectly in the
    ledger and DOUBLE the driver session's ``bottles_delivered`` /
    ``bottles_collected_from_customers``, because that tally sits outside
    ``_create_ledger_entry``'s idempotency short-circuit and carries no key.
    Closed at the ``_claim_status_transition`` level, not at the tally — and
    the statement above is still literally true of ``order_service.py``'s own
    inline copy of the read-modify-write, which is why the claim is load-bearing.
    NOTE (2026-08-03): the OTHER tally call site,
    ``record_standalone_collection``'s, has since moved INSIDE the fence — it is
    gated on the ``created`` flag ``_create_ledger_entry_with_status`` returns —
    so the sentence above no longer describes every tally in the service.

Nine through thirteen all satisfy global conservation AND, per place,
``get_place_balance == ledger_sum``. That is why the ~1290 tests of the fast
suite are blind to them, and why every assertion in section 11 below is about
PER-SCOPE ATTRIBUTION — which scope holds the bottles, which scope the ledger
rows are stamped to, and whether any live address can still reach them.

DELIBERATE NON-DUPLICATION
--------------------------
The behavioural matrices — ``bottles_leaving`` validation, merge-review guard
codes, preview shapes, permission boundaries, split/dissolve arithmetic — are
covered exhaustively on SQLite by ``test_place_split_full_e2e.py``,
``test_place_merge_review_full_e2e.py``, ``test_place_conservation_invariants_e2e.py``
and ``test_place_money_boundary_e2e.py``. They are NOT repeated here. This file
only carries a case when Postgres can say something SQLite cannot.
"""

from __future__ import annotations

import collections
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.customer_link import (
    AddressGroup,
    CustomerLinkEvent,
    PlaceSuggestionDismissal,
)
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, UserRole, UserStatus, UserType
from tests.integration.conftest import (
    REQUIRES_PG_REASON,
    _admin_engine_for,
    _resolve_database_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.e2e]

CENT = Decimal("0.01")
API = "/api/v1"


def D(value) -> Decimal:
    """A quantity at the columns' own scale, ``NUMERIC(12,2)``."""
    dec = Decimal(str(value if value is not None else 0))
    return dec if not dec.is_finite() else dec.quantize(CENT)


# =========================================================================== #
# Harness: one migrated Postgres database for the whole module
# =========================================================================== #


@pytest.fixture(scope="module")
def place_pg_url():
    """A transient, EMPTY Postgres database; dropped on module teardown."""
    base_url = _resolve_database_url()
    if not base_url.startswith(("postgresql://", "postgresql+", "postgres://")):
        pytest.skip(REQUIRES_PG_REASON)

    from sqlalchemy.engine.url import make_url
    from sqlalchemy.exc import OperationalError

    admin_engine = _admin_engine_for(base_url)
    db_name = f"place_e2e_{uuid.uuid4().hex[:12]}"
    quoted = f'"{db_name}"'
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {quoted}"))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(f"Postgres unreachable for integration test: {exc.orig}")

    target = make_url(base_url).set(database=db_name).render_as_string(hide_password=False)
    try:
        yield target
    finally:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": db_name},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {quoted}"))
        admin_engine.dispose()


@pytest.fixture(scope="module")
def place_pg_app(place_pg_url):
    """A Flask app on that database with the REAL migration chain applied."""
    from flask_migrate import upgrade

    from business_app import create_app

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": place_pg_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SECRET_KEY": "test-secret-key-for-place-pg-e2e-32chars",
            "JWT_SECRET_KEY": "test-jwt-secret-key-for-place-pg-e2e",
            "CELERY_ALWAYS_EAGER": True,
            "WTF_CSRF_ENABLED": False,
        }
    )
    with app.app_context():
        upgrade(revision="head")
    yield app


@pytest.fixture
def papp(place_pg_app):
    """A FRESH app context (hence a fresh session) per test."""
    from business_app import db as _db

    with place_pg_app.app_context():
        try:
            yield place_pg_app
        finally:
            _db.session.rollback()
            _db.session.remove()


@pytest.fixture
def pgdb(papp):
    from business_app import db as _db

    return _db


@pytest.fixture
def pgclient(papp):
    return papp.test_client()


# =========================================================================== #
# ORACLE 1 — REACHABILITY, ASSERTED AFTER *EVERY* TEST IN THIS MODULE
# =========================================================================== #
#
# `.superpowers/sdd/2026-07-29-place-ledger-e2e/DESIGN-locking.md` §6.1:
# "REACHABILITY, asserted as SQL, as an autouse post-condition on every pg
# concurrency test — not one test. For every `bottle_balances` row a live
# address must resolve to it."
#
# Why this and not conservation: EVERY defect this file confirmed is
# CONSERVING. Bottles end up in the wrong scope, never missing, so a global Σ
# and a per-place `stored == ledger_sum` are both satisfied while the bottles
# are unreachable by customer, driver and admin alike. Reachability is the
# property that actually breaks, and asserting it once — in one dedicated test
# — only covers the interleave somebody thought to write down. As a
# post-condition on every test in the module it also covers the races nobody
# named, at the cost of two indexed SELECTs per test.
#
# THE CHECK IS A DELTA, NOT AN ABSOLUTE. This module deliberately shares one
# module-scoped database across its ~79 tests (see the module docstring), so a
# single absolute assertion would blame every later test for the first one's
# violation and bury the culprit. The baseline is taken before the test body
# and only NEW violations fail — which names the test that produced them.
#
# IT RUNS IN ITS OWN TRANSACTION (§6.1: "so its snapshot skew does not
# contaminate the result"). The rollback before the queries is not hygiene: a
# test that ended with a deliberately-aborted transaction would otherwise make
# the oracle raise `PendingRollbackError` instead of reporting reachability.
# --------------------------------------------------------------------------- #

_ORPHANED_PLACE_BALANCES_SQL = """
    SELECT b.id
      FROM bottle_balances b
     WHERE b.address_group_id IS NOT NULL
       AND NOT EXISTS (
             SELECT 1 FROM addresses a
              WHERE a.address_group_id = b.address_group_id
           )
     ORDER BY b.id
"""

_STRANDED_ADDRESS_BALANCES_SQL = """
    SELECT b.id
      FROM bottle_balances b
      JOIN addresses a ON a.id = b.address_id
     WHERE b.address_id IS NOT NULL
       AND a.address_group_id IS NOT NULL
     ORDER BY b.id
"""

# ORACLE 2 (design §6.2), evaluated on the same pass because the soak needs it
# and because a ledger row nothing resolves to is the same class of damage as a
# balance row nothing resolves to. It deliberately does NOT flag the sanctioned
# §7.1 case — a row stamped with a group the address has LEFT.
_STAMP_INCOHERENT_LEDGER_SQL = """
    SELECT l.id
      FROM bottle_ledger l
      JOIN addresses a ON a.id = l.address_id
     WHERE l.address_group_id IS NULL
       AND a.address_group_id IS NOT NULL
     ORDER BY l.id
"""


def _reachability_report(pgdb) -> dict:
    """The three unreachability buckets, read in a transaction of their own.

    Deliberately raw SQL rather than a call into
    `reconcile_customer_link_invariants`: the sweep is itself a subject of this
    file (section 11.4 pins a false positive in it under snapshot skew), and an
    oracle that shares an implementation with one of its subjects cannot fail
    when that subject is wrong. These three statements are the definition; the
    nightly task carries the same three and is controlled separately in
    `tests/unit/test_customer_link_reconciliation.py`.
    """
    pgdb.session.rollback()
    try:
        return {
            "orphaned_place_balances": [
                r[0] for r in pgdb.session.execute(text(_ORPHANED_PLACE_BALANCES_SQL)).all()
            ],
            "stranded_address_balances": [
                r[0] for r in pgdb.session.execute(text(_STRANDED_ADDRESS_BALANCES_SQL)).all()
            ],
            "stamp_incoherent_ledger_entries": [
                r[0] for r in pgdb.session.execute(text(_STAMP_INCOHERENT_LEDGER_SQL)).all()
            ],
        }
    finally:
        pgdb.session.rollback()


def _new_unreachable(before: dict, after: dict) -> dict:
    return {key: sorted(set(after[key]) - set(before[key])) for key in after}


def _reachability_guard(exempt: bool, pgdb):
    """The autouse post-condition's ENTIRE body, as a generator anything can drive.

    Extracted from the fixture for ONE reason: every known-bad control in 13.1
    carries `unreachable_by_design`, so all three deliberately BYPASS the
    fixture and call `_reachability_report` directly. That proves the three SQL
    buckets can go red — it proves nothing whatever about the fixture's own
    assertion path. Invert the marker test, drop the `assert`, or lose the
    second report call, and all three controls stay green while ORACLE 1
    protects nothing at all: the precise "certifies safety it never checked"
    failure this section exists to prevent.

    Keeping ONE body and delegating to it means 13.1a exercises the same code the
    ~79 tests run under, not a copy of it that can drift.
    """
    if exempt:
        yield
        return
    before = _reachability_report(pgdb)
    yield
    new = _new_unreachable(before, _reachability_report(pgdb))
    assert not any(new.values()), (
        "ORACLE 1 (design §6.1): this test left bottles UNREACHABLE.\n"
        f"  orphaned_place_balances     (group balance row, zero live members): {new['orphaned_place_balances']}\n"
        f"  stranded_address_balances   (address row whose address is now grouped): {new['stranded_address_balances']}\n"
        f"  stamp_incoherent_ledger_entries (ledger row unstamped at a grouped address): "
        f"{new['stamp_incoherent_ledger_entries']}\n"
        "Every one of these satisfies global conservation and per-place "
        "`stored == ledger_sum`; that is precisely why they need their own "
        "oracle. The ids are NEW since this test started, so this test made them."
    )


@pytest.fixture(autouse=True)
def reachability_oracle(request, pgdb):
    """Design §6.1's autouse post-condition. Applies to EVERY test in this file.

    OPT-OUT, and why one exists: a handful of tests in this module manufacture
    an unreachable row ON PURPOSE — the known-bad controls that prove these very
    buckets can go red, the 13.1a tests that prove THIS fixture can go red, and
    the constraint tests that attempt an illegal row. (Deliberately not a count:
    a number in a docstring goes stale the first time one is added.)
    They carry `@pytest.mark.unreachable_by_design`, which is narrow, greppable
    and forces a deliberate act to silence the oracle. It is NOT a general
    escape hatch: a test that trips this without the marker has found something.

    The body lives in `_reachability_guard` so that 13.1a can drive it and prove
    THIS assertion fires; the marker decision is the only thing that stays here.
    """
    yield from _reachability_guard(
        request.node.get_closest_marker("unreachable_by_design") is not None, pgdb
    )


@pytest.fixture
def raw(place_pg_url):
    """Factory for independent psycopg2 connections (observers / probers).

    Every connection handed out is closed on teardown, which matters: the
    ephemeral database cannot be dropped while a backend holds it.
    """
    made = []

    def _connect(*, autocommit: bool = False, lock_timeout_ms: int | None = None):
        conn = psycopg2.connect(place_pg_url)
        conn.autocommit = autocommit
        made.append(conn)
        if lock_timeout_ms is not None:
            with conn.cursor() as cur:
                cur.execute(f"SET lock_timeout = '{lock_timeout_ms}ms'")
        return conn

    try:
        yield _connect
    finally:
        for conn in made:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - teardown best effort
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Catalog introspection — expected constraint names are DERIVED, never typed
# --------------------------------------------------------------------------- #


def _fk_name(pgdb, table: str, column: str, referenced: str) -> str:
    """The real name of the FK on ``table.column`` -> ``referenced``.

    Read from ``pg_constraint`` so the assertions below name whatever the
    migration chain actually created instead of a hand-copied literal that
    could silently stop matching.
    """
    row = pgdb.session.execute(
        text(
            """
            SELECT c.conname
              FROM pg_constraint c
              JOIN pg_class t   ON t.oid = c.conrelid
              JOIN pg_class rt  ON rt.oid = c.confrelid
              JOIN pg_attribute a
                   ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
             WHERE c.contype = 'f'
               AND t.relname = :table
               AND rt.relname = :referenced
               AND a.attname = :column
            """
        ),
        {"table": table, "column": column, "referenced": referenced},
    ).first()
    assert row is not None, (
        f"no FOREIGN KEY found on {table}.{column} -> {referenced}. "
        "If a migration dropped it, every 'the FK protects this' claim in the "
        "place lifecycle is void."
    )
    return row[0]


def _constraint_exists(pgdb, name: str) -> bool:
    return (
        pgdb.session.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
        ).first()
        is not None
    )


def _fk_delete_action(pgdb, name: str) -> str:
    return pgdb.session.execute(
        text("SELECT confdeltype FROM pg_constraint WHERE conname = :n"), {"n": name}
    ).scalar()


def _integrity_message(exc: BaseException) -> str:
    return str(getattr(exc, "orig", exc))


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _error_code(payload) -> str | None:
    """The machine-readable ``error_code`` as a VALUE, wherever the envelope put it.

    ``"CODE" in str(payload)`` — the obvious spelling — also passes when the
    code only appears inside the human-readable prose, which is NOT the
    contract the admin UI branches on. Reading the field means these assertions
    fail the day a route stops emitting it, which is exactly when the UI would
    silently lose the ability to say "remove it from the place first".
    """
    if not isinstance(payload, dict):
        return None
    for container in (payload, payload.get("data"), payload.get("errors")):
        if isinstance(container, dict) and container.get("error_code"):
            return container["error_code"]
    return None


# --------------------------------------------------------------------------- #
# Builders — real rows, real service write paths
# --------------------------------------------------------------------------- #

_SEQ = [0]


def _uniq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _user(pgdb, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL) -> User:
    n = _uniq()
    user = User(
        email=f"place.pg.{n}.{uuid.uuid4().hex[:6]}@example.com",
        phone=f"+9989{700000000 + n}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Place",
        last_name=f"User{n}",
        user_type=user_type,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pgdb.session.add(user)
    pgdb.session.commit()
    return user


def _admin(pgdb) -> User:
    return _user(pgdb, role=UserRole.ADMIN, user_type=UserType.STAFF)


def _addr(pgdb, owner: User, *, title=None, lat=41.3111, lng=69.2797, default=False) -> UserAddress:
    n = _uniq()
    address = UserAddress(
        user_id=owner.id,
        title=title or f"Door {n}",
        full_address=f"{n} Test Street, Tashkent",
        street_address=f"{n} Test Street",
        city="Tashkent",
        latitude=lat,
        longitude=lng,
        is_default=default,
    )
    pgdb.session.add(address)
    pgdb.session.commit()
    return address


def _order(pgdb, owner: User, address: UserAddress, status=OrderStatus.DELIVERED) -> Order:
    n = _uniq()
    order = Order(
        user_id=owner.id,
        order_number=f"ORD-PGPLACE-{n:06d}",
        status=status,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    pgdb.session.add(order)
    pgdb.session.commit()
    return order


def _deliver(pgdb, owner: User, address: UserAddress, qty, *, actor=None) -> BottleLedger:
    order = _order(pgdb, owner, address)
    entry = BottleTrackingService().record_bottles_delivered(
        order.id, owner.id, address.id, D(qty), actor_user_id=actor.id if actor else None
    )
    pgdb.session.commit()
    return entry


def _give_back(pgdb, owner: User, address: UserAddress, qty, *, actor=None) -> BottleLedger:
    order = _order(pgdb, owner, address)
    entry = BottleTrackingService().record_bottles_returned(
        owner.id,
        address.id,
        D(qty),
        order_id=order.id,
        delivery_id=None,
        actor_user_id=actor.id if actor else None,
    )
    pgdb.session.commit()
    return entry


def _adjust(pgdb, admin: User, address: UserAddress, delta, *, notes="admin correction", owner=None):
    entry = BottleTrackingService().admin_adjust_balance(
        user_id=owner.id if owner else None,
        address_id=address.id,
        adjustment=D(delta),
        actor_user_id=admin.id,
        notes=notes,
    )
    pgdb.session.commit()
    return entry


def _drop_ledger_row(pgdb, entry_id: int) -> None:
    """Erase ONE ledger row, leaving the stored balance standing.

    Not fiction: ``OrderDeletionService`` FK-traverses from ``orders`` into
    ``bottle_ledger`` and deletes exactly this way while ``bottle_balances``
    stands (pinned further down). It is how the dev address-24 shape — stored
    20.00 with zero ledger rows — comes about in production.
    """
    pgdb.session.execute(text("DELETE FROM bottle_ledger WHERE id = :i"), {"i": entry_id})
    pgdb.session.commit()
    pgdb.session.expire_all()


def _group(pgdb, admin: User, addresses, *, reason="same office", **kwargs) -> AddressGroup:
    group = CustomerLinkService().create_place_group(
        [a.id for a in addresses], acting_admin_id=admin.id, reason=reason, **kwargs
    )
    pgdb.session.commit()
    return group


def _stored(pgdb, scope: BottleScope) -> Decimal:
    total = (
        pgdb.session.query(func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00")))
        .filter(*scope.balance_filter())
        .scalar()
    )
    return D(total)


def _ledger_sum(pgdb, scope: BottleScope) -> Decimal:
    total = (
        pgdb.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
        .filter(*scope.ledger_filter())
        .scalar()
    )
    return D(total)


def _pair(pgdb, scope: BottleScope) -> tuple[Decimal, Decimal]:
    """(stored balance, ledger sum) — asserted TOGETHER, never one side."""
    pgdb.session.expire_all()
    return _stored(pgdb, scope), _ledger_sum(pgdb, scope)


def _sum_over(pgdb, scopes) -> Decimal:
    """Σ stored balance over an EXPLICIT list of scopes (never a global Σ)."""
    return D(sum((_stored(pgdb, s) for s in scopes), Decimal("0.00")))


def _soak_scopes(pgdb, pool) -> list:
    """Every scope the soak's addresses can have reached — own AND place.

    Not a global sweep: this module shares one database across ~80 tests, so a
    Σ over `bottle_balances` would fold in everything every other test wrote.
    The set is derived from the DATA rather than from the soak's bookkeeping,
    so a place the soak created and forgot to record is still counted:

      * each pool address's own scope;
      * every group a pool address currently points at;
      * every group a pool address's ledger rows are stamped to — which keeps
        a place the address has since LEFT (the sanctioned §7.1 residue) inside
        the Σ instead of silently dropping its bottles out of the conservation
        check.

    The two arms are disjoint by construction (`balance_filter`'s ungrouped arm
    carries `address_group_id IS NULL`), so nothing is double counted.
    """
    address_ids = [address_id for address_id, _ in pool]
    group_ids = set()
    for sql in (
        "SELECT DISTINCT address_group_id FROM addresses "
        "  WHERE id = ANY(:ids) AND address_group_id IS NOT NULL",
        "SELECT DISTINCT address_group_id FROM bottle_ledger "
        "  WHERE address_id = ANY(:ids) AND address_group_id IS NOT NULL",
        "SELECT DISTINCT address_group_id FROM bottle_balances "
        "  WHERE address_id = ANY(:ids) AND address_group_id IS NOT NULL",
    ):
        group_ids.update(
            r[0] for r in pgdb.session.execute(text(sql), {"ids": address_ids}).all()
        )
    return [BottleScope.for_address(a) for a in address_ids] + [
        BottleScope.for_group(g) for g in sorted(group_ids)
    ]


def _soak_sigma(pgdb, pool) -> Decimal:
    """Σ stored balance over every scope the soak's pool can reach."""
    pgdb.session.expire_all()
    return _sum_over(pgdb, _soak_scopes(pgdb, pool))


def _raw_balance_rows(pgdb, *, group_id=None, address_id=None) -> list[tuple]:
    """(id, address_group_id, address_id, balance) straight from SQL."""
    if group_id is not None:
        sql = "SELECT id, address_group_id, address_id, balance FROM bottle_balances WHERE address_group_id = :k"
        key = {"k": group_id}
    else:
        sql = "SELECT id, address_group_id, address_id, balance FROM bottle_balances WHERE address_id = :k"
        key = {"k": address_id}
    return [tuple(r) for r in pgdb.session.execute(text(sql), key).all()]


def _members(pgdb, group_id: int) -> list[int]:
    return [
        r[0]
        for r in pgdb.session.execute(
            text("SELECT id FROM addresses WHERE address_group_id = :g ORDER BY id"),
            {"g": group_id},
        ).all()
    ]


def _headers(papp, user: User) -> dict:
    """A real JWT carrying the ``role`` claim ``TokenService`` puts there.

    ``manager_or_higher_required`` (the admin address-delete route) reads
    ``claims['role']`` and 403s "Invalid user role" without it, so a token
    minted with identity alone would make that route unreachable and the fence
    test would pass for the wrong reason.
    """
    from flask_jwt_extended import create_access_token

    role = user.role.value if hasattr(user.role, "value") else user.role
    token = create_access_token(identity=str(user.id), additional_claims={"role": role})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@contextmanager
def _lock_timeout(pgdb, ms: int = 1200):
    """``SET LOCAL lock_timeout`` for the session's current transaction.

    LOCAL, so it dies with the transaction and cannot follow the pooled
    connection into the next test.
    """
    pgdb.session.execute(text(f"SET LOCAL lock_timeout = '{ms}ms'"))
    try:
        yield
    finally:
        pgdb.session.rollback()


def _assert_lock_timeout(exc_info) -> None:
    """55P03 = lock_not_available, which is what both lock_timeout and NOWAIT raise."""
    assert _sqlstate(exc_info.value) == "55P03", (
        "expected Postgres 55P03 (lock_not_available) — i.e. the call genuinely "
        f"waited for a row lock — got {_sqlstate(exc_info.value)}: "
        f"{_integrity_message(exc_info.value)[:300]}"
    )


def _hold_row_for_update(conn, *, group_id=None, address_id=None) -> None:
    """Take (and HOLD, until the caller commits/rolls back) one balance row."""
    with conn.cursor() as cur:
        if group_id is not None:
            cur.execute(
                "SELECT id FROM bottle_balances WHERE address_group_id = %s FOR UPDATE",
                (group_id,),
            )
        else:
            cur.execute(
                "SELECT id FROM bottle_balances WHERE address_id = %s "
                "  AND address_group_id IS NULL FOR UPDATE",
                (address_id,),
            )
        rows = cur.fetchall()
    assert rows, (
        "the observer found no row to hold — the setup must create the balance "
        "row through a real write path before the lock can be contended"
    )


def _hold_address_row_for_update(conn, address_id: int) -> None:
    """Hold the ``addresses`` row itself — RUNG 1 of the ladder."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM addresses WHERE id = %s FOR UPDATE", (address_id,))
        assert cur.fetchone() is not None


def _hold_group_membership_row_for_update(conn, group_id: int) -> None:
    """Hold the ``address_groups`` row — RUNG 0, the MEMBERSHIP MUTEX.

    Deliberately distinct from ``_hold_row_for_update(group_id=...)``, which
    holds the place's ``bottle_balances`` row (rung 2). Confusing the two is
    confusing the mapping with the money.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM address_groups WHERE id = %s FOR UPDATE", (group_id,))
        assert cur.fetchone() is not None


def _try_lock_address_row_nowait(conn, address_id: int) -> bool:
    """True if the ``addresses`` row could be locked RIGHT NOW."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM addresses WHERE id = %s FOR UPDATE NOWAIT", (address_id,)
            )
            assert cur.fetchall()
        return True
    except psycopg2.errors.LockNotAvailable:
        return False
    finally:
        conn.rollback()


def _address_row_is_held(prober, address_id: int, *, samples: int = 3, gap: float = 0.15) -> bool:
    """Rung-1 twin of ``_group_row_is_held``, sampled for the same reason."""
    readings = []
    for index in range(samples):
        if index:
            time.sleep(gap)
        readings.append(not _try_lock_address_row_nowait(prober, address_id))
    assert len(set(readings)) == 1, (
        f"the addresses row {address_id} flickered between held and free across "
        f"{samples} readings: {readings} — somebody else is on the row and the "
        "probe cannot answer the ordering question"
    )
    return readings[0]


@contextmanager
def _barrier_at(cls, name: str, barrier: threading.Barrier):
    """Make a service staticmethod wait on ``barrier`` before running.

    Forces a specific interleaving of two real transactions without sleeping:
    the barrier sits at a known point inside the production call, so both
    parties are provably past everything before it and provably before
    everything after it. The original attribute is restored on exit.

    ONE-SIDED, and only sound where the OUTCOME does not depend on which side
    finishes first (see the split-cap test, where both orderings land on the
    same two figures). Where the outcome DOES depend on it — anything that
    reads state another transaction is about to commit — use
    ``_rendezvous_at``: an entry barrier alone lets the first thread run the
    whole method and COMMIT before the second is even scheduled.
    """
    original = getattr(cls, name)

    def wrapper(*args, **kwargs):
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:  # pragma: no cover - abort path
            pass
        return original(*args, **kwargs)

    setattr(cls, name, staticmethod(wrapper))
    try:
        yield
    finally:
        setattr(cls, name, staticmethod(original))


@contextmanager
def _rendezvous_at(cls, name: str, *, parties: int = 2, timeout: float = 30.0):
    """Force ``parties`` concurrent calls of ``cls.name`` to OVERLAP COMPLETELY.

    A single barrier at the TOP of the call is not enough, and this was
    measured rather than reasoned: with one barrier,
    ``test_two_concurrent_removals_from_a_THREE_member_place_still_DISSOLVE``
    passed on one full-file run and failed on the next identical one. The
    barrier proves both threads REACHED ``_dissolve_if_last_member``; it does
    nothing to stop the first one released from running the membership count,
    returning, and COMMITTING before the second is scheduled at all — after
    which the second counts against a COMMITTED removal, does dissolve, and the
    strict xfail flips to a suite-breaking XPASS.

    Two barriers close the window: nobody enters the wrapped call until
    everybody has arrived, and nobody RETURNS from it — hence nobody commits —
    until everybody has finished it. A call that RAISES aborts the exit
    barrier, so a genuine 40P01 kill unblocks its partner immediately instead
    of stalling for ``timeout``.
    """
    original = getattr(cls, name)
    entered = threading.Barrier(parties, timeout=timeout)
    completed = threading.Barrier(parties, timeout=timeout)

    def wrapper(*args, **kwargs):
        try:
            entered.wait()
        except threading.BrokenBarrierError:  # pragma: no cover - abort path
            pass
        try:
            result = original(*args, **kwargs)
        except BaseException:
            # Let the partner out of the exit barrier at once: a deadlock
            # victim is never coming.
            completed.abort()
            raise
        try:
            completed.wait()
        except threading.BrokenBarrierError:  # pragma: no cover - abort path
            pass
        return result

    setattr(cls, name, staticmethod(wrapper))
    try:
        yield
    finally:
        setattr(cls, name, staticmethod(original))


def _wait_until_a_backend_blocks_on_a_lock(
    probe_conn, *, expected: int = 1, deadline_s: float = 20.0
) -> bool:
    """Poll ``pg_stat_activity`` until some backend is waiting on a lock.

    Used only by the two lock-ORDER proofs, which need to know that the thread
    under test has actually reached its blocking acquisition. Returns False on
    timeout rather than raising, so the caller's own assertions decide the
    verdict instead of a scheduling hiccup.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        with probe_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                " WHERE datname = current_database() "
                "   AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            )
            if cur.fetchone()[0] >= expected:
                return True
        time.sleep(0.05)
    return False


def _try_lock_group_row_nowait(conn, group_id: int) -> bool:
    """True if the group's balance row could be locked RIGHT NOW.

    ``FOR UPDATE NOWAIT`` raises 55P03 instead of waiting, so this is a
    non-blocking question: "is somebody else already holding this row?"
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bottle_balances WHERE address_group_id = %s FOR UPDATE NOWAIT",
                (group_id,),
            )
            rows = cur.fetchall()
        # Without this, "the row was FREE" and "there is no row" are the same
        # answer, and every test that EXPECTS True would pass vacuously the day
        # the place stops getting a balance row at all.
        assert rows, (
            f"the prober found no bottle_balances row for place group {group_id}; "
            "'free' is meaningless until a real write path has created it"
        )
        return True
    except psycopg2.errors.LockNotAvailable:
        return False
    finally:
        conn.rollback()


def _backend_activity(conn) -> list:
    """A compact snapshot of every backend on this database, for diagnostics."""
    diag = psycopg2.connect(conn.dsn)
    diag.autocommit = True
    try:
        with diag.cursor() as cur:
            cur.execute(
                "SELECT pid, state, wait_event_type, wait_event, left(query, 90) "
                "  FROM pg_stat_activity "
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            return cur.fetchall()
    finally:
        diag.close()


def _group_row_is_held(prober, group_id: int, *, samples: int = 3, gap: float = 0.15) -> bool:
    """Is the place's balance row held RIGHT NOW — asked repeatedly, not once.

    A single ``FOR UPDATE NOWAIT`` is a one-shot reading, and a one-shot reading
    is how a lock-ORDER proof goes quietly false-negative: measured, the ORDER
    proof below passed once against a DELIBERATELY REVERSED
    ``_absorb_joiners_into_group`` (group row taken AFTER the joiners instead of
    before), because the probe came back "held" when the blocked call could not
    possibly have held it. Sampling several times turns that from a silent pass
    into a LOUD failure: the transaction under test is blocked and holds its
    locks to the end of the transaction, so a genuine reading is the same every
    time, and a flicker means somebody else is on the row and the probe cannot
    answer the ordering question at all.
    """
    readings = []
    for index in range(samples):
        if index:
            time.sleep(gap)
        readings.append(not _try_lock_group_row_nowait(prober, group_id))
    assert len(set(readings)) == 1, (
        f"the balance row of place group {group_id} flickered between held and "
        f"free across {samples} readings ({readings}) — something other than the "
        "blocked call under test is touching it, so this probe cannot answer the "
        f"lock-ORDER question. Backends: {_backend_activity(prober)}"
    )
    return readings[0]


def _in_app(app, fn, sink: dict, key: str) -> threading.Thread:
    """Run ``fn`` in its own thread, its own app context, its own session."""
    from business_app import db as _db

    def target():
        with app.app_context():
            try:
                sink[key] = fn()
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                sink[key] = exc
            finally:
                try:
                    _db.session.rollback()
                except Exception:  # noqa: BLE001
                    pass
                _db.session.remove()

    thread = threading.Thread(target=target, name=key, daemon=True)
    thread.start()
    return thread


def _reraise(sink: dict, key: str):
    value = sink.get(key)
    if isinstance(value, BaseException):
        raise value
    return value


# =========================================================================== #
# 1. DDL TRUTH — the constraints the migration chain actually created
# =========================================================================== #


class TestScopeKeyConstraintIsRealInTheMigratedSchema:
    """``ck_bottle_balance_scope``: exactly one scope key, enforced by the DB.

    The CHECK is honoured on SQLite too, so these are not about the predicate —
    they are about it being present in the MIGRATED schema (the models' version
    is what ``create_all`` gives the fast suite; production ran migrations) and
    about the ``num_nonnulls()`` trap: a Postgres-only rewrite of this CHECK
    would break ``db.create_all()`` and could be "fixed" by dropping it here,
    leaving the whole SQLite suite green while production accepts dual-key rows
    that every ``balance_filter()`` then double-counts.
    """

    def test_the_three_scope_constraints_exist_in_the_migrated_schema(self, papp, pgdb):
        for name in (
            "ck_bottle_balance_scope",
            "uq_bottle_balance_group",
            "uq_bottle_balance_addr",
        ):
            assert _constraint_exists(pgdb, name), f"{name} is missing from the migrated schema"

    def test_real_service_writes_produce_valid_rows_for_BOTH_scope_shapes(self, papp, pgdb):
        """The positive half that makes the two negatives meaningful.

        ``get_or_create_balance`` builds its INSERT from
        ``BottleScope.balance_defaults()``. A scope that ever emitted both keys
        (or neither) would be rejected by the real CHECK right here.
        """
        admin = _admin(pgdb)
        solo_owner, b_owner, c_owner = _user(pgdb), _user(pgdb), _user(pgdb)
        solo = _addr(pgdb, solo_owner)
        b, c = _addr(pgdb, b_owner), _addr(pgdb, c_owner)
        group = _group(pgdb, admin, [b, c])

        _deliver(pgdb, solo_owner, solo, 3)
        _deliver(pgdb, b_owner, b, 4)

        assert _raw_balance_rows(pgdb, address_id=solo.id) == [
            (
                _raw_balance_rows(pgdb, address_id=solo.id)[0][0],
                None,
                solo.id,
                Decimal("3.00"),
            )
        ]
        group_rows = _raw_balance_rows(pgdb, group_id=group.id)
        assert len(group_rows) == 1
        assert (group_rows[0][1], group_rows[0][2], group_rows[0][3]) == (
            group.id,
            None,
            Decimal("4.00"),
        )
        # And the member address never gained a row of its own.
        assert _raw_balance_rows(pgdb, address_id=b.id) == []

    def test_a_row_carrying_BOTH_scope_keys_is_refused_by_the_CHECK(self, papp, pgdb):
        """The row itself is the subject, so it is built by hand on purpose."""
        admin = _admin(pgdb)
        a_owner, b_owner = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, a_owner), _addr(pgdb, b_owner)
        group = _group(pgdb, admin, [a, b])
        before = pgdb.session.execute(text("SELECT count(*) FROM bottle_balances")).scalar()

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text(
                    "INSERT INTO bottle_balances (address_group_id, address_id, balance, created_at) "
                    "VALUES (:g, :a, 5.00, NOW())"
                ),
                {"g": group.id, "a": a.id},
            )
            pgdb.session.commit()
        assert "ck_bottle_balance_scope" in _integrity_message(exc_info.value)
        pgdb.session.rollback()
        assert pgdb.session.execute(text("SELECT count(*) FROM bottle_balances")).scalar() == before

    def test_a_row_carrying_NEITHER_scope_key_is_refused_by_the_CHECK(self, papp, pgdb):
        """A keyless row is invisible to every ``balance_filter()`` yet counted
        by the dashboard's unfiltered aggregates — a silent phantom-bottle
        source. Only the real CHECK stops it."""
        before = pgdb.session.execute(text("SELECT count(*) FROM bottle_balances")).scalar()
        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text(
                    "INSERT INTO bottle_balances (address_group_id, address_id, balance, created_at) "
                    "VALUES (NULL, NULL, 5.00, NOW())"
                )
            )
            pgdb.session.commit()
        assert "ck_bottle_balance_scope" in _integrity_message(exc_info.value)
        pgdb.session.rollback()
        assert pgdb.session.execute(text("SELECT count(*) FROM bottle_balances")).scalar() == before

    def test_the_absorb_CANNOT_be_simplified_into_a_repoint_UPDATE(self, papp, pgdb):
        """``absorb_address_into_group``'s delete-and-credit dance looks
        gratuitous; the obvious "simplification" is to UPDATE the address row's
        ``address_group_id``. The database forbids it. This pins the reason so
        it survives the next refactor — and then runs the REAL join to show the
        row is deleted and its figure credited onto the place instead."""
        admin = _admin(pgdb)
        a_owner, b_owner = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, a_owner), _addr(pgdb, b_owner)
        _deliver(pgdb, a_owner, a, 6)

        # A pre-existing group to repoint onto (any real group will do).
        x_owner, y_owner = _user(pgdb), _user(pgdb)
        other = _group(pgdb, admin, [_addr(pgdb, x_owner), _addr(pgdb, y_owner)])

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text("UPDATE bottle_balances SET address_group_id = :g WHERE address_id = :a"),
                {"g": other.id, "a": a.id},
            )
            pgdb.session.commit()
        assert "ck_bottle_balance_scope" in _integrity_message(exc_info.value)
        pgdb.session.rollback()

        group = _group(pgdb, admin, [a, b])
        assert _raw_balance_rows(pgdb, address_id=a.id) == [], (
            "the join must DELETE the joiner's own row, not repoint it"
        )
        stored, ledger = _pair(pgdb, BottleScope.for_group(group.id))
        assert (stored, ledger) == (Decimal("6.00"), Decimal("6.00"))

    def test_a_second_balance_row_for_one_PLACE_GROUP_is_refused(self, papp, pgdb):
        """``get_or_create_balance``'s ``ON CONFLICT DO NOTHING`` names
        ``address_group_id`` as the arbiter. Drop this unique and that clause
        raises "no unique or exclusion constraint matching" at runtime in
        production, and every place read silently sums two rows."""
        admin = _admin(pgdb)
        a_owner, b_owner = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, a_owner), _addr(pgdb, b_owner)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, a_owner, a, 2)

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text(
                    "INSERT INTO bottle_balances (address_group_id, address_id, balance, created_at) "
                    "VALUES (:g, NULL, 1.00, NOW())"
                ),
                {"g": group.id},
            )
            pgdb.session.commit()
        assert "uq_bottle_balance_group" in _integrity_message(exc_info.value)
        pgdb.session.rollback()
        assert len(_raw_balance_rows(pgdb, group_id=group.id)) == 1

    def test_a_second_balance_row_for_one_ADDRESS_is_refused(self, papp, pgdb):
        """Same arbiter dependency for the address arm — and
        ``absorb_address_into_group`` reads ``own_row`` with ``.first()`` on the
        documented promise of "at most one row"; a second would be silently
        ignored and its bottles destroyed on the join."""
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        _deliver(pgdb, owner, a, 2)

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text(
                    "INSERT INTO bottle_balances (address_group_id, address_id, balance, created_at) "
                    "VALUES (NULL, :a, 1.00, NOW())"
                ),
                {"a": a.id},
            )
            pgdb.session.commit()
        assert "uq_bottle_balance_addr" in _integrity_message(exc_info.value)
        pgdb.session.rollback()
        assert len(_raw_balance_rows(pgdb, address_id=a.id)) == 1

    def test_many_places_coexist_because_UNIQUE_treats_NULLS_as_DISTINCT(self, papp, pgdb):
        """The entire two-column scope design rests on multi-NULL uniqueness.

        Postgres 15 supports ``UNIQUE NULLS NOT DISTINCT``; a well-meaning
        "tighten the constraint" migration would make the SECOND place in the
        system unable to get a balance row at all, and no SQLite test would
        notice. Both arms are asserted: three group-scoped rows all with
        ``address_id IS NULL``, and three address-scoped rows all with
        ``address_group_id IS NULL``.
        """
        admin = _admin(pgdb)
        group_ids, solo_ids = [], []
        for _ in range(3):
            o1, o2 = _user(pgdb), _user(pgdb)
            a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
            group = _group(pgdb, admin, [a1, a2])
            _deliver(pgdb, o1, a1, 1)
            group_ids.append(group.id)

            solo_owner = _user(pgdb)
            solo = _addr(pgdb, solo_owner)
            _deliver(pgdb, solo_owner, solo, 1)
            solo_ids.append(solo.id)

        rows = pgdb.session.execute(
            text(
                "SELECT address_group_id, address_id FROM bottle_balances "
                "WHERE address_group_id = ANY(:g)"
            ),
            {"g": group_ids},
        ).all()
        assert len(rows) == 3
        assert all(r[1] is None for r in rows), "every place row must carry a NULL address_id"

        rows = pgdb.session.execute(
            text(
                "SELECT address_group_id, address_id FROM bottle_balances "
                "WHERE address_id = ANY(:a)"
            ),
            {"a": solo_ids},
        ).all()
        assert len(rows) == 3
        assert all(r[0] is None for r in rows), "every solo row must carry a NULL address_group_id"

    def test_get_or_create_balance_called_TWICE_in_one_transaction_yields_ONE_row(
        self, papp, pgdb
    ):
        """``pg_insert(...).on_conflict_do_nothing`` is a Postgres-dialect
        construct compiled against SQLite in the fast suite; its real arbiter-index
        semantics are only exercised here."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
        group = _group(pgdb, admin, [a1, a2])
        assert _raw_balance_rows(pgdb, group_id=group.id) == []

        scope = BottleScope.for_group(group.id)
        # An EXPLICIT scope is asserted against the ladder's rung-1 registry, not
        # self-served — the function cannot resolve "a specific place the address
        # may not currently map to". Real callers hold the lock by the time they
        # get here; a test poking the funnel directly must too.
        BottleTrackingService.resolve_scope_for_write(a1.id)
        BottleTrackingService.resolve_scope_for_write(a2.id)
        first = BottleTrackingService.get_or_create_balance(a1.id, scope=scope)
        second = BottleTrackingService.get_or_create_balance(a2.id, scope=scope)
        assert first is not None and second is not None
        assert first.id == second.id
        pgdb.session.commit()

        rows = _raw_balance_rows(pgdb, group_id=group.id)
        assert len(rows) == 1
        assert rows[0][3] == Decimal("0.00")

    def test_during_a_split_the_leavers_row_and_the_places_row_COEXIST(self, papp, pgdb):
        """``_split_bottles_out_of_place`` writes the ``:in`` half with an
        EXPLICIT address scope while the address is STILL grouped. If
        ``get_or_create_balance`` ever resolved the scope itself instead of
        honouring the explicit one, the +3 would land back on the group row and
        the split would be a no-op that still cleared membership — three bottles
        lost, invisible on SQLite. A 3-member group keeps the place alive so
        both rows are observable after commit.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 8)

        result = CustomerLinkService().remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="split", bottles_leaving=3
        )
        pgdb.session.commit()
        assert result["dissolved"] is False

        group_rows = _raw_balance_rows(pgdb, group_id=group.id)
        addr_rows = _raw_balance_rows(pgdb, address_id=a.id)
        assert len(group_rows) == 1 and group_rows[0][3] == Decimal("5.00")
        assert len(addr_rows) == 1 and addr_rows[0][3] == Decimal("3.00")

        # The `:in` half was written on the ADDRESS scope even though the
        # address was still a member when it was written.
        in_half = (
            BottleLedger.query.filter(
                BottleLedger.idempotency_key.like(f"place_leave:{group.id}:%:{a.id}:in")
            )
            .one()
        )
        assert in_half.address_group_id is None
        assert D(in_half.quantity) == Decimal("3.00")
        # And the pair still balances across the two scopes.
        assert _sum_over(
            pgdb, [BottleScope.for_group(group.id), BottleScope.for_address(a.id)]
        ) == Decimal("8.00")


# =========================================================================== #
# 2. FOREIGN KEY INTEGRITY — which constraint blocks which delete
# =========================================================================== #


class TestMemberlessGroupIsPinnedByRealForeignKeys:
    """§7.3 keeps the memberless ``AddressGroup`` row. That is not taste."""

    def _dissolved_group_with_departed_history(self, pgdb, admin):
        """Place G whose scope keeps a DEPARTED member's entries after dissolve.

        THREE members are required, not two: removing one of two members already
        leaves exactly one, which dissolves immediately — so a two-member group
        can never produce "a member departed BEFORE the dissolve". A, B, C join;
        B leaves while C is still there (no dissolve, B's entries STAY stamped
        with the group); then C leaves, which leaves A alone and dissolves the
        place onto A, re-stamping only A's OWN entries out.
        """
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 6)
        _deliver(pgdb, ob, b, 5)

        service = CustomerLinkService()
        # B departs while C is still a member: no dissolve, and B's entries STAY
        # stamped with the group forever (spec §7.1).
        assert (
            service.remove_address_from_group(b.id, acting_admin_id=admin.id, reason="left")
        )["dissolved"] is False
        pgdb.session.commit()
        # C departs: A is now the last member, so the place dissolves onto A.
        result = service.remove_address_from_group(c.id, acting_admin_id=admin.id, reason="left too")
        pgdb.session.commit()
        assert result["dissolved"] is True
        assert _members(pgdb, group.id) == []
        return group, a, b, oa, ob

    def test_a_memberless_group_CANNOT_be_deleted_because_the_ledger_FK_holds_it(
        self, papp, pgdb
    ):
        """The spec's literal "the group is then deleted" is not implementable.

        Someone implementing that literal reading either crashes in production
        or — far worse — NULLs ``bottle_ledger.address_group_id`` first, which
        drops the whole place's history into a departed address's own scope and
        MINTS bottles onto an address that left with nothing.
        """
        admin = _admin(pgdb)
        group, a, b, _oa, _ob = self._dissolved_group_with_departed_history(pgdb, admin)

        expected = _fk_name(pgdb, "bottle_ledger", "address_group_id", "address_groups")
        stamped_before = pgdb.session.execute(
            text("SELECT count(*) FROM bottle_ledger WHERE address_group_id = :g"),
            {"g": group.id},
        ).scalar()
        assert stamped_before > 0, "setup must leave the departed member's rows stamped"

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text("DELETE FROM address_groups WHERE id = :g"), {"g": group.id}
            )
            pgdb.session.commit()
        assert expected in _integrity_message(exc_info.value)
        pgdb.session.rollback()

        assert AddressGroup.query.get(group.id) is not None
        assert (
            pgdb.session.execute(
                text("SELECT count(*) FROM bottle_ledger WHERE address_group_id = :g"),
                {"g": group.id},
            ).scalar()
            == stamped_before
        )

    def test_the_dissolve_DID_delete_the_groups_balance_row_so_the_LEDGER_FK_is_the_blocker(
        self, papp, pgdb
    ):
        """Which constraint blocks the delete is what distinguishes "the balance
        row was correctly cleaned up" from "a row was left behind".

        If the dissolve ever stopped deleting the group's balance row, the
        nightly ``orphaned_place_balances`` sweep fires and ``reconcile_balance``
        can never reach the row to fix it.
        """
        admin = _admin(pgdb)
        group, _a, _b, _oa, _ob = self._dissolved_group_with_departed_history(pgdb, admin)

        assert _raw_balance_rows(pgdb, group_id=group.id) == [], (
            "release_group_history_to_address must delete the group's balance row"
        )
        ledger_fk = _fk_name(pgdb, "bottle_ledger", "address_group_id", "address_groups")
        balances_fk = _fk_name(pgdb, "bottle_balances", "address_group_id", "address_groups")

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text("DELETE FROM address_groups WHERE id = :g"), {"g": group.id}
            )
            pgdb.session.commit()
        message = _integrity_message(exc_info.value)
        assert ledger_fk in message
        assert balances_fk not in message
        pgdb.session.rollback()

    def test_bottle_fines_address_group_id_ALSO_pins_a_memberless_group(self, papp, pgdb):
        """Everyone reasons about the ledger FK; the FINES FK is the second,
        unnamed one. A "clean up memberless groups" script that deleted the
        ledger rows first would still fail here — and if it deleted the fines
        too it would erase money owed. Nobody has pinned this reference.

        UPDATED: the fine now has to be issued on a member that LEAVES, and the
        leftover ledger rows have to be swept away by hand. Both changes are the
        cleanup script this test is about, made literal.

        The fine used to be issued on the SURVIVOR, because the survivor's ledger
        rows are re-stamped OUT of the group on dissolve and nothing then
        shadowed the fines FK. The dissolve now re-stamps the survivor's
        `bottle_fines` alongside its `bottle_ledger` (that is what makes the
        FREEZE policy coherent), so a survivor's fine leaves with it. A DEPARTED
        member's frozen references are deliberately NOT re-stamped — §7.1 keeps
        their history anchored to the group they left — so that is where a fine
        can still outlive its place, and it is exactly the shape the missing
        `stranded_fine_scopes` sweep bucket is about.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 6)
        service = BottleTrackingService()
        fine = service.issue_fine(
            user_id=None,
            address_id=b.id,
            quantity=Decimal("2"),
            fine_amount=Decimal("50000"),
            actor_user_id=admin.id,
            notes="lost crates",
        )
        pgdb.session.commit()
        assert fine.address_group_id == group.id

        links = CustomerLinkService()
        links.remove_address_from_group(b.id, acting_admin_id=admin.id, reason="left")
        pgdb.session.commit()
        links.remove_address_from_group(c.id, acting_admin_id=admin.id, reason="left too")
        pgdb.session.commit()
        assert _members(pgdb, group.id) == []
        assert BottleFine.query.get(fine.id).address_group_id == group.id, (
            "a DEPARTED member's frozen fine scope must NOT follow the dissolve"
        )

        # The cleanup script's first step, made literal: delete every
        # bottle_ledger row still stamped with the group, so the ledger FK cannot
        # shadow the fines FK below. This is the exact order such a script runs
        # in, and the point of the test is what it hits next.
        pgdb.session.execute(
            text("DELETE FROM bottle_ledger WHERE address_group_id = :g"), {"g": group.id}
        )
        pgdb.session.commit()

        # A's entries were re-stamped out on dissolve; B never had any. So no
        # bottle_ledger row carries the group any more...
        assert (
            pgdb.session.execute(
                text("SELECT count(*) FROM bottle_ledger WHERE address_group_id = :g"),
                {"g": group.id},
            ).scalar()
            == 0
        )
        # ...and the FINE is the only thing left holding the group row.
        fines_fk = _fk_name(pgdb, "bottle_fines", "address_group_id", "address_groups")
        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text("DELETE FROM address_groups WHERE id = :g"), {"g": group.id}
            )
            pgdb.session.commit()
        assert fines_fk in _integrity_message(exc_info.value)
        pgdb.session.rollback()
        assert BottleFine.query.get(fine.id) is not None

    def test_a_ledger_row_cannot_be_stamped_with_a_NONEXISTENT_group_id(self, papp, pgdb):
        """``absorb_address_into_group`` re-stamps with a bulk UPDATE and
        ``synchronize_session=False``. On SQLite (FKs off) a bug that passed the
        wrong id — the joiner's OLD group, a stale variable, ``None`` coerced to
        0 — writes happily and permanently mis-scopes the history."""
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        _deliver(pgdb, owner, a, 4)
        bogus = (
            pgdb.session.execute(text("SELECT COALESCE(MAX(id), 0) + 1000 FROM address_groups")).scalar()
        )
        expected = _fk_name(pgdb, "bottle_ledger", "address_group_id", "address_groups")

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text("UPDATE bottle_ledger SET address_group_id = :g WHERE address_id = :a"),
                {"g": bogus, "a": a.id},
            )
            pgdb.session.commit()
        assert expected in _integrity_message(exc_info.value)
        pgdb.session.rollback()
        assert _ledger_sum(pgdb, BottleScope.for_address(a.id)) == Decimal("4.00")

    def test_addresses_address_group_id_cannot_point_at_a_NONEXISTENT_group(
        self, papp, pgdb, pgclient
    ):
        """Every "``address_group_id IS NOT NULL`` means shared place" predicate
        (16 sites, 5 on the money path) depends on this being impossible. The
        service checks the group exists before writing membership, but nothing
        re-checks between that read and the flush; on Postgres it cannot go
        wrong, on SQLite a stranded pointer is invisible.

        The API half is asserted with it: a missing group is a 404, not a
        silently-written membership.
        """
        admin = _admin(pgdb)
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        bogus = pgdb.session.execute(
            text("SELECT COALESCE(MAX(id), 0) + 1000 FROM address_groups")
        ).scalar()
        expected = _fk_name(pgdb, "addresses", "address_group_id", "address_groups")

        with pytest.raises(IntegrityError) as exc_info:
            pgdb.session.execute(
                text("UPDATE addresses SET address_group_id = :g WHERE id = :a"),
                {"g": bogus, "a": a.id},
            )
            pgdb.session.commit()
        assert expected in _integrity_message(exc_info.value)
        pgdb.session.rollback()

        response = pgclient.post(
            f"{API}/admin/place-groups/{bogus}/addresses",
            json={"addressIds": [a.id], "reason": "typo in the group id"},
            headers=_headers(papp, admin),
        )
        assert response.status_code == 404, response.get_json()
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id is None

    def test_nothing_resolves_to_a_memberless_group_but_its_history_survives(
        self, papp, pgdb
    ):
        """The state the design calls INERT.

        If a reader ever filtered the ledger by ``address_id`` alone — the trap
        ``bottle_scope.ledger_filter``'s docstring names — a departed member
        would suddenly see the whole place's history, and ``reconcile_balance``
        would mint it onto them.
        """
        admin = _admin(pgdb)
        group, a, b, _oa, _ob = self._dissolved_group_with_departed_history(pgdb, admin)

        for address in (a, b):
            scope = BottleTrackingService.resolve_scope(address.id)
            assert scope.group_id is None, "a departed address must resolve to ITSELF"
            assert scope.address_id == address.id

        # B left with nothing, so its own scope must be empty on both figures.
        assert _pair(pgdb, BottleScope.for_address(b.id)) == (
            Decimal("0.00"),
            Decimal("0.00"),
        )
        # A, the last member out, inherited the WHOLE place (6 of its own + 5
        # that B left behind) — on both figures.
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("11.00"),
            Decimal("11.00"),
        )

        # The group's own scope keeps B's departed history as ROWS and holds no
        # balance row at all. Its ledger sums to zero because the dissolve's
        # `:out` half is what carried B's 5 across — the rows are retained, the
        # bottles are not double-counted.
        assert _stored(pgdb, BottleScope.for_group(group.id)) == Decimal("0.00")
        assert _raw_balance_rows(pgdb, group_id=group.id) == []
        assert _ledger_sum(pgdb, BottleScope.for_group(group.id)) == Decimal("0.00")
        retained = pgdb.session.execute(
            text(
                "SELECT id, address_id, quantity FROM bottle_ledger "
                "WHERE address_group_id = :g ORDER BY id"
            ),
            {"g": group.id},
        ).all()
        assert [r[1] for r in retained] == [b.id, a.id], (
            "the group scope must retain the departed member's row (and only the "
            "dissolve's own out-half beside it)"
        )
        assert [D(r[2]) for r in retained] == [Decimal("5.00"), Decimal("-5.00")]

        # get_all_balances offers no row for the group either.
        page = BottleTrackingService.get_all_balances(page=1, per_page=200)
        assert all(row.address_group_id != group.id for row in page["items"])


class TestTheDeleteFenceIsTheOnlyGuardForAGroupedAddress:
    """§7.3: ``bottle_balances.address_id NOT NULL`` used to make this fail with
    an IntegrityError. A grouped address has NO balance row of its own, so that
    guard evaporated for exactly the members who share a pool, and the
    application-level fence became load-bearing with NOTHING in the database
    behind it. These tests prove both halves of that sentence.
    """

    def _grouped_clean_address(self, pgdb, admin):
        """Grouped address A with zero ledger rows, zero fines, no balance row,
        whose owner has a second address (so the only-address guard passes)."""
        oa, ob = _user(pgdb), _user(pgdb)
        a = _addr(pgdb, oa)
        _addr(pgdb, oa, title="Spare")  # second address for the same owner
        b = _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        # History belongs to B only, so A itself is bottle-free.
        _deliver(pgdb, ob, b, 7)
        assert (
            pgdb.session.execute(
                text("SELECT count(*) FROM bottle_ledger WHERE address_id = :a"), {"a": a.id}
            ).scalar()
            == 0
        )
        assert _raw_balance_rows(pgdb, address_id=a.id) == []
        return group, a, oa

    def test_POSTGRES_WOULD_ALLOW_the_delete__no_FK_protects_a_grouped_address(
        self, papp, pgdb
    ):
        """The control that makes every fence test below meaningful: the raw
        DELETE SUCCEEDS. Executed inside a SAVEPOINT and rolled back, so the row
        survives the test."""
        admin = _admin(pgdb)
        _group_obj, a, _owner = self._grouped_clean_address(pgdb, admin)

        savepoint = pgdb.session.begin_nested()
        result = pgdb.session.execute(text("DELETE FROM addresses WHERE id = :a"), {"a": a.id})
        assert result.rowcount == 1, (
            "the raw DELETE was blocked — if a real FK now protects a grouped "
            "address, the §7.3 fence is no longer the only guard and this test "
            "should be rewritten rather than deleted"
        )
        savepoint.rollback()
        pgdb.session.rollback()
        assert UserAddress.query.get(a.id) is not None

    def test_all_THREE_delete_entry_points_refuse_a_grouped_address(
        self, papp, pgdb, pgclient
    ):
        """A fence on two of three is not a fence. Each of the three real routes
        is driven with a real JWT, and membership plus the place's balance are
        re-checked after every attempt."""
        admin = _admin(pgdb)
        group, a, owner = self._grouped_clean_address(pgdb, admin)
        customer_headers = _headers(papp, owner)
        admin_headers = _headers(papp, admin)
        place_before = _pair(pgdb, BottleScope.for_group(group.id))

        attempts = [
            ("customer /auth/addresses", f"{API}/auth/addresses/{a.id}", customer_headers),
            ("customer /addresses", f"{API}/addresses/{a.id}", customer_headers),
            (
                "admin /users/<uid>/addresses",
                f"{API}/admin/users/{owner.id}/addresses/{a.id}",
                admin_headers,
            ),
        ]
        for label, url, headers in attempts:
            response = pgclient.delete(url, headers=headers)
            payload = response.get_json()
            assert response.status_code == 400, f"{label}: {response.status_code} {payload}"
            assert _error_code(payload) == "PLACE_GROUP_ADDRESS_NOT_DELETABLE", (
                f"{label}: the machine-readable code is what the admin UI branches "
                f"on, and it is not in this envelope: {payload}"
            )
            pgdb.session.expire_all()
            assert UserAddress.query.get(a.id) is not None, f"{label} deleted the address"
            assert UserAddress.query.get(a.id).address_group_id == group.id
            assert _pair(pgdb, BottleScope.for_group(group.id)) == place_before

    def test_the_fence_fires_BEFORE_the_FK_for_a_grouped_address_WITH_history(
        self, papp, pgdb, pgclient
    ):
        """In ``admin.py`` the IntegrityError arm and the ValidationError arm are
        both present and ORDERING matters. If the fence call moved below
        ``db.session.delete``, the admin would get an opaque "referenced by
        existing records" with no machine-readable code, and the admin UI could
        not tell "remove it from the place first" from "it has orders"."""
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a = _addr(pgdb, oa)
        _addr(pgdb, oa, title="Spare")
        b = _addr(pgdb, ob)
        _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 4)  # A itself now has ledger rows

        response = pgclient.delete(
            f"{API}/admin/users/{oa.id}/addresses/{a.id}", headers=_headers(papp, admin)
        )
        payload = response.get_json()
        assert response.status_code == 400, payload
        assert _error_code(payload) == "PLACE_GROUP_ADDRESS_NOT_DELETABLE", payload
        assert "referenced by existing records" not in str(payload)

    def test_an_UNGROUPED_address_with_ledger_rows_is_blocked_by_the_REAL_FK(
        self, papp, pgdb, pgclient
    ):
        """On SQLite with FOREIGN KEYS OFF this delete SUCCEEDS and leaves
        dangling ``bottle_ledger.address_id`` rows — the exact class of bug the
        project's own SQLite-FK blind-spot note describes. Only a Postgres run
        proves the route's IntegrityError handler is ever reached at all."""
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        _addr(pgdb, owner, title="Spare", default=True)
        _deliver(pgdb, owner, a, 5)
        ledger_ids = sorted(
            r[0]
            for r in pgdb.session.execute(
                text("SELECT id FROM bottle_ledger WHERE address_id = :a"), {"a": a.id}
            ).all()
        )
        assert ledger_ids

        response = pgclient.delete(f"{API}/addresses/{a.id}", headers=_headers(papp, owner))
        payload = str(response.get_json())
        assert response.status_code == 400, payload
        assert "referenced by existing records" in payload
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id) is not None
        assert (
            sorted(
                r[0]
                for r in pgdb.session.execute(
                    text("SELECT id FROM bottle_ledger WHERE address_id = :a"), {"a": a.id}
                ).all()
            )
            == ledger_ids
        )

    def test_an_ungrouped_address_whose_ONLY_artefact_is_a_balance_row_is_blocked(
        self, papp, pgdb, pgclient
    ):
        """The dev address-24 shape — stored balance, ZERO ledger rows — is real
        production data. If only the ledger FK were relied on, this address
        would delete and its stored bottles would become a permanently orphaned
        balance row that nothing can ever reach."""
        admin = _admin(pgdb)
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        _addr(pgdb, owner, title="Spare", default=True)
        entry = _adjust(pgdb, admin, a, 20, notes="figure carried from before the ledger")
        _drop_ledger_row(pgdb, entry.id)
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("20.00"),
            Decimal("0.00"),
        )
        expected = _fk_name(pgdb, "bottle_balances", "address_id", "addresses")
        assert expected  # the FK the route's IntegrityError arm depends on

        response = pgclient.delete(f"{API}/addresses/{a.id}", headers=_headers(papp, owner))
        payload = str(response.get_json())
        assert response.status_code == 400, payload
        assert "referenced by existing records" in payload
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id) is not None
        assert _stored(pgdb, BottleScope.for_address(a.id)) == Decimal("20.00")

    def test_a_DEPARTED_address_passes_the_fence_and_is_blocked_by_the_FK(
        self, papp, pgdb, pgclient
    ):
        """A departed address looks CLEAN to every application-level check —
        its membership pointer is NULL — so only the FK stops the delete. If a
        future ``cascade=`` were added to the relationship, deleting it would
        silently strip rows out of a LIVE place's ledger sum, creating drift
        that ``reconcile_balance`` then bakes in."""
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a = _addr(pgdb, oa)
        _addr(pgdb, oa, title="Spare", default=True)
        b, c = _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 6)
        CustomerLinkService().remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="moved out"
        )
        pgdb.session.commit()

        # The fence PASSES: nothing raises.
        CustomerLinkService.assert_address_not_in_place_group(a.id)

        group_stamped = sorted(
            r[0]
            for r in pgdb.session.execute(
                text(
                    "SELECT id FROM bottle_ledger WHERE address_id = :a AND address_group_id = :g"
                ),
                {"a": a.id, "g": group.id},
            ).all()
        )
        assert group_stamped, "a departed member's rows must stay stamped with the group"

        response = pgclient.delete(f"{API}/addresses/{a.id}", headers=_headers(papp, oa))
        payload = str(response.get_json())
        assert response.status_code == 400, payload
        assert "referenced by existing records" in payload
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id) is not None
        assert (
            sorted(
                r[0]
                for r in pgdb.session.execute(
                    text(
                        "SELECT id FROM bottle_ledger WHERE address_id = :a AND address_group_id = :g"
                    ),
                    {"a": a.id, "g": group.id},
                ).all()
            )
            == group_stamped
        ), "the failed delete must not have removed or NULLed the group's ledger rows"
        assert _ledger_sum(pgdb, BottleScope.for_group(group.id)) == Decimal("6.00")

    def test_place_suggestion_dismissals_really_CASCADE_on_address_delete(
        self, papp, pgdb, pgclient
    ):
        """The one FK in the place machinery declared CASCADE rather than
        RESTRICT. If the ``ondelete`` were dropped in a migration, deleting an
        address that a suggestion was once dismissed for starts returning
        "referenced by existing records" to customers deleting a perfectly
        ordinary address."""
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        # Co-located to the 4th decimal so `dismiss_place_suggestion` takes its
        # point-fingerprint arm. Both inside the Tashkent delivery polygon —
        # `UserAddress` has a before_insert zone guard.
        a = _addr(pgdb, oa, lat=41.3220, lng=69.2650)
        _addr(pgdb, oa, title="Spare", default=True, lat=41.3230, lng=69.2660)
        b = _addr(pgdb, ob, lat=41.3220, lng=69.2650)
        dismissal = CustomerLinkService().dismiss_place_suggestion(
            a.id, b.id, acting_admin_id=admin.id, reason="different flats"
        )
        dismissal_id = dismissal.id

        low_fk = _fk_name(pgdb, "place_suggestion_dismissals", "address_id_low", "addresses")
        assert _fk_delete_action(pgdb, low_fk) == "c", (
            f"{low_fk} is no longer ON DELETE CASCADE — ordinary address deletes "
            "will start failing for customers"
        )

        response = pgclient.delete(f"{API}/addresses/{a.id}", headers=_headers(papp, oa))
        assert response.status_code == 200, response.get_json()
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id) is None
        assert PlaceSuggestionDismissal.query.get(dismissal_id) is None


class TestDerivedAttributionSatisfiesTheNotNullForeignKeys:
    """``bottle_ledger.user_id`` / ``address_id`` are NOT NULL with real FKs, and
    on a place-level write they are DERIVED from the place's representative
    (lowest-id member) address. ``resolve_place_attribution_user_id`` can return
    None in principle — its own docstring admits the empty-member_ids case —
    which is a NOT NULL violation on Postgres and a silently NULL row on SQLite.
    """

    def test_a_place_level_adjustment_books_the_REPRESENTATIVE_owner_on_a_real_FK(
        self, papp, pgdb
    ):
        """``user_id`` is DERIVED (representative = lowest-id member's owner)
        while ``address_id`` stays the address the admin acted on. Both columns
        are NOT NULL with real FKs, so this commit is the proof that the
        derivation produces a persistable row on Postgres — on SQLite a None
        would have been stored silently.

        The write is booked through B's door but stamped with A's owner; that is
        deliberate ("two identical calls can never attribute to two different
        coworkers") and is pinned here so the rule cannot drift away from
        ``serialize_bottle_balance``'s ``representative_address_id``.
        """
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        assert a.id < b.id
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, o1, a, 3)
        before = _pair(pgdb, BottleScope.for_group(group.id))

        entry = _adjust(pgdb, admin, b, 5, notes="counted the crates", owner=None)

        row = pgdb.session.execute(
            text(
                "SELECT user_id, address_id, address_group_id, quantity "
                "FROM bottle_ledger WHERE id = :i"
            ),
            {"i": entry.id},
        ).one()
        assert row[0] == o1.id, "user_id must be the representative (lowest-id) member's owner"
        assert row[1] == b.id, "address_id stays the address the admin acted on"
        assert row[2] == group.id
        assert D(row[3]) == Decimal("5.00")
        after = _pair(pgdb, BottleScope.for_group(group.id))
        assert after == (before[0] + Decimal("5.00"), before[1] + Decimal("5.00"))

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: a derived place-level ADMIN_ADJUSTMENT writes an INCOHERENT "
            "(user_id, address_id) pair — user_id is the representative member's "
            "owner while address_id is the address the admin acted on, so the row "
            "claims a coworker who does not own that address. `admin_adjustment` is "
            "NOT in PLACE_LEVEL_LEDGER_SOURCES, so "
            "serialize_customer_place_ledger_entry shows the OTHER coworker's name "
            "and is_own=True for an unexplained +/-N they did not cause — exactly the "
            "harm merge_correction/merge_backfill were redacted to prevent."
        ),
    )
    def test_every_ledger_row_user_id_OWNS_its_address_id(self, papp, pgdb, pgclient):
        """The coherence invariant every other writer satisfies.

        ``_split_bottles_out_of_place``, ``release_group_history_to_address`` and
        the merge anchor all take ``(user_id, address_id)`` from the SAME
        address, so their rows are self-consistent. The derived place-level
        adjustment is the only writer that mixes two people into one row.

        The CUSTOMER-VISIBLE consequence is fetched through the real
        ``/orders/bottles/my-ledger`` route first and carried into the failure
        message, so this pins a demonstrated harm rather than an abstraction:
        the representative is shown an unexplained +5 flagged ``is_own`` for an
        adjustment made at a coworker's door, with ``notes`` suppressed.
        """
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        assert a.id < b.id
        _group(pgdb, admin, [a, b])
        _deliver(pgdb, o1, a, 3)
        entry = _adjust(pgdb, admin, b, 5, notes="counted the crates", owner=None)

        response = pgclient.get(
            f"{API}/orders/bottles/my-ledger/{a.id}", headers=_headers(papp, o1)
        )
        assert response.status_code == 200, response.get_json()
        as_seen_by_the_representative = [
            item for item in response.get_json()["data"]["items"] if item["id"] == entry.id
        ]

        row = pgdb.session.execute(
            text(
                "SELECT l.user_id, l.address_id, ad.user_id "
                "FROM bottle_ledger l JOIN addresses ad ON ad.id = l.address_id "
                "WHERE l.id = :i"
            ),
            {"i": entry.id},
        ).one()
        assert row[0] == row[2], (
            f"ledger row {entry.id} is stamped with user {row[0]} but its "
            f"address {row[1]} is owned by user {row[2]}; customer {o1.id} is "
            f"served it as {as_seen_by_the_representative}"
        )

    def test_attribution_follows_the_representative_when_the_lowest_id_member_departs(
        self, papp, pgdb
    ):
        """Two copies of "which address represents this place" exist
        conceptually — the admin UI's ``representative_address_id`` and the
        derived attribution user. If they drift, the panel opens one place and
        the write books another; on Postgres a stale representative that has
        been deleted is a hard FK failure."""
        admin = _admin(pgdb)
        o1, o2, o3 = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, o1), _addr(pgdb, o2), _addr(pgdb, o3)
        assert a.id < b.id < c.id
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, o1, a, 9)

        CustomerLinkService().remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="left", bottles_leaving=0
        )
        pgdb.session.commit()
        assert _members(pgdb, group.id) == sorted([b.id, c.id])

        entry = _adjust(pgdb, admin, c, 2, notes="recount")
        row = pgdb.session.execute(
            text("SELECT user_id, address_id FROM bottle_ledger WHERE id = :i"), {"i": entry.id}
        ).one()
        assert row[0] == o2.id, (
            "after A's departure the representative must be the NEW lowest-id "
            "member (B), so the derived user_id is B's owner"
        )
        assert row[1] == c.id
        # The derivation is stable: asked through ANY member, same answer.
        assert (
            BottleTrackingService.resolve_place_attribution_user_id(b.id)
            == BottleTrackingService.resolve_place_attribution_user_id(c.id)
            == o2.id
        )

    def test_merge_anchor_rows_satisfy_the_NOT_NULL_FKs_when_the_merge_HAS_history(
        self, papp, pgdb, pgclient
    ):
        """The anchor is BORROWED from the lowest-id entry in the merged set. If
        it ever fell back to None the insert is a NOT NULL/FK error at commit —
        a 500 in the middle of an admin merge, with an ``AddressGroup``
        already flushed."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        first_entry = _deliver(pgdb, o1, a, 6)
        _deliver(pgdb, o2, b, 5)
        _give_back(pgdb, o2, b, 4)
        drift_entry = _adjust(pgdb, admin, a, 3, notes="carried figure")
        _drop_ledger_row(pgdb, drift_entry.id)

        preview = pgclient.get(
            f"{API}/admin/place-groups/merge-preview?address_ids={a.id},{b.id}",
            headers=_headers(papp, admin),
        ).get_json()["data"]
        response = pgclient.post(
            f"{API}/admin/place-groups",
            json={
                "addressIds": [a.id, b.id],
                "reason": "one office, counted 12",
                "previewEntryIds": preview["entry_ids"],
                "resultingBalance": 12,
            },
            headers=_headers(papp, admin),
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]

        rows = pgdb.session.execute(
            text(
                "SELECT idempotency_key, user_id, address_id, address_group_id "
                "FROM bottle_ledger WHERE idempotency_key LIKE :backfill "
                "   OR idempotency_key LIKE :correction"
            ),
            {"backfill": f"merge_backfill:{group_id}:%", "correction": f"merge_correction:{group_id}:%"},
        ).all()
        assert len(rows) == 2, f"expected a backfill AND a correction, got {rows}"
        for key, user_id, address_id, group_col in rows:
            assert user_id == first_entry.user_id, key
            assert address_id == first_entry.address_id, key
            assert group_col == group_id, key

    def test_merge_anchor_falls_back_to_the_lowest_id_JOINING_ADDRESS_with_NO_history(
        self, papp, pgdb, pgclient
    ):
        """The branch a bare ``min(preview['entries'])`` would raise ValueError
        on, and the ONLY path where the anchor comes from ``addresses`` rather
        than from the ledger — a different NOT NULL source with different
        failure modes."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        assert a.id < b.id

        response = pgclient.post(
            f"{API}/admin/place-groups",
            json={
                "addressIds": [b.id, a.id],
                "reason": "brand new office, twelve on site",
                "resultingBalance": 12,
                "previewEntryIds": [],
            },
            headers=_headers(papp, admin),
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]

        rows = pgdb.session.execute(
            text(
                "SELECT idempotency_key, user_id, address_id, quantity "
                "FROM bottle_ledger WHERE address_group_id = :g"
            ),
            {"g": group_id},
        ).all()
        assert len(rows) == 1, f"drift is 0, so only a correction should exist: {rows}"
        key, user_id, address_id, quantity = rows[0]
        assert key.startswith("merge_correction:")
        assert (user_id, address_id) == (o1.id, a.id)
        assert D(quantity) == Decimal("12.00")
        assert _pair(pgdb, BottleScope.for_group(group_id)) == (
            Decimal("12.00"),
            Decimal("12.00"),
        )

    def test_a_fine_frozen_to_a_group_keeps_a_VALID_FK_after_its_member_departs(
        self, papp, pgdb
    ):
        """If ``_fine_scope`` resolved from the address at payment time instead
        of the frozen column, FINE_ISSUED would land in the group ledger and
        FINE_PAID in the address ledger — the exact split the model's docstring
        warns about — and on Postgres a frozen id pointing at a group somebody
        tried to delete becomes an FK error."""
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 6)
        service = BottleTrackingService()
        fine = service.issue_fine(
            user_id=None,
            address_id=a.id,
            quantity=Decimal("2"),
            fine_amount=Decimal("40000"),
            actor_user_id=admin.id,
            notes="two missing",
        )
        pgdb.session.commit()
        assert fine.address_group_id == group.id

        CustomerLinkService().remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="left", bottles_leaving=0
        )
        pgdb.session.commit()
        assert UserAddress.query.get(a.id).address_group_id is None

        service.mark_fine_paid(fine.id, actor_user_id=admin.id, notes="settled")
        pgdb.session.commit()

        scopes = pgdb.session.execute(
            text(
                "SELECT event_type, address_group_id FROM bottle_ledger "
                "WHERE (entry_metadata ->> 'fine_id')::int = :f ORDER BY id"
            ),
            {"f": fine.id},
        ).all()
        assert len(scopes) == 2, scopes
        assert {row[1] for row in scopes} == {group.id}, (
            "both halves of the fine pair must land in the FROZEN group scope"
        )
        # The frozen id still references a live row, and the debit hit the PLACE.
        assert AddressGroup.query.get(fine.address_group_id) is not None
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("4.00"),
            Decimal("4.00"),
        )


# =========================================================================== #
# 3. LOCKING — deterministic ``lock_timeout`` proofs
# =========================================================================== #
#
# Every test in this class follows one shape:
#
#   * an INDEPENDENT psycopg2 connection takes exactly one row FOR UPDATE and
#     holds it;
#   * the session under test sets ``SET LOCAL lock_timeout`` and runs the real
#     service call;
#   * Postgres raises 55P03 iff the service genuinely tried to take that row.
#
# There is no sleeping, no thread scheduling and no polling, so these are fully
# deterministic — and each one is a claim ``with_for_update()``-as-a-no-op
# SQLite cannot make at all. Two ORDER proofs at the end need a thread, and are
# marked as such.


class TestTheJoinTakesBothRowsAndTakesTheGroupRowFirst:
    def test_the_join_WAITS_for_the_joining_addresss_own_balance_row(
        self, papp, pgdb, raw
    ):
        """The join genuinely CONTENDS for the joining address's balance row.

        WHAT THIS DOES NOT PROVE, stated because it was measured: deleting
        ``.with_for_update()`` from ``absorb_address_into_group`` leaves this test
        GREEN, because the ``DELETE`` that follows the read blocks on the
        observer anyway. So this pins the contention, not the lock. The
        lost-update test at the end of this class is the one that fails against
        the unlocked shape — that pair is the evidence, not this test alone.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        _deliver(pgdb, oa, a, 6)

        observer = raw()
        _hold_row_for_update(observer, address_id=a.id)

        with pytest.raises(DBAPIError) as exc_info:
            with _lock_timeout(pgdb):
                CustomerLinkService().create_place_group(
                    [a.id, b.id], acting_admin_id=admin.id, reason="same office"
                )
        _assert_lock_timeout(exc_info)

        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id is None, (
            "a lock failure must leave NO membership behind"
        )
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("6.00"),
            Decimal("6.00"),
        ), "a rolled-back join must leave BOTH halves of the joiner's scope intact"

        observer.rollback()  # release
        group = _group(pgdb, admin, [a, b])
        assert _raw_balance_rows(pgdb, address_id=a.id) == []
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("6.00"),
            Decimal("6.00"),
        )

    def test_the_join_WAITS_for_the_DESTINATION_GROUPS_balance_row(
        self, papp, pgdb, raw
    ):
        """The join genuinely CONTENDS for the destination group's balance row.

        Same honesty note as above: removing ``.with_for_update()`` from
        ``_absorb_joiners_into_group`` keeps this test green, because step 5's
        ``place_row.balance = ... + absorbed`` UPDATE blocks on its own. The two
        tests that DO fail against the unlocked shape are the ORDER proof below
        (the prober finds the group row free) and the concurrent-joins test in
        section 4 (an absorbed balance is lost, 12.00 instead of 15.00).
        """
        admin = _admin(pgdb)
        o1, o2, od = _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2, d = _addr(pgdb, o1), _addr(pgdb, o2), _addr(pgdb, od)
        group = _group(pgdb, admin, [a1, a2])
        _deliver(pgdb, o1, a1, 10)
        _deliver(pgdb, od, d, 4)

        observer = raw()
        _hold_row_for_update(observer, group_id=group.id)

        with pytest.raises(DBAPIError) as exc_info:
            with _lock_timeout(pgdb):
                CustomerLinkService().add_addresses_to_group(
                    group.id, [d.id], acting_admin_id=admin.id, reason="joins the office"
                )
        _assert_lock_timeout(exc_info)
        pgdb.session.expire_all()
        assert UserAddress.query.get(d.id).address_group_id is None

        observer.rollback()
        CustomerLinkService().add_addresses_to_group(
            group.id, [d.id], acting_admin_id=admin.id, reason="joins the office"
        )
        pgdb.session.commit()
        assert _raw_balance_rows(pgdb, address_id=d.id) == []
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("14.00"),
            Decimal("14.00"),
        )

    def test_the_join_takes_the_ADDRESSES_row_write_lock_BEFORE_any_bottle_row(
        self, papp, pgdb, raw
    ):
        """THE THIRD LOCKABLE RESOURCE, which no ordering docstring names.

        ``_absorb_joiners_into_group`` step 1 sets ``addr.address_group_id`` and
        FLUSHES for every joiner — an ``addresses`` ROW WRITE-LOCK — and only
        step 2 takes the group's ``bottle_balances`` row. The removal path
        acquires ``addresses(A)`` LAST. That is a genuine A-then-B / B-then-A
        pattern ACROSS TWO TABLES, currently unreachable only because the
        membership fence guarantees a join and a removal never target the same
        address row.

        Proven two ways here: the join blocks when ``addresses(D)`` is held, and
        while it is blocked the group's balance row is still FREE — i.e. the
        addresses row was taken first.
        """
        admin = _admin(pgdb)
        o1, o2, od = _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2, d = _addr(pgdb, o1), _addr(pgdb, o2), _addr(pgdb, od)
        group = _group(pgdb, admin, [a1, a2])
        _deliver(pgdb, o1, a1, 10)
        _deliver(pgdb, od, d, 4)
        pgdb.session.commit()

        observer = raw()
        _hold_address_row_for_update(observer, d.id)
        probe = raw(autocommit=True)
        prober = raw()

        sink: dict = {}

        def join():
            CustomerLinkService().add_addresses_to_group(
                group.id, [d.id], acting_admin_id=admin.id, reason="joins the office"
            )
            return "done"

        thread = _in_app(papp, join, sink, "join")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                "the join never blocked on the addresses row — step 1's flush no "
                "longer takes an addresses ROW WRITE-LOCK, which changes the "
                "feature's lock footprint"
            )
            assert not _group_row_is_held(prober, group.id), (
                "the group's bottle_balances row was ALREADY held while the join "
                "waited on the addresses row — the two resources are now acquired "
                "in the opposite order from what this test documents"
            )
        finally:
            observer.rollback()
            thread.join(timeout=30)
        assert not thread.is_alive()
        assert _reraise(sink, "join") == "done"

        pgdb.session.expire_all()
        assert UserAddress.query.get(d.id).address_group_id == group.id
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("14.00"),
            Decimal("14.00"),
        )

    def test_ORDER_PROOF_the_join_already_HOLDS_the_group_row_while_waiting_for_the_address_row(
        self, papp, pgdb, raw
    ):
        """The ONLY dynamic evidence for the group-before-address rule.

        The static pin in ``test_bottle_place_lock_order.py`` compares source-text
        positions and would happily pass if the lock moved inside a branch that
        never executes. Here: an observer holds the JOINER's balance row, the
        join blocks on it in a thread, and a third connection asks — with
        ``FOR UPDATE NOWAIT`` — whether the GROUP's row is free. It must NOT be.

        Reversing the order (address first) makes the prober succeed, because the
        blocked join would not yet hold the group row. That is the discrimination
        no other construction provides.
        """
        admin = _admin(pgdb)
        o1, o2, od = _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2, d = _addr(pgdb, o1), _addr(pgdb, o2), _addr(pgdb, od)
        group = _group(pgdb, admin, [a1, a2])
        _deliver(pgdb, o1, a1, 10)
        _deliver(pgdb, od, d, 4)
        pgdb.session.commit()
        before = _sum_over(
            pgdb, [BottleScope.for_group(group.id), BottleScope.for_address(d.id)]
        )

        observer = raw()
        _hold_row_for_update(observer, address_id=d.id)
        probe = raw(autocommit=True)
        prober = raw()
        sink: dict = {}

        def join():
            CustomerLinkService().add_addresses_to_group(
                group.id, [d.id], acting_admin_id=admin.id, reason="joins the office"
            )
            return "done"

        thread = _in_app(papp, join, sink, "join")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                "the join never blocked on the joiner's balance row"
            )
            assert _group_row_is_held(prober, group.id), (
                "the group's balance row was FREE while the join waited on the "
                "joiner's address row — the join is taking the ADDRESS row before "
                "the GROUP row, which is the ABBA the whole ordering rule forbids"
            )
        finally:
            observer.rollback()
            thread.join(timeout=30)
        assert not thread.is_alive()
        assert _reraise(sink, "join") == "done"

        pgdb.session.expire_all()
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("14.00"),
            Decimal("14.00"),
        )
        assert (
            _sum_over(pgdb, [BottleScope.for_group(group.id), BottleScope.for_address(d.id)])
            == before
        ), "conservation across the two scopes the join touched"

    # ------------------------------------------------------------------ #
    # The lost-update race, run once and asserted from two angles
    # ------------------------------------------------------------------ #

    def _race_a_late_delivery_against_the_join(self, papp, pgdb, raw):
        """Commit a delivery at the joiner AFTER the join has started waiting.

        Two real sessions: session 1 records a +3 delivery at the joining
        address and holds its ``bottle_balances`` row UNCOMMITTED; the join then
        starts and must block on that row; the delivery commits; the join
        finishes. This is the exact interleaving an admin grouping two addresses
        while a driver marks a delivery delivered produces.

        Returns ``(group_id, address_id, late_order_id)``.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        _deliver(pgdb, oa, a, 5)
        late_order = _order(pgdb, oa, a)
        pgdb.session.commit()
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("5.00"),
            Decimal("5.00"),
        )

        probe = raw(autocommit=True)
        ready, go = threading.Event(), threading.Event()
        sink: dict = {}

        def late_delivery():
            from business_app import db as _db

            BottleTrackingService().record_bottles_delivered(
                late_order.id, oa.id, a.id, Decimal("3.00")
            )
            _db.session.flush()      # holds a.id's balance row FOR UPDATE
            ready.set()
            assert go.wait(timeout=30)
            _db.session.commit()
            return "delivered"

        def join():
            CustomerLinkService().create_place_group(
                [a.id, b.id], acting_admin_id=admin.id, reason="same office"
            )
            return "joined"

        deliverer = _in_app(papp, late_delivery, sink, "deliver")
        try:
            assert ready.wait(timeout=30), "the concurrent delivery never took its lock"
            joiner = _in_app(papp, join, sink, "join")
            try:
                # The join must WAIT: it wants the very row the delivery holds.
                assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                    "the join did not block on the joining address's balance row "
                    "— absorb_address_into_group is no longer taking it FOR UPDATE"
                )
            finally:
                go.set()
                joiner.join(timeout=60)
        finally:
            go.set()
            deliverer.join(timeout=60)
        assert _reraise(sink, "deliver") == "delivered"
        assert _reraise(sink, "join") == "joined"

        pgdb.session.expire_all()
        group_id = UserAddress.query.get(a.id).address_group_id
        assert group_id is not None, "the join must have committed"
        return group_id, a.id, late_order.id

    def test_a_CONCURRENT_DELIVERY_is_not_swallowed_from_the_place_BALANCE(
        self, papp, pgdb, raw
    ):
        """THE test that can fail against the pre-fix absorb.

        With an unlocked read-then-delete the absorb reads 5, the DELETE blocks,
        the delivery commits 8, the DELETE removes the 8-row and 5 is credited —
        THREE BOTTLES DESTROYED with no error anywhere. Everything else about the
        two implementations is externally identical, which is why this
        construction is the only externally visible difference between them.
        """
        group_id, address_id, _late = self._race_a_late_delivery_against_the_join(
            papp, pgdb, raw
        )
        assert _stored(pgdb, BottleScope.for_group(group_id)) == Decimal("8.00"), (
            "the concurrent +3 was swallowed by the absorb — this is the lost "
            "update the FOR UPDATE on the joiner's balance row exists to prevent"
        )
        assert _raw_balance_rows(pgdb, address_id=address_id) == [], (
            "the joiner's own balance row must be gone after the absorb"
        )

    def test_a_CONCURRENT_DELIVERY_is_also_re_scoped_in_the_LEDGER(
        self, papp, pgdb, raw
    ):
        """FIXED — the xfail is gone. §7.2's two halves move TOGETHER again.

        WAS: `absorb_address_into_group` re-stamped the joiner's ledger rows
        BEFORE it took that address's `bottle_balances` row FOR UPDATE, so the
        lock protected the BALANCE but not the LEDGER. A delivery committing
        during the join was picked up by the locked balance read (+3) but had
        been invisible to the earlier unlocked ledger SELECT, so its entry stayed
        stamped `address_group_id=NULL`. The place carried a PERMANENT
        stored-vs-ledger drift of +3, `get_place_ledger()` never showed that real
        delivery, and Reconcile then DESTROYED the three bottles.

        NOW the delivery cannot commit inside that window at all: the join holds
        `addresses(joiner)` FOR NO KEY UPDATE from `_load_addresses`, and the
        delivery takes the same row FOR SHARE before it resolves its scope. It
        either lands wholly before the join (and is re-stamped with everything
        else) or wholly after (and resolves to the place) — never half of each.
        """
        group_id, address_id, late_order_id = self._race_a_late_delivery_against_the_join(
            papp, pgdb, raw
        )
        late = BottleLedger.query.filter_by(idempotency_key=f"delivery:{late_order_id}").one()
        assert late.address_group_id == group_id, (
            f"the concurrent delivery's ledger entry is still stamped "
            f"address_group_id={late.address_group_id} while its address "
            f"{address_id} belongs to place group {group_id}"
        )
        stored, ledger = _pair(pgdb, BottleScope.for_group(group_id))
        assert (stored, ledger) == (Decimal("8.00"), Decimal("8.00"))
        # And nothing may be stranded in the (now grouped) address's own scope.
        assert _pair(pgdb, BottleScope.for_address(address_id)) == (
            Decimal("0.00"),
            Decimal("0.00"),
        )


class TestTheSplitAndTheDissolveTakeTheGroupRowBeforeWritingAnything:
    def test_the_split_BLOCKS_on_the_group_row_and_writes_NOTHING(self, papp, pgdb, raw):
        """The ``CustomerLinkEvent`` is flushed BEFORE the split. If the
        transaction were not rolled back cleanly on a lock failure, a dangling
        audit event for a removal that never happened would be adopted by the
        next commit on the session — the exact hazard the "validated before
        anything is written" comments describe."""
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 8)
        events_before = CustomerLinkEvent.query.filter(
            CustomerLinkEvent.event_type == "remove_from_place_group"
        ).count()

        observer = raw()
        _hold_row_for_update(observer, group_id=group.id)

        with pytest.raises(DBAPIError) as exc_info:
            with _lock_timeout(pgdb):
                CustomerLinkService().remove_address_from_group(
                    a.id, acting_admin_id=admin.id, reason="split", bottles_leaving=3
                )
        _assert_lock_timeout(exc_info)
        observer.rollback()

        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id == group.id
        assert _raw_balance_rows(pgdb, address_id=a.id) == []
        assert (
            BottleLedger.query.filter(
                BottleLedger.idempotency_key.like(f"place_leave:{group.id}:%")
            ).count()
            == 0
        )
        assert (
            CustomerLinkEvent.query.filter(
                CustomerLinkEvent.event_type == "remove_from_place_group"
            ).count()
            == events_before
        ), "a rolled-back removal must leave NO audit event behind"
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("8.00"),
            Decimal("8.00"),
        )

    def test_the_DISSOLVE_blocks_on_the_group_row_taken_UNCONDITIONALLY_first(
        self, papp, pgdb, raw
    ):
        """``release_group_history_to_address`` takes the group row via
        ``get_or_create_balance`` at the TOP, precisely so the path is safe by
        ORDERING rather than by the join's membership fence. Making that
        acquisition lazy (only on ``own_sum != 0``) is a tempting
        micro-optimisation that silently turns this into a late acquirer — and
        with ``bottles_leaving`` defaulting to 0 the dissolve is then the FIRST
        bottle work the whole removal does."""
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 10)

        observer = raw()
        _hold_row_for_update(observer, group_id=group.id)

        with pytest.raises(DBAPIError) as exc_info:
            with _lock_timeout(pgdb):
                CustomerLinkService().remove_address_from_group(
                    a.id, acting_admin_id=admin.id, reason="last one out"
                )
        _assert_lock_timeout(exc_info)

        pgdb.session.expire_all()
        assert sorted(_members(pgdb, group.id)) == sorted([a.id, b.id]), (
            "the dissolve blocked, so NOTHING may have been un-pointed"
        )
        assert (
            BottleLedger.query.filter(
                BottleLedger.idempotency_key.like(f"place_dissolve:{group.id}:%")
            ).count()
            == 0
        )
        assert _raw_balance_rows(pgdb, address_id=b.id) == []

        observer.rollback()
        result = CustomerLinkService().remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="last one out"
        )
        pgdb.session.commit()
        assert result["dissolved"] is True
        assert _members(pgdb, group.id) == []
        assert _raw_balance_rows(pgdb, group_id=group.id) == [], (
            "the dissolve must delete the group's balance row"
        )
        assert _pair(pgdb, BottleScope.for_address(b.id)) == (
            Decimal("10.00"),
            Decimal("10.00"),
        )


    def test_ORDER_PROOF_the_removal_takes_the_ADDRESSES_rows_FIRST_before_any_bottle_row(
        self, papp, pgdb, raw
    ):
        """INVERTED BY THE LADDER FIX — and this inversion is the single most
        direct evidence that the two-removal 40P01 is gone.

        BEFORE: the removal took ``bottle_balances(G)`` and only then
        ``addresses(A)``, while the JOIN took ``addresses(D)`` first and the
        group's balance row second. A genuine A-then-B / B-then-A across two
        tables, unreachable only because a (false) membership fence was assumed
        to guarantee a join and a removal never target the SAME address row. Two
        concurrent removals from a two-member place closed it and deadlocked.
        This test used to assert ``_group_row_is_held(prober, group.id)``.

        AFTER: both paths climb ONE total order — ``address_groups`` ->
        ``addresses`` (ascending id, one statement) -> ``bottle_balances``. So
        with ``addresses(A)`` held by an observer, the removal blocks on it at
        RUNG 1, before it has taken any bottle row at all, and the prober's
        ``FOR UPDATE NOWAIT`` on the group's balance row must now SUCCEED.

        Its sibling ``test_the_join_takes_the_ADDRESSES_row_write_lock_BEFORE_
        any_bottle_row`` stays green and becomes the general rule rather than
        half of a cycle.

        IF THIS TEST DOES NOT INVERT, rung 1 is not actually preceding rung 2 on
        the removal path and the fix is illusory, no matter what the deadlock
        test reports on any given run.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 8)
        pgdb.session.commit()

        observer = raw()
        _hold_address_row_for_update(observer, a.id)
        probe = raw(autocommit=True)
        prober = raw()
        sink: dict = {}

        def remove():
            CustomerLinkService().remove_address_from_group(
                a.id, acting_admin_id=admin.id, reason="moves out", bottles_leaving=3
            )
            return "removed"

        thread = _in_app(papp, remove, sink, "remove")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                "the removal never blocked on the departing address's own row"
            )
            assert not _group_row_is_held(prober, group.id), (
                "the group's bottle_balances row was ALREADY held while the "
                "removal waited on addresses(A) — rung 2 is being taken before "
                "rung 1, which is the pre-fix order and re-opens the 40P01 "
                "between two concurrent removals"
            )
        finally:
            observer.rollback()
            thread.join(timeout=30)
        assert _reraise(sink, "remove") == "removed"

        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id is None
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("3.00"),
            Decimal("3.00"),
        )
        # BOTH halves of the other side too: a split that moved the balance but
        # not the ledger conserves the global total while corrupting the split.
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("5.00"),
            Decimal("5.00"),
        )
        assert _sum_over(
            pgdb, [BottleScope.for_group(group.id), BottleScope.for_address(a.id)]
        ) == Decimal("8.00")


class TestTheLadderIsClimbedInOrderFromTheBottom:
    """RUNG 1 BEFORE RUNG 2, proven from the blocked side on both paths.

    The inversion proof above shows the removal no longer holds a bottle row
    while waiting on ``addresses``. These two show the same thing for the
    highest-volume path in the system (a delivery) and for the top of the
    ladder (``address_groups``), because "the fix is the ladder" is only true if
    every path climbs it from the bottom.
    """

    def test_a_delivery_blocked_on_the_ADDRESSES_row_holds_NO_bottle_row(
        self, papp, pgdb, raw
    ):
        """The delivery takes rung 1 BEFORE it chooses a balance row.

        That order is the whole scope fence: resolving the mapping under the
        lock means the place it picks is the place the mapping still names at
        COMMIT. If the balance row were taken first, the delivery would be
        holding rung 2 while waiting for rung 1 — the exact reverse of the
        lifecycle, and a manufactured ABBA on the busiest path in the system.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 5)
        order = _order(pgdb, oa, a)
        pgdb.session.commit()

        observer = raw()
        _hold_address_row_for_update(observer, a.id)
        probe = raw(autocommit=True)
        prober = raw()
        sink: dict = {}

        def deliver():
            from business_app import db as _db

            entry = BottleTrackingService().record_bottles_delivered(
                order.id, oa.id, a.id, Decimal("2.00")
            )
            _db.session.commit()
            return entry.id

        thread = _in_app(papp, deliver, sink, "deliver")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                "the delivery never blocked on addresses(A) — resolve_scope_for_write "
                "is no longer taking rung 1, and the scope fence is gone"
            )
            assert not _group_row_is_held(prober, group.id), (
                "the place's bottle_balances row was ALREADY held while the "
                "delivery waited on addresses(A) — rung 2 is being taken before "
                "rung 1 on the delivery path"
            )
        finally:
            observer.rollback()
            thread.join(timeout=30)
        assert not thread.is_alive()
        _reraise(sink, "deliver")

        pgdb.session.expire_all()
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("7.00"),
            Decimal("7.00"),
        )

    def test_a_removal_blocked_on_the_ADDRESS_GROUPS_row_holds_NEITHER_lower_rung(
        self, papp, pgdb, raw
    ):
        """RUNG 0 IS FIRST, and nothing below it is held while it is contended.

        ``address_groups(G)`` is the membership mutex: it is what makes locking
        the member SET (a predicate) sound, and it is what serialises two
        removals, or a join and a removal, on one place. If a lifecycle
        operation could hold an ``addresses`` row or a ``bottle_balances`` row
        while queueing for it, the total order would not be total.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 8)
        pgdb.session.commit()

        observer = raw()
        _hold_group_membership_row_for_update(observer, group.id)
        probe = raw(autocommit=True)
        prober = raw()
        sink: dict = {}

        def remove():
            CustomerLinkService().remove_address_from_group(
                a.id, acting_admin_id=admin.id, reason="moves out", bottles_leaving=3
            )
            return "removed"

        thread = _in_app(papp, remove, sink, "remove")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                "the removal never blocked on address_groups(G) — rung 0 is not "
                "being taken, and the member-set lock below it is unsound"
            )
            assert not _address_row_is_held(prober, a.id), (
                "an addresses row was held while the removal queued for "
                "address_groups(G) — rung 1 is being taken before rung 0"
            )
            assert not _group_row_is_held(prober, group.id), (
                "the place's bottle_balances row was held while the removal "
                "queued for address_groups(G) — rung 2 before rung 0"
            )
        finally:
            observer.rollback()
            thread.join(timeout=30)
        assert not thread.is_alive()
        assert _reraise(sink, "remove") == "removed"

        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id is None
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("3.00"),
            Decimal("3.00"),
        )


class TestOnePlaceIsOneLock:
    def test_a_delivery_at_member_B_WAITS_for_the_place_row_member_A_shares(
        self, papp, pgdb, raw
    ):
        """Grouping two customers turned two independent locks into ONE shared
        lock. That is the root cause of every cross-resource deadlock risk in
        the three plans (and why ``OrderEditService``'s cash-before-bottles rule
        exists), and nothing in the fast suite can demonstrate it."""
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 5)
        order = _order(pgdb, ob, b)

        observer = raw()
        _hold_row_for_update(observer, group_id=group.id)

        with pytest.raises(DBAPIError) as exc_info:
            with _lock_timeout(pgdb):
                BottleTrackingService().record_bottles_delivered(
                    order.id, ob.id, b.id, Decimal("2.00")
                )
        _assert_lock_timeout(exc_info)
        observer.rollback()

        pgdb.session.expire_all()
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("5.00"),
            Decimal("5.00"),
        )

    def test_two_DIFFERENT_places_do_not_falsely_serialise(self, papp, pgdb, raw):
        """If a future "simplification" locked ``bottle_balances`` with a
        table-level or predicate lock (or dropped the scope filter from the
        FOR UPDATE query), EVERY delivery in the system would serialise behind
        every other — a production-wide throughput collapse no functional test
        detects."""
        admin = _admin(pgdb)
        o1, o2, o3, o4 = _user(pgdb), _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
        b1, b2 = _addr(pgdb, o3), _addr(pgdb, o4)
        g1 = _group(pgdb, admin, [a1, a2])
        g2 = _group(pgdb, admin, [b1, b2])
        _deliver(pgdb, o1, a1, 5)
        _deliver(pgdb, o3, b1, 5)
        order = _order(pgdb, o3, b1)

        observer = raw()
        _hold_row_for_update(observer, group_id=g1.id)

        with _lock_timeout(pgdb, ms=1500):
            BottleTrackingService().record_bottles_delivered(
                order.id, o3.id, b1.id, Decimal("2.00")
            )
            pgdb.session.commit()
        observer.rollback()

        assert _pair(pgdb, BottleScope.for_group(g2.id)) == (
            Decimal("7.00"),
            Decimal("7.00"),
        )
        assert _pair(pgdb, BottleScope.for_group(g1.id)) == (
            Decimal("5.00"),
            Decimal("5.00"),
        )


class TestTheMergeReviewIsASingleRowAcquirer:
    def test_a_reviewed_join_does_not_block_on_an_UNRELATED_addresss_row(
        self, papp, pgdb, raw
    ):
        """``_apply_merge_review`` writes every entry on ``BottleScope.for_group``
        and takes exactly ONE ``bottle_balances`` row. Adding one address-scoped
        write (e.g. "also correct the joiner") would make it a FOURTH two-row
        acquirer with no ordering analysis, and only a Postgres lock test can
        show the widened footprint."""
        admin = _admin(pgdb)
        o1, o2, od, ox = _user(pgdb), _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2, d, x = (
            _addr(pgdb, o1),
            _addr(pgdb, o2),
            _addr(pgdb, od),
            _addr(pgdb, ox),
        )
        group = _group(pgdb, admin, [a1, a2])
        _deliver(pgdb, o1, a1, 10)
        _deliver(pgdb, od, d, 4)
        _deliver(pgdb, ox, x, 9)          # completely unrelated place
        pgdb.session.commit()

        preview = BottleTrackingService.build_merge_preview([d.id], group_id=group.id)
        entry_ids = list(preview["entry_ids"])

        observer = raw()
        _hold_row_for_update(observer, address_id=x.id)

        with _lock_timeout(pgdb, ms=2500):
            CustomerLinkService().add_addresses_to_group(
                group.id,
                [d.id],
                acting_admin_id=admin.id,
                reason="counted 20 on site",
                preview_entry_ids=entry_ids,
                resulting_balance=20,
            )
        observer.rollback()

        pgdb.session.expire_all()
        stored, ledger = _pair(pgdb, BottleScope.for_group(group.id))
        assert stored == Decimal("20.00")
        assert ledger == stored, (
            "the whole point of the review: after it, the place's stored balance "
            "and its ledger sum are the SAME number"
        )
        assert _pair(pgdb, BottleScope.for_address(x.id)) == (
            Decimal("9.00"),
            Decimal("9.00"),
        ), "the unrelated place must be untouched on BOTH figures"


# =========================================================================== #
# 4. REAL CONCURRENCY — two sessions, real transactions, real READ COMMITTED
# =========================================================================== #


class TestConcurrentFirstTimeDeliveriesAtANewPlace:
    def test_two_deliveries_racing_the_SAME_new_places_row_into_existence(
        self, papp, pgdb, raw
    ):
        """The exact create-race ``ON CONFLICT DO NOTHING`` + re-select-FOR-UPDATE
        exists for, and a complete no-op on SQLite.

        Deterministic: session 1 inserts the row and holds it UNCOMMITTED, so
        session 2's own ``ON CONFLICT`` insert blocks on the uncommitted unique
        index entry. If the FOR UPDATE re-select were dropped, session 2 would
        read a stale zero and overwrite session 1 — FOUR BOTTLES VANISH.
        """
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
        group = _group(pgdb, admin, [a1, a2])
        assert _raw_balance_rows(pgdb, group_id=group.id) == [], (
            "the place must start with NO balance row for this to be a create-race"
        )
        order1, order2 = _order(pgdb, o1, a1), _order(pgdb, o2, a2)
        pgdb.session.commit()

        probe = raw(autocommit=True)
        ready, go = threading.Event(), threading.Event()
        sink: dict = {}

        def first():
            from business_app import db as _db

            BottleTrackingService().record_bottles_delivered(
                order1.id, o1.id, a1.id, Decimal("4.00")
            )
            _db.session.flush()
            ready.set()
            assert go.wait(timeout=30)
            _db.session.commit()
            return "first"

        def second():
            from business_app import db as _db

            BottleTrackingService().record_bottles_delivered(
                order2.id, o2.id, a2.id, Decimal("6.00")
            )
            _db.session.commit()
            return "second"

        t1 = _in_app(papp, first, sink, "first")
        try:
            assert ready.wait(timeout=30)
            t2 = _in_app(papp, second, sink, "second")
            try:
                assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                    "the second delivery did not block — it never contended for "
                    "the place's single row"
                )
            finally:
                go.set()
                t2.join(timeout=60)
        finally:
            go.set()
            t1.join(timeout=60)
        assert _reraise(sink, "first") == "first"
        assert _reraise(sink, "second") == "second"

        pgdb.session.expire_all()
        rows = _raw_balance_rows(pgdb, group_id=group.id)
        assert len(rows) == 1, f"the create-race produced {len(rows)} rows: {rows}"
        assert rows[0][3] == Decimal("10.00")
        assert _ledger_sum(pgdb, BottleScope.for_group(group.id)) == Decimal("10.00")
        snapshots = sorted(
            D(r[0])
            for r in pgdb.session.execute(
                text("SELECT balance_after FROM bottle_ledger WHERE address_group_id = :g"),
                {"g": group.id},
            ).all()
        )
        assert snapshots == [Decimal("4.00"), Decimal("10.00")], (
            "both entries recorded the same running total — the second read a "
            "stale balance instead of the locked one"
        )

    def test_two_deliveries_at_two_DIFFERENT_new_places_do_not_collide_on_the_NULL_address_id(
        self, papp, pgdb
    ):
        """Same NULLS-DISTINCT hazard as the DDL test, but reached through the
        real write path under contention — which is how it would actually
        surface in production."""
        admin = _admin(pgdb)
        o1, o2, o3, o4 = _user(pgdb), _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
        b1, b2 = _addr(pgdb, o3), _addr(pgdb, o4)
        g1 = _group(pgdb, admin, [a1, a2])
        g2 = _group(pgdb, admin, [b1, b2])
        order1, order2 = _order(pgdb, o1, a1), _order(pgdb, o3, b1)
        pgdb.session.commit()

        barrier = threading.Barrier(2, timeout=30)
        sink: dict = {}

        def deliver(order_id, user_id, address_id, qty):
            def run():
                from business_app import db as _db

                barrier.wait()
                BottleTrackingService().record_bottles_delivered(
                    order_id, user_id, address_id, Decimal(qty)
                )
                _db.session.commit()
                return qty

            return run

        t1 = _in_app(papp, deliver(order1.id, o1.id, a1.id, "3.00"), sink, "g1")
        t2 = _in_app(papp, deliver(order2.id, o3.id, b1.id, "7.00"), sink, "g2")
        t1.join(timeout=60)
        t2.join(timeout=60)
        assert _reraise(sink, "g1") == "3.00"
        assert _reraise(sink, "g2") == "7.00"

        pgdb.session.expire_all()
        assert _pair(pgdb, BottleScope.for_group(g1.id)) == (
            Decimal("3.00"),
            Decimal("3.00"),
        )
        assert _pair(pgdb, BottleScope.for_group(g2.id)) == (
            Decimal("7.00"),
            Decimal("7.00"),
        )
        rows = pgdb.session.execute(
            text(
                "SELECT address_group_id, address_id FROM bottle_balances "
                " WHERE address_group_id = ANY(:g)"
            ),
            {"g": [g1.id, g2.id]},
        ).all()
        assert len(rows) == 2 and all(r[1] is None for r in rows)


class TestConcurrentPlaceLifecycleOperations:
    def test_a_SPLIT_and_a_JOIN_on_one_place_neither_deadlock_nor_lose_bottles(
        self, papp, pgdb, raw
    ):
        """Both paths take G's balance row first, but the JOIN also takes the
        ``addresses`` ROW WRITE-LOCK for D before it touches any bottle row,
        while the removal takes ``addresses(A)`` LAST. That is a second lock
        resource no ordering analysis in the feature mentions; it is currently
        safe only because the two operate on DIFFERENT address rows.

        Contention is forced by an observer barrier and only INVARIANTS are
        asserted — the order of completion is deliberately not.
        """
        admin = _admin(pgdb)
        oa, ob, oc, od = _user(pgdb), _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c, d = (
            _addr(pgdb, oa),
            _addr(pgdb, ob),
            _addr(pgdb, oc),
            _addr(pgdb, od),
        )
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 12)
        _deliver(pgdb, od, d, 4)
        pgdb.session.commit()
        scopes = [
            BottleScope.for_group(group.id),
            BottleScope.for_address(a.id),
            BottleScope.for_address(d.id),
        ]
        before = _sum_over(pgdb, scopes)
        assert before == Decimal("16.00")

        observer = raw()
        _hold_row_for_update(observer, group_id=group.id)
        probe = raw(autocommit=True)
        sink: dict = {}

        def split():
            CustomerLinkService().remove_address_from_group(
                a.id, acting_admin_id=admin.id, reason="moves out", bottles_leaving=5
            )
            return "split"

        def join():
            CustomerLinkService().add_addresses_to_group(
                group.id, [d.id], acting_admin_id=admin.id, reason="moves in"
            )
            return "join"

        t1 = _in_app(papp, split, sink, "split")
        t2 = _in_app(papp, join, sink, "join")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe, expected=2), (
                "both operations were expected to contend for the place's row"
            )
        finally:
            observer.rollback()
            t1.join(timeout=90)
            t2.join(timeout=90)
        assert not t1.is_alive() and not t2.is_alive()
        assert _reraise(sink, "split") == "split"
        assert _reraise(sink, "join") == "join", (
            "a 40P01 DeadlockDetected here means the split/join lock order is "
            "genuinely cyclical"
        )

        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id is None
        assert UserAddress.query.get(d.id).address_group_id == group.id
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("5.00"),
            Decimal("5.00"),
        )
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("11.00"),
            Decimal("11.00"),
        ), "the place's two figures must agree after a concurrent split and join"
        assert _raw_balance_rows(pgdb, address_id=d.id) == []
        assert _sum_over(pgdb, scopes) == before, "bottles were minted or destroyed"
        # Σ alone conserves under every attribution bug in this feature, so pin
        # where each bottle ended up as well.
        assert _pair(pgdb, BottleScope.for_address(d.id)) == (
            Decimal("0.00"),
            Decimal("0.00"),
        ), "the joiner's own scope must be empty on BOTH figures after the join"

    def test_two_concurrent_JOINS_into_one_place_do_not_lose_an_absorbed_balance(
        self, papp, pgdb, raw
    ):
        """Step 5 of ``_absorb_joiners_into_group`` is
        ``place_row.balance = place_row.balance + absorbed`` — a read-modify-write
        in PYTHON. It is only safe because of the FOR UPDATE taken in step 2. On
        SQLite both threads read 10 and one increment vanishes, silently."""
        admin = _admin(pgdb)
        o1, o2, od, oe = _user(pgdb), _user(pgdb), _user(pgdb), _user(pgdb)
        a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
        d, e = _addr(pgdb, od), _addr(pgdb, oe)
        group = _group(pgdb, admin, [a1, a2])
        _deliver(pgdb, o1, a1, 10)
        _deliver(pgdb, od, d, 3)
        _deliver(pgdb, oe, e, 2)
        pgdb.session.commit()
        scopes = [
            BottleScope.for_group(group.id),
            BottleScope.for_address(d.id),
            BottleScope.for_address(e.id),
        ]
        before = _sum_over(pgdb, scopes)
        assert before == Decimal("15.00")

        observer = raw()
        _hold_row_for_update(observer, group_id=group.id)
        probe = raw(autocommit=True)
        sink: dict = {}

        def join(address_id, label):
            def run():
                CustomerLinkService().add_addresses_to_group(
                    group.id, [address_id], acting_admin_id=admin.id, reason="joins"
                )
                return label

            return run

        t1 = _in_app(papp, join(d.id, "d"), sink, "d")
        t2 = _in_app(papp, join(e.id, "e"), sink, "e")
        try:
            assert _wait_until_a_backend_blocks_on_a_lock(probe, expected=2)
        finally:
            observer.rollback()
            t1.join(timeout=90)
            t2.join(timeout=90)
        assert _reraise(sink, "d") == "d"
        assert _reraise(sink, "e") == "e"

        pgdb.session.expire_all()
        stored, ledger = _pair(pgdb, BottleScope.for_group(group.id))
        assert stored == Decimal("15.00"), "one join's absorbed balance was lost"
        assert ledger == Decimal("15.00")
        assert _raw_balance_rows(pgdb, address_id=d.id) == []
        assert _raw_balance_rows(pgdb, address_id=e.id) == []
        assert _sum_over(pgdb, scopes) == before
        stray = pgdb.session.execute(
            text(
                "SELECT count(*) FROM bottle_ledger "
                " WHERE address_id = ANY(:a) AND address_group_id IS NULL"
            ),
            {"a": [d.id, e.id]},
        ).scalar()
        assert stray == 0, "a joiner's ledger rows were left in its own scope"
        # The rebuilt snapshot chain walks the merged timeline monotonically.
        chain = [
            D(r[0])
            for r in pgdb.session.execute(
                text(
                    "SELECT balance_after FROM bottle_ledger WHERE address_group_id = :g "
                    " ORDER BY occurred_at, id"
                ),
                {"g": group.id},
            ).all()
        ]
        assert chain[-1] == Decimal("15.00")
        running = Decimal("0.00")
        quantities = [
            D(r[0])
            for r in pgdb.session.execute(
                text(
                    "SELECT quantity FROM bottle_ledger WHERE address_group_id = :g "
                    " ORDER BY occurred_at, id"
                ),
                {"g": group.id},
            ).all()
        ]
        for quantity, snapshot in zip(quantities, chain):
            running += quantity
            assert snapshot == running

    # ------------------------------------------------------------------ #
    # Two concurrent removals: the `addresses` row lock nobody analysed
    # ------------------------------------------------------------------ #

    def _race_two_removals(self, papp, pgdb, raw, group, leaving_a, leaving_b, admin):
        """Two removals of one place, PROVABLY QUEUED on rung 0 — not racing.

        REWRITTEN FOR THE LADDER. The old harness forced both transactions to
        reach ``_dissolve_if_last_member`` before either finished it, because
        that method was the first thing either of them locked anything
        interesting in: the removal cleared (and flushed) its own membership
        pointer first, counted the remaining members with an UNLOCKED SELECT,
        and only then asked for ``bottle_balances(G)``. Both sides therefore
        counted against the other's invisible, uncommitted clear.

        That interleaving is now UNCONSTRUCTIBLE, and its unconstructibility IS
        the fix. ``remove_address_from_group`` takes ``address_groups(G)``
        FOR NO KEY UPDATE as its second statement, so the second admin blocks
        there — before it has read a member count, before it has written
        anything, before it has touched a bottle row. A Python barrier demanding
        both threads arrive inside ``_dissolve_if_last_member`` can never be
        satisfied, and waiting 30 s for it to break would prove nothing.

        So the shape inverts: T1 is held INSIDE ``_dissolve_if_last_member``
        (holding rung 0 and every member's ``addresses`` row) while T2 is
        launched, and T1 is not released until POSTGRES ITSELF reports a backend
        waiting on a lock. That is the assertion — T2 is queued, not racing —
        and it is what the whole redesign buys.

        Returns ``(sink, blocked)``: sink values are either the service result
        dict or the exception the thread raised, so the caller decides what is
        acceptable, and ``blocked`` is Postgres's own confirmation that the
        second removal waited.
        """
        sink: dict = {}
        probe = raw(autocommit=True)
        second: dict = {}

        def remove(address_id, label):
            def run():
                return CustomerLinkService().remove_address_from_group(
                    address_id, acting_admin_id=admin.id, reason=f"removal {label}"
                )

            return run

        def launch_the_second_removal(*_args, **_kwargs):
            second["thread"] = _in_app(papp, remove(leaving_b, "b"), sink, "b")
            second["blocked"] = _wait_until_a_backend_blocks_on_a_lock(probe)

        with _hook(
            CustomerLinkService,
            "_dissolve_if_last_member",
            before=launch_the_second_removal,
            thread_name="a",
        ):
            t1 = _in_app(papp, remove(leaving_a, "a"), sink, "a")
            t1.join(timeout=90)
            t2 = second.get("thread")
            assert t2 is not None, (
                "the first removal never reached _dissolve_if_last_member — the "
                "hook no longer names a call site on the removal path"
            )
            t2.join(timeout=90)
        assert not t1.is_alive() and not t2.is_alive(), "a removal thread hung"
        pgdb.session.expire_all()
        return sink, second.get("blocked")

    @staticmethod
    def _deadlocks(sink) -> list:
        return [
            f"{key}: {_integrity_message(value)[:200]}"
            for key, value in sink.items()
            if isinstance(value, BaseException) and _sqlstate(value) == "40P01"
        ]

    def test_two_concurrent_removals_from_a_TWO_member_place_do_not_DEADLOCK(
        self, papp, pgdb, raw
    ):
        """FIXED — the xfail is gone, and this is the confirmed 40P01 closing.

        WAS: the lifecycle took a THIRD lock resource — ``addresses`` row
        write-locks — on BOTH SIDES of the ``bottle_balances`` locks.
        ``remove_address_from_group`` wrote ``addresses(A).address_group_id =
        NULL`` and FLUSHED before any bottle row was touched, then
        ``_dissolve_if_last_member`` -> ``release_group_history_to_address`` took
        ``bottle_balances(G)`` FOR UPDATE, and only AFTER that un-pointed the
        SURVIVOR's ``addresses(B)`` row. Two removals from a two-member place
        were a textbook ABBA: T1 held addresses(A) + bottle_balances(G) and
        wanted addresses(B); T2 held addresses(B) and wanted bottle_balances(G).
        Postgres killed one side with 40P01, which reached the admin as a 500.

        NOW: ``addresses`` joins the documented ordering instead of straddling
        it. The removal takes ``address_groups(G)`` (rung 0), then the WHOLE
        member set ascending by id in one statement (rung 1) — which already
        contains both A and B — and only then the bottle rows. The survivor
        un-point is a lock UPGRADE on a row held since rung 1, so there is no
        second ``addresses`` acquisition below rung 2 and no cycle to form.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 10)

        sink, blocked = self._race_two_removals(papp, pgdb, raw, group, a.id, b.id, admin)
        assert blocked is True, (
            "the second removal never waited on a lock, so this run did not "
            "exercise the contention at all"
        )
        assert self._deadlocks(sink) == [], (
            "Postgres detected a deadlock between two ordinary place-group "
            f"removals: {self._deadlocks(sink)}"
        )

    def test_two_concurrent_removals_CONSERVE_and_the_loser_is_REFUSED_BY_NAME(
        self, papp, pgdb, raw
    ):
        """UPDATED: the loser's outcome changed from a database kill to a named refusal.

        WAS: a 40P01 was tolerated here on purpose, because the only thing
        stopping a double dissolve was one side blocking on the other's rows.
        With a ``lock_timeout`` that surfaced as ``LockNotAvailable``; production
        sets none, so the second admin's request simply HUNG for the life of the
        first transaction and then acted on reads taken before it.

        NOW: the two removals serialise on ``address_groups(G)``. The second one
        wakes, locks a member set that no longer contains its address, and is
        refused with a named ``PLACE_GROUP_NOT_FOUND`` the admin panel can
        render — no deadlock, no hang, no stale-read write. Conservation still
        holds, and that is asserted independently.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 10)
        scopes = [
            BottleScope.for_group(group.id),
            BottleScope.for_address(a.id),
            BottleScope.for_address(b.id),
        ]
        before = _sum_over(pgdb, scopes)
        assert before == Decimal("10.00")

        sink, blocked = self._race_two_removals(papp, pgdb, raw, group, a.id, b.id, admin)
        assert blocked is True

        failures = [v for v in sink.values() if isinstance(v, BaseException)]
        assert self._deadlocks(sink) == [], self._deadlocks(sink)
        for failure in failures:
            assert isinstance(failure, ValidationError), (
                f"a database-level failure reached the caller: {failure!r}"
            )
            assert failure.error_code == "PLACE_GROUP_NOT_FOUND", failure.error_code
        survivors = [v for v in sink.values() if not isinstance(v, BaseException)]
        assert survivors, "both removals failed; the race produced no outcome to check"

        assert _sum_over(pgdb, scopes) == before, (
            "the removal race minted or destroyed bottles"
        )
        for scope in scopes:
            stored, ledger = _pair(pgdb, scope)
            assert stored == ledger, (
                f"scope {scope} ended with stored {stored} but a ledger sum of "
                f"{ledger} — an unaudited balance movement"
            )

    def test_two_concurrent_removals_from_a_THREE_member_place_still_DISSOLVE(
        self, papp, pgdb, raw
    ):
        """FIXED — the xfail is gone.

        WAS: each removal counted 2 remaining (its own plus the other's
        uncommitted one), so NEITHER triggered §7.3's dissolve and a one-member
        place group survived with the pool still keyed to the group.
        ``_dissolve_if_last_member`` counted members with a plain, UNLOCKED
        SELECT and ``remove_address_from_group`` took no lock at all when
        ``bottles_leaving == 0``. This arm did NOT deadlock — with two members
        remaining neither removal reached the survivor un-point — which is
        exactly why the failure was silent.

        NOW: the count is a re-read of a member set pinned by rung 0 + rung 1.
        The second removal cannot start until the first has committed, so it
        sees ONE member remaining and dissolves.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 9)
        scopes = [
            BottleScope.for_group(group.id),
            BottleScope.for_address(a.id),
            BottleScope.for_address(b.id),
            BottleScope.for_address(c.id),
        ]
        before = _sum_over(pgdb, scopes)

        sink, blocked = self._race_two_removals(papp, pgdb, raw, group, a.id, b.id, admin)
        assert blocked is True
        _reraise(sink, "a")
        _reraise(sink, "b")

        # Conservation survives either way; the RULE does not. Σ alone is BLIND
        # to this defect — nothing is minted or destroyed — so the per-scope
        # halves are asserted too, and then the rule itself.
        assert _sum_over(pgdb, scopes) == before
        for scope in scopes:
            stored, ledger = _pair(pgdb, scope)
            assert stored == ledger, f"{scope}: stored {stored} vs ledger {ledger}"
        survivors = _members(pgdb, group.id)
        assert survivors == [], (
            f"the place was left with members {survivors} (owner "
            f"{[oa.id, ob.id, oc.id]} own {[a.id, b.id, c.id]}) and was never "
            "dissolved, so a one-member place group survives — its pool is still "
            "keyed to the group, reachable by exactly one customer"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "STILL RED, and the reason is now a PROPERTY OF THIS HARNESS rather "
            "than of the code — recorded rather than adjusted away, because a pin "
            "that stops matching its own defect must say so out loud. "
            "ORIGINALLY: `_validated_bottles_leaving` read get_place_balance() "
            "with NO lock and `_split_bottles_out_of_place` — which then DID take "
            "the group row FOR UPDATE — never re-validated the quantity under it, "
            "so a driver's return committing IN THAT WINDOW let an admin move more "
            "bottles out of a place than it held. That window is GONE: "
            "`remove_address_from_group` now takes address_groups(G), every "
            "member's addresses row and the group's bottle_balances row BEFORE "
            "`_validated_bottles_leaving` runs, so nothing can move the figure the "
            "cap is computed from. Its sibling in test_place_split_full_e2e.py "
            "(`test_the_split_must_never_leave_the_place_below_zero`) XPASSED on "
            "exactly that change. "
            "WHY THIS ONE CANNOT: the return thread here carries no lock_timeout, "
            "so instead of being cancelled it BLOCKS at rung 1 and commits STRICTLY "
            "AFTER the split. The place then ends at 1 − 5 = −4 — an ordinary "
            "OVER-RETURN by member B, not a bypassed cap. The codebase permits a "
            "negative place deliberately (`_validated_bottles_leaving` and "
            "`_coerce_resulting_balance` both say so), so `place >= 0` is no longer "
            "a proxy for `the cap held`. The property the fix DOES establish is "
            "asserted by the test directly below, which is new; this marker stays "
            "so the analysis is not lost."
        ),
    )
    def test_a_concurrent_RETURN_cannot_push_a_validated_split_past_the_cap(
        self, papp, pgdb
    ):
        """The split's cap is validated OUTSIDE the lock it later takes.

        The barrier sits at the top of ``_split_bottles_out_of_place``, i.e.
        AFTER ``_validated_bottles_leaving`` accepted 7 against a place holding 8,
        and BEFORE the group row is locked. A perfectly ordinary return of 5
        commits in that window. Nothing is minted — conservation holds — but the
        place ends NEGATIVE on a split the service promised to cap at 8.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 8)
        pgdb.session.commit()
        scopes = [BottleScope.for_group(group.id), BottleScope.for_address(a.id)]
        assert _sum_over(pgdb, scopes) == Decimal("8.00")

        barrier = threading.Barrier(2, timeout=30)
        sink: dict = {}
        with _barrier_at(CustomerLinkService, "_split_bottles_out_of_place", barrier):

            def split():
                return CustomerLinkService().remove_address_from_group(
                    a.id, acting_admin_id=admin.id, reason="takes seven", bottles_leaving=7
                )

            def late_return():
                from business_app import db as _db

                # Reaching the barrier means the split has been VALIDATED against
                # a place balance of 8 and has not yet locked the place's row.
                barrier.wait(timeout=30)
                BottleTrackingService().record_bottles_returned(
                    ob.id, b.id, Decimal("5.00")
                )
                _db.session.commit()
                return "returned"

            t1 = _in_app(papp, split, sink, "split")
            t2 = _in_app(papp, late_return, sink, "return")
            t1.join(timeout=90)
            t2.join(timeout=90)
        assert _reraise(sink, "return") == "returned"
        result = _reraise(sink, "split")

        pgdb.session.expire_all()
        # Conservation is intact either way — assert the PAIR, not one side.
        assert _sum_over(pgdb, scopes) == Decimal("3.00")
        for scope in scopes:
            stored, ledger = _pair(pgdb, scope)
            assert stored == ledger, f"{scope}: stored {stored} vs ledger {ledger}"

        place = _stored(pgdb, BottleScope.for_group(group.id))
        assert place >= 0, (
            f"the place ended at {place} after a split of "
            f"{result['bottles_leaving']} that was validated against a cap of 8 "
            "— the cap was bypassed by a concurrent return"
        )


    def test_the_split_moved_exactly_what_the_place_HELD_when_the_cap_was_read(
        self, papp, pgdb
    ):
        """THE PROPERTY THE LADDER ACTUALLY ESTABLISHES, asserted directly.

        Companion to the strict xfail above, and the reason that marker's residual
        is a harness artefact rather than an open defect. The claim is NOT "the
        place can never go negative" — it can, legitimately, whenever a member
        over-returns. The claim is that the §7.1 cap is evaluated against a figure
        NOTHING CAN MOVE UNDERNEATH IT: `remove_address_from_group` holds
        `address_groups(G)`, every member's `addresses` row and the group's
        `bottle_balances` row before `_validated_bottles_leaving` is called, so a
        concurrent return is serialised — it lands wholly before the cap read or
        wholly after the split, never in between.

        Same interleave as the xfail above, asserted on that property: the split
        moves exactly the 7 it was validated for out of a place that really did
        hold 8, and the −4 that follows is attributable to the return ALONE.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 8)
        pgdb.session.commit()
        place_scope = BottleScope.for_group(group.id)

        barrier = threading.Barrier(2, timeout=30)
        sink: dict = {}
        with _barrier_at(CustomerLinkService, "_split_bottles_out_of_place", barrier):

            def split():
                return CustomerLinkService().remove_address_from_group(
                    a.id, acting_admin_id=admin.id, reason="takes seven", bottles_leaving=7
                )

            def late_return():
                from business_app import db as _db

                barrier.wait(timeout=30)
                BottleTrackingService().record_bottles_returned(
                    ob.id, b.id, Decimal("5.00")
                )
                _db.session.commit()
                return "returned"

            t1 = _in_app(papp, split, sink, "split")
            t2 = _in_app(papp, late_return, sink, "return")
            t1.join(timeout=90)
            t2.join(timeout=90)
        assert _reraise(sink, "return") == "returned"
        result = _reraise(sink, "split")
        pgdb.session.expire_all()

        # The split moved exactly what it was validated for, out of a place that
        # really did hold 8 at that moment: the :out half is -7 and the departing
        # address ends holding exactly 7.
        assert result["bottles_leaving"] == Decimal("7.00")
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("7.00"),
            Decimal("7.00"),
        )
        out_half = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"place_leave:{group.id}:%:{a.id}:out")
        ).one()
        assert D(out_half.quantity) == Decimal("-7.00")
        assert D(out_half.balance_after) == Decimal("1.00"), (
            "the :out half was written against a place holding 8 — if the return "
            "had committed inside the cap window this snapshot would read -4.00"
        )

        # The place is negative because B over-returned AFTERWARDS, and both
        # figures agree on it — no unaudited movement anywhere.
        stored, ledger = _pair(pgdb, place_scope)
        assert (stored, ledger) == (Decimal("-4.00"), Decimal("-4.00"))
        assert _sum_over(
            pgdb, [place_scope, BottleScope.for_address(a.id)]
        ) == Decimal("3.00")


class TestTheSplitRejoinCycleNeverStrandsARow:
    def test_three_split_and_rejoin_cycles_leave_exactly_one_row_per_live_scope(
        self, papp, pgdb
    ):
        """The exact loop that produced the original "split then re-add stranded
        bottles" bug §7.2 was written to close. On Postgres a leftover row is
        FATAL on the next cycle (``uq_bottle_balance_addr`` /
        ``uq_bottle_balance_group``) rather than merely wrong, and the CHECK is
        re-verified at every step.

        THREE members, deliberately: removing one of two would leave exactly one
        and DISSOLVE the place (§7.3), so a two-address group cannot be cycled at
        all — the third member is what keeps the place alive across the loop.
        """
        admin = _admin(pgdb)
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        service = CustomerLinkService()
        expected_total = Decimal("0.00")

        for cycle in range(3):
            _deliver(pgdb, oa, a, 10)
            expected_total += Decimal("10.00")
            scopes = [BottleScope.for_group(group.id), BottleScope.for_address(a.id)]
            assert _sum_over(pgdb, scopes) == expected_total, f"cycle {cycle} after delivery"

            service.remove_address_from_group(
                a.id, acting_admin_id=admin.id, reason=f"cycle {cycle} out", bottles_leaving=4
            )
            pgdb.session.commit()
            pgdb.session.expire_all()
            assert len(_raw_balance_rows(pgdb, address_id=a.id)) == 1
            assert len(_raw_balance_rows(pgdb, group_id=group.id)) == 1
            assert _sum_over(pgdb, scopes) == expected_total, f"cycle {cycle} after split"

            service.add_addresses_to_group(
                group.id, [a.id], acting_admin_id=admin.id, reason=f"cycle {cycle} back in"
            )
            pgdb.session.commit()
            pgdb.session.expire_all()
            assert _raw_balance_rows(pgdb, address_id=a.id) == [], (
                f"cycle {cycle}: the re-join left a stranded address row — the "
                "next cycle's re-create would hit uq_bottle_balance_addr"
            )
            assert len(_raw_balance_rows(pgdb, group_id=group.id)) == 1
            stored, ledger = _pair(pgdb, BottleScope.for_group(group.id))
            assert stored == expected_total, f"cycle {cycle} after rejoin"
            assert ledger == expected_total, f"cycle {cycle} ledger after rejoin"

        # The tail used to re-assert `ck_bottle_balance_scope` itself
        # (`(address_group_id IS NULL) = (address_id IS NULL)` => 0 rows), which
        # the CHECK makes physically impossible and which is pinned directly in
        # section 1 — it could not fail. What CAN fail here is attribution: three
        # round trips must leave A's own scope completely empty on BOTH figures
        # and every bottle accounted for by the place, with no ledger row
        # stranded outside the group.
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("0.00"),
            Decimal("0.00"),
        ), "the last re-join left bottles behind in the cycled address's own scope"
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            expected_total,
            expected_total,
        )
        stranded = pgdb.session.execute(
            text(
                "SELECT count(*) FROM bottle_ledger "
                " WHERE address_id = :a AND address_group_id IS NULL"
            ),
            {"a": a.id},
        ).scalar()
        assert stranded == 0, (
            f"{stranded} of A's ledger rows are still keyed to A's own scope "
            "after the final re-join"
        )


# =========================================================================== #
# 5. NUMERIC(12,2) EXACTNESS AND THE CONVERGENCE GUARANTEE
# =========================================================================== #
#
# SQLite stores NUMERIC as a C double and hides rounding drift; Postgres returns
# real Decimals. The `quantize(cents)` dance in `_apply_merge_review` and the
# `Decimal(str(...))` conversions are only exercised faithfully here — and the
# convergence equality (`get_place_balance == SUM(bottle_ledger.quantity)`) is
# the single strongest guarantee the feature offers: if it breaks, the admin
# panel's Reconcile button destroys the admin's number.


def _merge_preview(pgclient, papp, admin, address_ids, *, group_id=None, exclude=None):
    query = f"address_ids={','.join(str(i) for i in address_ids)}"
    if group_id is not None:
        query += f"&group_id={group_id}"
    if exclude:
        query += f"&exclude={','.join(str(i) for i in exclude)}"
    response = pgclient.get(
        f"{API}/admin/place-groups/merge-preview?{query}", headers=_headers(papp, admin)
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _post_merge(pgclient, papp, admin, address_ids, **body):
    return pgclient.post(
        f"{API}/admin/place-groups",
        json={"addressIds": list(address_ids), **body},
        headers=_headers(papp, admin),
    )


def _review_entries(pgdb, group_id: int) -> list[tuple]:
    """The review's own appended rows, in write order: (source, quantity, key)."""
    rows = pgdb.session.execute(
        text(
            "SELECT entry_metadata ->> 'source', quantity, idempotency_key "
            "  FROM bottle_ledger "
            " WHERE address_group_id = :g "
            "   AND entry_metadata ->> 'source' IN "
            "       ('merge_backfill', 'merge_exclude', 'merge_correction') "
            " ORDER BY id"
        ),
        {"g": group_id},
    ).all()
    return [(r[0], D(r[1]), r[2]) for r in rows]


class TestTheMergeReviewConvergesExactlyOnPostgresNumeric:
    def test_the_address_24_shape_converges_with_a_POSITIVE_backfill(
        self, papp, pgdb, pgclient
    ):
        """Dev address 24 reproduced through real paths: stored 20.00, ZERO
        ledger rows. The backfill must be +20.00, SIGNED, and BALANCE-DECOUPLED
        — the group's stored figure must not move on it — and afterwards the two
        figures must be the SAME Decimal, not merely close."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        carried = _adjust(pgdb, admin, a, 20, notes="figure carried from before the ledger")
        _drop_ledger_row(pgdb, carried.id)
        _deliver(pgdb, o2, b, 6)
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("20.00"),
            Decimal("0.00"),
        )

        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id])
        assert preview["stored_balance"] == 26.0
        assert preview["computed_balance"] == 6.0
        assert preview["drift"] == 20.0
        assert preview["projected_place_balance"] == 26.0

        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="one office; the ledger never had the opening balance",
            previewEntryIds=preview["entry_ids"],
            resultingBalance=preview["projected_place_balance"],
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]

        appended = _review_entries(pgdb, group_id)
        assert [(source, quantity) for source, quantity, _key in appended] == [
            ("merge_backfill", Decimal("20.00"))
        ], f"expected exactly one +20 backfill and no correction: {appended}"

        stored, ledger = _pair(pgdb, BottleScope.for_group(group_id))
        assert stored == Decimal("26.00")
        assert ledger == Decimal("26.00")
        assert stored == ledger, "THE convergence guarantee"
        # Read straight from SQL too: the ORM is not the thing under test.
        raw_stored, raw_ledger = pgdb.session.execute(
            text(
                "SELECT (SELECT balance FROM bottle_balances WHERE address_group_id = :g), "
                "       (SELECT COALESCE(SUM(quantity), 0) FROM bottle_ledger "
                "         WHERE address_group_id = :g)"
            ),
            {"g": group_id},
        ).one()
        assert raw_stored == raw_ledger == Decimal("26.00")

    def test_a_NEGATIVE_drift_writes_a_NEGATIVE_backfill_and_still_converges(
        self, papp, pgdb, pgclient
    ):
        """Both directions are claimed real but only the positive one is produced
        naturally by the address-24 shape. A sign error, or notes/metadata that
        assume a positive drift, would only ever be caught by this case."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        _deliver(pgdb, o1, a, 10)
        surplus = _adjust(pgdb, admin, a, -6, notes="the ledger over-recorded")
        _drop_ledger_row(pgdb, surplus.id)
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("4.00"),
            Decimal("10.00"),
        )

        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id])
        assert preview["drift"] == -6.0
        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="retire a surplus the ledger over-recorded",
            previewEntryIds=preview["entry_ids"],
            resultingBalance=preview["projected_place_balance"],
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]

        appended = _review_entries(pgdb, group_id)
        assert [(s, q) for s, q, _k in appended] == [
            ("merge_backfill", Decimal("-6.00"))
        ], appended
        stored, ledger = _pair(pgdb, BottleScope.for_group(group_id))
        assert (stored, ledger) == (Decimal("4.00"), Decimal("4.00"))
        # Sign-neutral note (it is written for BOTH directions).
        notes = pgdb.session.execute(
            text(
                "SELECT notes FROM bottle_ledger WHERE idempotency_key = :k"
            ),
            {"k": appended[0][2]},
        ).scalar()
        assert "aligned to the balance the place carries" in notes

    def test_exclusions_and_an_override_compose_in_the_FIXED_order_and_converge(
        self, papp, pgdb, pgclient
    ):
        """Order on Postgres: ONE ``merge_backfill`` (decoupled), then one
        ``merge_exclude`` per excluded id (each a reversing ``-quantity``,
        COUPLED), then ONE ``merge_correction`` measured against
        ``stored_before - excluded_total``.

        Measuring the override against ``computed_balance - excluded_total``
        instead is the documented old bug (stating 10 gave 15). On drifted
        production data the two bases differ, and only real NUMERIC arithmetic on
        a genuinely drifted place distinguishes them.
        """
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        _deliver(pgdb, o1, a, 6)
        _deliver(pgdb, o2, b, 5)
        refund = _give_back(pgdb, o2, b, 4)
        carried = _adjust(pgdb, admin, a, 3, notes="carried figure")
        _drop_ledger_row(pgdb, carried.id)
        # stored 10 (6 + 5 - 4 + 3), ledger 7 (6 + 5 - 4) => drift 3
        assert _stored(pgdb, BottleScope.for_address(a.id)) + _stored(
            pgdb, BottleScope.for_address(b.id)
        ) == Decimal("10.00")

        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id], exclude=[refund.id])
        assert preview["drift"] == 3.0
        assert preview["excluded_total"] == -4.0
        assert preview["projected_place_balance"] == 14.0

        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="that return was somebody else's; we counted twelve",
            previewEntryIds=preview["entry_ids"],
            excludedLedgerEntryIds=[refund.id],
            resultingBalance=12,
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]

        appended = _review_entries(pgdb, group_id)
        assert [(s, q) for s, q, _k in appended] == [
            ("merge_backfill", Decimal("3.00")),
            ("merge_exclude", Decimal("4.00")),
            ("merge_correction", Decimal("-2.00")),
        ], f"wrong entries or wrong order: {appended}"

        stored, ledger = _pair(pgdb, BottleScope.for_group(group_id))
        assert stored == Decimal("12.00"), "the place must hold exactly the stated number"
        assert ledger == Decimal("12.00"), "and its ledger must sum to the same number"
        # The original entry was NOT rewritten — only reversed.
        assert D(BottleLedger.query.get(refund.id).quantity) == Decimal("-4.00")

    def test_fractional_quantities_survive_NUMERIC_12_2_exactly_through_a_review(
        self, papp, pgdb, pgclient
    ):
        """0.33 + 1.50 - 0.01 is where a float backend hides its rounding.

        ``_coerce_resulting_balance`` -> ``Decimal(str(value))`` on a JSON float
        is the one place a float can enter the pipeline, so the override is sent
        as the STRING '7.77' and as a bare float in two separate assertions
        below.
        """
        from business_app.services.bottle_tracking_service import format_bottle_quantity

        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        _deliver(pgdb, o1, a, "0.33")
        _deliver(pgdb, o2, b, "1.50")
        crumb = _give_back(pgdb, o2, b, "0.01")

        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id], exclude=[crumb.id])
        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="counted seven and three quarters plus change",
            previewEntryIds=preview["entry_ids"],
            excludedLedgerEntryIds=[crumb.id],
            resultingBalance="7.77",
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]

        stored, ledger = _pair(pgdb, BottleScope.for_group(group_id))
        assert stored == Decimal("7.77")
        assert ledger == Decimal("7.77")
        assert str(stored) == "7.77", f"binary float noise reached the column: {stored!r}"
        assert format_bottle_quantity(stored) == "7.77"
        # Every persisted quantity is an exact cent-scale Decimal.
        quantities = [
            r[0]
            for r in pgdb.session.execute(
                text("SELECT quantity FROM bottle_ledger WHERE address_group_id = :g"),
                {"g": group_id},
            ).all()
        ]
        assert all(q == q.quantize(CENT) for q in quantities), quantities

        # The other half this docstring promises, which was missing: the SAME
        # number as a BARE JSON FLOAT. `_coerce_resulting_balance`'s
        # `Decimal(str(value))` is the one line standing between the float 7.77
        # and 7.7699999999999996 reaching a NUMERIC(12,2) column; sending only
        # the string variant never exercises it.
        o3, o4 = _user(pgdb), _user(pgdb)
        c, e = _addr(pgdb, o3), _addr(pgdb, o4)
        _deliver(pgdb, o3, c, "0.33")
        _deliver(pgdb, o4, e, "1.50")
        float_preview = _merge_preview(pgclient, papp, admin, [c.id, e.id])
        float_response = _post_merge(
            pgclient,
            papp,
            admin,
            [c.id, e.id],
            reason="the same count, stated as a float",
            previewEntryIds=float_preview["entry_ids"],
            resultingBalance=7.77,
        )
        assert float_response.status_code == 201, float_response.get_json()
        float_group_id = float_response.get_json()["data"]["place_group_id"]
        float_stored, float_ledger = _pair(pgdb, BottleScope.for_group(float_group_id))
        assert (float_stored, float_ledger) == (Decimal("7.77"), Decimal("7.77"))
        assert str(float_stored) == "7.77", (
            f"binary float noise reached the column: {float_stored!r}"
        )
        assert format_bottle_quantity(float_stored) == "7.77"

    def test_an_out_of_range_resultingBalance_is_a_400_not_a_500(
        self, papp, pgdb, pgclient
    ):
        """WAS a strict xfail. `_coerce_resulting_balance` still range-checks
        nothing about the SIGN — a place may legitimately be negative — but it
        now bounds the MAGNITUDE at the column's own scale/precision, so a
        stated 10^14 is refused as a 400 by the service instead of dying at
        NUMERIC(12,2) as a DataError and coming back through the routes' bare
        `except Exception` as a 500. Invisible on SQLite, which has no numeric
        bound at all — this is why it lives on `pg_app`/`pg_db`."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        _deliver(pgdb, o1, a, 3)
        groups_before = AddressGroup.query.count()

        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id])
        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="fat finger",
            previewEntryIds=preview["entry_ids"],
            resultingBalance=99999999999999,
        )
        # Whatever the status, nothing may have been committed.
        pgdb.session.rollback()
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id is None
        assert UserAddress.query.get(b.id).address_group_id is None
        assert AddressGroup.query.count() == groups_before
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("3.00"),
            Decimal("3.00"),
        )
        assert response.status_code == 400, (
            f"a client-supplied out-of-range number returned "
            f"{response.status_code}: {response.get_json()}"
        )


    def test_a_STALE_preview_leaves_no_committed_AddressGroup_behind(
        self, papp, pgdb, pgclient
    ):
        """The staleness guard runs before the FIRST write specifically so a
        rejection leaves no flushed ``AddressGroup`` for the next commit on the
        session to adopt. Under Postgres a leftover flushed row would be a REAL,
        COMMITTED group; on SQLite it may simply vanish with the in-memory
        teardown and never be noticed. Verified with raw SQL, not just the status
        code."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        _deliver(pgdb, o1, a, 6)
        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id])

        groups_before = pgdb.session.execute(
            text("SELECT count(*) FROM address_groups")
        ).scalar()
        ledger_before = pgdb.session.execute(
            text("SELECT count(*) FROM bottle_ledger")
        ).scalar()
        events_before = pgdb.session.execute(
            text("SELECT count(*) FROM customer_link_events")
        ).scalar()

        # A delivery commits between the preview and the join.
        _deliver(pgdb, o2, b, 2)

        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="counted eight",
            previewEntryIds=preview["entry_ids"],
            resultingBalance=8,
        )
        assert response.status_code == 400, response.get_json()
        assert "MERGE_PREVIEW_STALE" in str(response.get_json())

        pgdb.session.rollback()
        pgdb.session.expire_all()
        assert (
            pgdb.session.execute(text("SELECT count(*) FROM address_groups")).scalar()
            == groups_before
        ), "a rejected merge committed an AddressGroup row"
        assert (
            pgdb.session.execute(text("SELECT count(*) FROM bottle_ledger")).scalar()
            == ledger_before + 1
        ), "only the intervening delivery's own entry may exist"
        assert (
            pgdb.session.execute(text("SELECT count(*) FROM customer_link_events")).scalar()
            == events_before
        )
        assert UserAddress.query.get(a.id).address_group_id is None
        assert UserAddress.query.get(b.id).address_group_id is None

    def test_a_delivery_AFTER_a_reviewed_merge_keeps_the_two_figures_together(
        self, papp, pgdb, pgclient
    ):
        """``record_bottles_delivered`` resolves the scope from the address at
        WRITE time. If an in-flight order captured the pre-join scope anywhere (a
        cached ``BottleScope``, a stored ``address_group_id``), the bottles land
        in the wrong scope and the convergence guarantee breaks on the very next
        write."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        carried = _adjust(pgdb, admin, a, 20, notes="carried figure")
        _drop_ledger_row(pgdb, carried.id)
        _deliver(pgdb, o2, b, 6)
        # An order already out for delivery at A, not yet bottle-recorded.
        in_flight = _order(pgdb, o1, a, status=OrderStatus.OUT_FOR_DELIVERY)

        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id])
        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="align and merge",
            previewEntryIds=preview["entry_ids"],
            resultingBalance=preview["projected_place_balance"],
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]
        assert _pair(pgdb, BottleScope.for_group(group_id)) == (
            Decimal("26.00"),
            Decimal("26.00"),
        )

        entry = BottleTrackingService().record_bottles_delivered(
            in_flight.id, o1.id, a.id, Decimal("2.50")
        )
        pgdb.session.commit()
        pgdb.session.expire_all()
        assert entry.address_group_id == group_id, (
            "the delivery landed outside the place it was delivered to"
        )
        stored, ledger = _pair(pgdb, BottleScope.for_group(group_id))
        assert (stored, ledger) == (Decimal("28.50"), Decimal("28.50"))
        assert D(entry.balance_after) == Decimal("28.50")


class TestReconcileBalanceAgainstAPlace:
    def test_reconcile_is_a_NO_OP_on_a_place_that_went_through_a_reviewed_merge(
        self, papp, pgdb, pgclient
    ):
        """The payoff the decoupled backfill exists for. If the backfill were
        ever made balance-COUPLED again (the documented previous attempt), the
        ledger would read -8 while the balance read 12 and this live admin route
        would silently set the balance to -8."""
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        carried = _adjust(pgdb, admin, a, 20, notes="carried figure")
        _drop_ledger_row(pgdb, carried.id)
        _deliver(pgdb, o2, b, 6)
        preview = _merge_preview(pgclient, papp, admin, [a.id, b.id])
        response = _post_merge(
            pgclient,
            papp,
            admin,
            [a.id, b.id],
            reason="align the ledger",
            previewEntryIds=preview["entry_ids"],
            resultingBalance=preview["projected_place_balance"],
        )
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]
        ledger_rows_before = pgdb.session.execute(
            text("SELECT count(*) FROM bottle_ledger WHERE address_group_id = :g"),
            {"g": group_id},
        ).scalar()

        result = pgclient.post(
            f"{API}/admin/bottles/reconcile/{a.id}", headers=_headers(papp, admin)
        )
        assert result.status_code == 200, result.get_json()
        data = result.get_json()["data"]
        assert data["discrepancy"] == 0.0
        assert data["corrected"] is False
        assert data["address_group_id"] == group_id

        pgdb.session.expire_all()
        assert _pair(pgdb, BottleScope.for_group(group_id)) == (
            Decimal("26.00"),
            Decimal("26.00"),
        )
        assert (
            pgdb.session.execute(
                text("SELECT count(*) FROM bottle_ledger WHERE address_group_id = :g"),
                {"g": group_id},
            ).scalar()
            == ledger_rows_before
        ), "reconcile must not append a ledger entry"

    def test_reconcile_DESTROYS_a_drifted_un_reviewed_place_and_audits_NOTHING(
        self, papp, pgdb, pgclient
    ):
        """Pinned DELIBERATELY. ``reconcile_balance`` is destructive by
        construction — it assigns ``balance = ledger_sum``, writes no ledger entry
        and only logs a warning — is called by no Plan C code, and is exposed at a
        live admin route next to the merge review. This is the ONE place in the
        feature where bottles are destroyed, and pinning it makes any future
        change to it a deliberate decision rather than an accident."""
        admin = _admin(pgdb)
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        carried = _adjust(pgdb, admin, a, 20, notes="carried figure")
        _drop_ledger_row(pgdb, carried.id)
        scope = BottleScope.for_address(a.id)
        assert _pair(pgdb, scope) == (Decimal("20.00"), Decimal("0.00"))
        rows_before = pgdb.session.execute(
            text("SELECT count(*) FROM bottle_ledger WHERE address_id = :a"), {"a": a.id}
        ).scalar()

        response = pgclient.post(
            f"{API}/admin/bottles/reconcile/{a.id}", headers=_headers(papp, admin)
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["previous_balance"] == 20.0
        assert data["recalculated_balance"] == 0.0
        assert data["discrepancy"] == 20.0
        assert data["corrected"] is True

        pgdb.session.expire_all()
        assert _pair(pgdb, scope) == (Decimal("0.00"), Decimal("0.00")), (
            "twenty bottles were destroyed — that is what this route does"
        )
        assert (
            pgdb.session.execute(
                text("SELECT count(*) FROM bottle_ledger WHERE address_id = :a"),
                {"a": a.id},
            ).scalar()
            == rows_before
        ), "and NOT ONE ledger entry records the loss"


class TestARePopulatedDissolvedGroupIsAReachableTrap:
    """UPDATED: the trap is CLOSED, and this class is now its proof.

    ``add_addresses_to_group`` used to have no minimum member count and no "this
    group was dissolved" check, and ``POST /admin/place-groups/<id>/addresses``
    is a live route. A dissolved group's scope permanently carries departed
    members' entries (the FK makes that unavoidable), so re-attaching a live
    place to that scope was the one interaction the design never considered.

    It now refuses with ``PLACE_GROUP_DISSOLVED``, evaluated while holding
    ``address_groups(G)`` FOR NO KEY UPDATE (rung 0) — so unlike an unlocked
    existence check it is not a TOCTOU. It prevents NEW exposure; it does not
    un-mix a group already re-populated before the fix, which needs a data audit.
    The structural answer is an incarnation/epoch column on ``address_groups``,
    a MIGRATION, flagged as an owner decision.
    """

    def _dissolved_group_and_a_would_be_new_tenant(self, pgdb, admin, pgclient, papp):
        """Dissolve a DRIFTED place, then try to add a fresh address back to it.

        The drift matters: the group's residual ledger sum after a dissolve is
        ``ledger_before - place_total``, so it is non-zero exactly when the place
        was drifted — which dev address 24 proves is an ordinary production state.
        """
        oa, ob, oc = _user(pgdb), _user(pgdb), _user(pgdb)
        a, b, c = _addr(pgdb, oa), _addr(pgdb, ob), _addr(pgdb, oc)
        group = _group(pgdb, admin, [a, b, c])
        _deliver(pgdb, oa, a, 6)
        _deliver(pgdb, ob, b, 5)
        carried = _adjust(pgdb, admin, a, 4, notes="carried figure")
        _drop_ledger_row(pgdb, carried.id)
        stored, ledger = _pair(pgdb, BottleScope.for_group(group.id))
        assert (stored, ledger) == (Decimal("15.00"), Decimal("11.00"))

        service = CustomerLinkService()
        service.remove_address_from_group(b.id, acting_admin_id=admin.id, reason="left")
        pgdb.session.commit()
        service.remove_address_from_group(c.id, acting_admin_id=admin.id, reason="left too")
        pgdb.session.commit()
        assert _members(pgdb, group.id) == []
        residual = _ledger_sum(pgdb, BottleScope.for_group(group.id))
        assert residual == Decimal("-4.00"), (
            f"setup expected a -4 residual in the dissolved group's scope, got {residual}"
        )

        od = _user(pgdb)
        d = _addr(pgdb, od)
        _deliver(pgdb, od, d, 2)
        response = pgclient.post(
            f"{API}/admin/place-groups/{group.id}/addresses",
            json={"addressIds": [d.id], "reason": "new tenant at the same door"},
            headers=_headers(papp, admin),
        )
        assert response.status_code == 400, response.get_json()
        assert _error_code(response.get_json()) == "PLACE_GROUP_DISSOLVED", response.get_json()
        pgdb.session.expire_all()
        # TWO strangers remain inside the DEAD group's scope, by two different
        # mechanisms, and both are why re-tenanting the id can never be safe:
        #   `early`    — B departed while the place was alive, so B's own
        #                DELIVERY row stayed stamped with the group (§7.1);
        #   `survivor` — A was the last member out, so A's OWN rows were
        #                re-stamped away, but the dissolve's `:out` half was
        #                written INTO the group scope attributed to A.
        return group, d, od, {"early": (ob, b), "survivor": (oa, a)}

    def test_a_new_tenant_does_not_inherit_a_STRANGERS_residual(
        self, papp, pgdb, pgclient
    ):
        """FIXED — the xfail is gone.

        WAS: nothing prevented re-populating a DISSOLVED group id through the
        live route, and the group's ledger scope permanently carries departed
        members' entries by design. The re-added address's place started life
        with a stranger's residual already in scope — stored 2.00 against a
        ledger sum of -2.00 — and `build_merge_preview` /
        `recompute_balance_after` computed over it too.
        """
        admin = _admin(pgdb)
        group, d, _od, _departed = self._dissolved_group_and_a_would_be_new_tenant(
            pgdb, admin, pgclient, papp
        )
        # The would-be tenant kept its OWN scope, which agrees with itself.
        assert UserAddress.query.get(d.id).address_group_id is None
        assert _pair(pgdb, BottleScope.for_address(d.id)) == (
            Decimal("2.00"),
            Decimal("2.00"),
        )
        # The dead group's residual is still anchored where §7.1/§7.3 put it, and
        # no live address resolves to it.
        assert _members(pgdb, group.id) == []
        assert _raw_balance_rows(pgdb, group_id=group.id) == []
        assert _ledger_sum(pgdb, BottleScope.for_group(group.id)) == Decimal("-4.00")

    def test_reconcile_for_the_new_tenant_is_a_NO_OP(self, papp, pgdb, pgclient):
        """UPDATED: every figure below changed.

        This used to pin the consequence exactly: the live Reconcile route wrote
        the departed members' residual onto the NEW tenant's balance —
        `previous_balance 2.0, recalculated_balance -2.0, discrepancy 4.0,
        corrected True`, and the place ended at -2.00. The tenant never joins the
        dead group any more, so Reconcile sees only their own two bottles.
        """
        admin = _admin(pgdb)
        group, d, _od, _departed = self._dissolved_group_and_a_would_be_new_tenant(
            pgdb, admin, pgclient, papp
        )
        assert _stored(pgdb, BottleScope.for_address(d.id)) == Decimal("2.00")

        response = pgclient.post(
            f"{API}/admin/bottles/reconcile/{d.id}", headers=_headers(papp, admin)
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["previous_balance"] == 2.0
        assert data["recalculated_balance"] == 2.0
        assert data["discrepancy"] == 0.0
        assert data["corrected"] is False
        pgdb.session.expire_all()
        assert _stored(pgdb, BottleScope.for_address(d.id)) == Decimal("2.00")
        assert _raw_balance_rows(pgdb, group_id=group.id) == []

    def test_a_new_tenant_does_not_see_a_departed_customers_history(
        self, papp, pgdb, pgclient
    ):
        """FIXED — the xfail is gone. This was the cross-customer DATA EXPOSURE.

        WAS: `get_place_ledger` filters on `address_group_id` alone — correct for
        a live place, wrong for a REUSED group id. After a dissolved group was
        re-populated through the live route, the new member's place ledger (and
        the customer-facing bottle history built from it) listed a DEPARTED,
        unrelated customer's deliveries. `serialize_customer_place_ledger_entry`
        only redacts `member_name` for merge_correction/merge_backfill sources,
        so an ordinary DELIVERY row kept the stranger's name.

        The filter is unchanged and still correct; what changed is that a group
        id can no longer denote two tenancies.

        Driven through the REAL customer route with the NEW tenant's own JWT.

        Not `get_place_ledger` directly: the claim is that a customer SEES a
        stranger's deliveries, and everything between the query and the screen
        (`can_view_address_history`, `serialize_customer_place_ledger_entry`'s
        redaction whitelist) is part of whether that is true. The Telegram bot
        renders exactly this payload.
        """
        admin = _admin(pgdb)
        group, d, od, departed = self._dissolved_group_and_a_would_be_new_tenant(
            pgdb, admin, pgclient, papp
        )
        early_owner, early_address = departed["early"]
        survivor_owner, survivor_address = departed["survivor"]

        # The gate opens legitimately: `d` is the viewer's OWN address. Nothing
        # is bypassed — the place scope simply hands them the wrong place.
        assert CustomerLinkService().can_view_address_history(od.id, d.id) is True

        response = pgclient.get(
            f"{API}/orders/bottles/my-ledger/{d.id}?per_page=50", headers=_headers(papp, od)
        )
        assert response.status_code == 200, response.get_json()
        items = response.get_json()["data"]["items"]
        assert items, "the route returned nothing at all — the setup is not exercising it"

        own_address_ids = {
            r[0]
            for r in pgdb.session.execute(
                text("SELECT id FROM addresses WHERE user_id = :u"), {"u": od.id}
            ).all()
        }
        foreign = [item for item in items if item["address_id"] not in own_address_ids]
        # Characterise precisely WHOSE data reached WHOM before failing.
        exposure = [
            {
                "address_id": item["address_id"],
                "belongs_to": (
                    early_owner.id
                    if item["address_id"] == early_address.id
                    else survivor_owner.id
                    if item["address_id"] == survivor_address.id
                    else None
                ),
                "member_name": item["member_name"],
                "event_type": item["event_type"],
                "quantity": item["quantity"],
                "order_number": item["order_number"],
                "occurred_at": item["occurred_at"],
            }
            for item in foreign
        ]
        assert foreign == [], (
            f"customer {od.id} (address {d.id}) was served {len(foreign)} ledger "
            f"rows belonging to customers who left this place before they ever "
            f"joined it: {exposure}. Departed member {early_owner.id} at address "
            f"{early_address.id} and last-member-out {survivor_owner.id} at "
            f"{survivor_address.id} both remain inside group {group.id}'s scope."
        )


class TestOrderDeletionVersusTheBottleLedger:
    def test_deleting_a_delivered_order_does_not_silently_drift_the_place(
        self, papp, pgdb
    ):
        """`OrderDeletionService` builds its plan by REFLECTING FK metadata, so
        `bottle_ledger.order_id` makes bottle entries children of the order and
        they are deleted with it — while `bottle_balances` (no FK to orders) is
        left untouched.

        This used to pin the damage: the place kept holding 6.00 with a ledger
        that summed to 0.00, a permanent stored-vs-ledger divergence created
        OUTSIDE the coupled/decoupled discipline entirely, which
        `POST /admin/bottles/reconcile/<addr>` then "repaired" by overwriting the
        customer's real balance.

        `execute_deletion_plan` now REVERSES the plan's bottle-ledger rows
        through `_create_ledger_entry` first (`_plan_with_bottle_ledger_reversed`
        / `_reverse_bottle_ledger_rows`), so the stored balance moves with them,
        under the place lock, as an audited entry — and the reversal carries the
        same `order_id`/`delivery_id` as the rows it cancels, so the rebuilt plan
        sweeps the pair away together. Stored and ledger therefore both land on
        0.00 rather than merely staying equal by accident.
        """
        from business_app.services.order_deletion_service import OrderDeletionService

        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, o1), _addr(pgdb, o2)
        group = _group(pgdb, admin, [a, b])
        order = _order(pgdb, o1, a)
        BottleTrackingService().record_bottles_delivered(
            order.id, o1.id, a.id, Decimal("6.00")
        )
        pgdb.session.commit()
        scope = BottleScope.for_group(group.id)
        assert _pair(pgdb, scope) == (Decimal("6.00"), Decimal("6.00"))
        order_number = order.order_number

        result = OrderDeletionService().delete_order_by_number(
            order_number, apply_changes=True
        )
        assert result["applied"] is True
        # TWO rows, not one: the original DELIVERY entry and the reversal minted
        # to cancel it. Both are stamped with this order, so both go.
        assert result["deleted_rows_by_table"].get("bottle_ledger", 0) == 2, (
            "expected the original bottle_ledger row AND its reversal to be swept "
            f"away together; got {result['deleted_rows_by_table'].get('bottle_ledger', 0)}"
        )
        pgdb.session.expire_all()
        stored, ledger = _pair(pgdb, scope)
        assert stored == ledger, (
            f"deleting the order left the place holding {stored} with a ledger sum "
            f"of {ledger} — a permanent, unaudited drift"
        )
        # And they agree on the RIGHT figure: the 6.00 the deleted order put
        # there is gone from both sides, not stranded on one.
        assert stored == Decimal("0.00"), (
            f"the place still holds {stored} bottles from an order that no longer "
            "exists"
        )


class TestGetOrCreateBalanceUnderAStricterIsolationLevel:
    """The mapped suspicion — "get_or_create_balance can return None and its own
    guard cannot see it" — is NOT reachable on Postgres, and this is the proof.

    The worry was: after ``pg_insert(...).on_conflict_do_nothing``, the
    re-``SELECT ... FOR UPDATE`` may find nothing under a snapshot that predates
    a concurrent commit, ``assert_scope_row_valid(None)`` short-circuits on None,
    and the caller then crashes on ``balance.balance``.

    What Postgres actually does is refuse the INSERT itself with
    ``SerializationFailure`` (40001) — a retryable, LOUD error raised BEFORE the
    re-select is ever reached. So the None path cannot be entered by this route:
    either the insert succeeds (and a transaction always sees its own insert) or
    it aborts. The residual risk is therefore a RETRY-POLICY question, not a
    silent-corruption one, and this test pins that distinction so a future move
    to a stricter isolation level is evaluated on the right terms.
    """

    def test_a_create_race_under_REPEATABLE_READ_fails_LOUDLY_not_as_a_None(
        self, papp, pgdb
    ):
        admin = _admin(pgdb)
        o1, o2 = _user(pgdb), _user(pgdb)
        a1, a2 = _addr(pgdb, o1), _addr(pgdb, o2)
        group = _group(pgdb, admin, [a1, a2])
        order = _order(pgdb, o1, a1)
        pgdb.session.commit()
        assert _raw_balance_rows(pgdb, group_id=group.id) == []

        # An older snapshot, taken BEFORE the row exists. Set through SQLAlchemy's
        # own execution option on a session with no open transaction, so it is
        # reset when the connection returns to the pool.
        pgdb.session.commit()
        pgdb.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        pgdb.session.execute(text("SELECT count(*) FROM bottle_balances")).scalar()

        # A concurrent session creates the place's row through a real delivery.
        sink: dict = {}

        def deliver():
            from business_app import db as _db

            BottleTrackingService().record_bottles_delivered(
                order.id, o1.id, a1.id, Decimal("4.00")
            )
            _db.session.commit()
            return "delivered"

        thread = _in_app(papp, deliver, sink, "deliver")
        thread.join(timeout=60)
        assert _reraise(sink, "deliver") == "delivered"

        with pytest.raises(DBAPIError) as exc_info:
            # Rung 1 first — see `test_get_or_create_balance_called_TWICE...`.
            BottleTrackingService.resolve_scope_for_write(a1.id)
            BottleTrackingService.get_or_create_balance(
                a1.id, scope=BottleScope.for_group(group.id)
            )
        assert _sqlstate(exc_info.value) == "40001", (
            "expected a serialization failure; anything else (especially a None "
            "return) means assert_scope_row_valid's `if balance is None: return` "
            f"short-circuit is now reachable. Got {_sqlstate(exc_info.value)}"
        )
        pgdb.session.rollback()

        # And after a rollback the same call succeeds on a fresh snapshot — the
        # error really was retryable, not corrupting.
        #
        # Rung 1 must be RE-TAKEN, and that is not test bookkeeping: the ROLLBACK
        # released every row lock this transaction held, so the scope-lock
        # registry is cleared on `after_rollback` and the retry is refused by
        # name if it tries to write under a lock it no longer has. That is the
        # same mechanism that turns `atomic_transaction`'s non-nesting hazard
        # (an inner @transactional commits its caller's work and silently drops
        # the caller's locks) from an invisible hole into a red test.
        BottleTrackingService.resolve_scope_for_write(a1.id)
        row = BottleTrackingService.get_or_create_balance(
            a1.id, scope=BottleScope.for_group(group.id)
        )
        assert row is not None
        assert D(row.balance) == Decimal("4.00")
        pgdb.session.rollback()


# =========================================================================== #
# 11. THE UNLOCKED `resolve_scope` READ — ONE defect, three costumes
# =========================================================================== #
#
# `resolve_scope` reads `user_addresses` with NO lock, the caller then takes the
# BALANCE row FOR UPDATE, and NOTHING re-validates the scope once the lock is
# held. Every scenario below satisfies global conservation AND, per place,
# `get_place_balance == ledger_sum`. That is exactly why the ~1290 tests in the
# fast suite are blind to them: the damage is not that bottles vanish, it is
# that they become UNREACHABLE — held by a scope no live address resolves to.
#
# Each test therefore asserts PER-SCOPE ATTRIBUTION: which scope holds the
# bottles, which scope the ledger rows are stamped to, and whether any address
# can still reach them. A Sigma over the scopes is reported alongside, precisely
# to show it is UNCHANGED while the bottles are lost to every reader.
#
# SQLite cannot host any of this: `with_for_update()` compiles to nothing there,
# so there is no lock to block on and no window to interleave.
# --------------------------------------------------------------------------- #


@contextmanager
def _hook(cls, name, *, before=None, after=None, thread_name=None, once=True):
    """Run ``before`` / ``after`` around ONE named thread's calls of ``cls.name``.

    The deterministic alternative to a sleep. A barrier proves both parties
    ARRIVED somewhere; it cannot express "T2 must be parked at this exact line
    while T1 runs to completion and commits", which is what every interleave
    below needs. A one-shot callback at a named production call site can, and it
    names the line in the source that the race turns on.

    ``thread_name`` scopes the hook to a single ``_in_app`` thread, so the other
    party's calls of the very same method run untouched — several of these race
    a service method against ITSELF.

    ``once`` makes it fire for the first matching call only: a callback that
    parks a thread must never park it twice, and a hook that re-arms silently
    turns a deterministic interleave back into a timing race.

    The DESCRIPTOR KIND is preserved. ``_barrier_at`` above always re-installs
    its wrapper as a ``staticmethod``, which is correct for the static bottle
    helpers it was written for and silently WRONG for an instance method:
    ``self`` is then never passed, and the call dies with a "missing positional
    argument" that looks like a signature change in production code. Reading the
    attribute statically and restoring the same kind makes this usable on both.
    """
    import inspect

    raw_attr = inspect.getattr_static(cls, name)
    is_static = isinstance(raw_attr, staticmethod)
    original = raw_attr.__func__ if is_static else raw_attr
    state = {"fired": False}
    guard = threading.Lock()

    def _claim() -> bool:
        if thread_name is not None and threading.current_thread().name != thread_name:
            return False
        if not once:
            return True
        with guard:
            if state["fired"]:
                return False
            state["fired"] = True
            return True

    def wrapper(*args, **kwargs):
        mine = _claim()
        if mine and before is not None:
            before(*args, **kwargs)
        result = original(*args, **kwargs)
        if mine and after is not None:
            after(result, *args, **kwargs)
        return result

    setattr(cls, name, staticmethod(wrapper) if is_static else wrapper)
    try:
        yield
    finally:
        setattr(cls, name, raw_attr)


def _sweep(papp):
    """The nightly Celery invariant sweep, run in-process.

    Imported lazily: `business_app.tasks.customer_link_tasks` pulls in Celery's
    `shared_task`, and this module must not pay that at collection time.
    """
    from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants

    return reconcile_customer_link_invariants()


class TestAScopeResolvedBeforeTheLockIsNeverRevalidated:
    """Costume 1: a group-scoped delivery blocks on a removal that DISSOLVES the
    place, and the delivery lands in the memberless group.

    The highest-volume write path in the system (a driver marking a delivery
    delivered) against the most ordinary admin action (removing an address from
    a shared place). Both are correct in isolation; together they resurrect the
    exact `orphaned_place_balances` violation that spec §7.3's dissolve was
    written to eliminate.
    """

    def _race_a_group_scoped_delivery_against_the_dissolve(self, papp, pgdb, raw):
        """Park a delivery between `resolve_scope` and the FOR UPDATE, dissolve
        the place underneath it, then let it finish.

        Returns ``(group, address_a, address_b, late_order_id, ledger_id)``.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 5)
        late_order = _order(pgdb, oa, a)
        pgdb.session.commit()
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("5.00"),
            Decimal("5.00"),
        ), "setup: a clean two-member place holding five bottles"

        probe = raw(autocommit=True)
        scope_resolved = threading.Event()
        row_deleted = threading.Event()
        blocked: dict = {}
        sink: dict = {}

        def park_the_delivery(*_args, **_kwargs):
            # We are INSIDE `get_or_create_balance`, i.e. `_create_ledger_entry`
            # has already run `resolve_scope` and decided this write belongs to
            # the GROUP — and the FOR UPDATE has not been issued yet.
            scope_resolved.set()
            assert row_deleted.wait(timeout=30), "the dissolve never reached its delete"

        def release_the_delivery(_result, *_args, **_kwargs):
            # The group's balance row is now DELETED but NOT COMMITTED, and this
            # transaction holds it. Let the delivery run into that lock and wait
            # until Postgres itself confirms a backend is blocked on it.
            row_deleted.set()
            blocked["seen"] = _wait_until_a_backend_blocks_on_a_lock(probe)

        def deliver():
            from business_app import db as _db

            entry = BottleTrackingService().record_bottles_delivered(
                late_order.id, oa.id, a.id, Decimal("2.00")
            )
            _db.session.commit()
            return entry.id

        def dissolve():
            return CustomerLinkService().remove_address_from_group(
                a.id, acting_admin_id=admin.id, reason="employee moved out"
            )

        with _hook(
            BottleTrackingService,
            "get_or_create_balance",
            before=park_the_delivery,
            thread_name="deliver",
        ), _hook(
            BottleTrackingService,
            "release_group_history_to_address",
            after=release_the_delivery,
            thread_name="dissolve",
        ):
            deliverer = _in_app(papp, deliver, sink, "deliver")
            try:
                assert scope_resolved.wait(timeout=30), (
                    "the delivery never reached get_or_create_balance — the hook "
                    "no longer names a call site on the delivery path"
                )
                dissolver = _in_app(papp, dissolve, sink, "dissolve")
                dissolver.join(timeout=60)
            finally:
                # Never leave the delivery parked, whatever went wrong above.
                row_deleted.set()
                deliverer.join(timeout=60)

        removal = _reraise(sink, "dissolve")
        ledger_id = _reraise(sink, "deliver")
        assert removal["dissolved"] is True, (
            "the setup depends on the removal DISSOLVING the place; if §7.3's "
            "arm changed, this race no longer describes anything"
        )
        assert blocked.get("seen") is True, (
            "the delivery never actually WAITED on the group's balance row, so "
            "this run did not exercise the lock window at all. Backends: "
            f"{_backend_activity(probe)}"
        )
        pgdb.session.expire_all()
        return group, a, b, late_order.id, ledger_id

    def test_a_delivery_that_blocks_on_a_DISSOLVE_stays_REACHABLE_by_some_address(
        self, papp, pgdb, raw
    ):
        """FIXED — the xfail is gone. Two bottles physically handed over are reachable.

        WAS: `_create_ledger_entry` resolved the scope from an UNLOCKED read of
        `addresses` and never re-validated it once `get_or_create_balance` held
        the balance row. A delivery that resolved to place group G and then
        blocked on G's balance row while a removal DISSOLVED that place found
        the row deleted when it woke, so the ON CONFLICT DO NOTHING insert MINTED
        A BRAND-NEW `bottle_balances` row keyed to the now-MEMBERLESS group and
        stamped the DELIVERY ledger row `address_group_id=G` too. No live address
        resolved to G any more, while global conservation held and G's own
        stored == ledger_sum — the exact orphan class §7.3's dissolve exists to
        eliminate, recreated by an ordinary delivery.

        NOW: the delivery takes `addresses(A)` FOR SHARE (rung 1) BEFORE it
        chooses a balance row, so it blocks on the MAPPING, not on the money.
        The dissolve holds `addresses(A)` FOR NO KEY UPDATE, and when it commits
        Postgres's EvalPlanQual re-check hands the woken delivery the COMMITTED
        `address_group_id` — NULL — so it resolves to A's own scope and books
        there. There is nothing left to "re-validate": the mapping was pinned
        before the row was picked.

        Deliberately does NOT dictate WHERE they land — refusing the write would
        also satisfy this. What may never happen is the pre-fix answer: they land
        in a place nobody can open.
        """
        group, a, b, _order_id, _ledger_id = (
            self._race_a_group_scoped_delivery_against_the_dissolve(papp, pgdb, raw)
        )
        reachable = BottleTrackingService.get_place_balance(
            a.id
        ) + BottleTrackingService.get_place_balance(b.id)
        assert _members(pgdb, group.id) == [], "the place really did dissolve"
        assert reachable == Decimal("7.00"), (
            f"the two delivered bottles are unreachable: address {a.id} resolves "
            f"to {BottleTrackingService.get_place_balance(a.id)} and address "
            f"{b.id} to {BottleTrackingService.get_place_balance(b.id)}, summing "
            f"to {reachable} — the missing 2.00 sit in memberless group "
            f"{group.id}: {_raw_balance_rows(pgdb, group_id=group.id)}"
        )
        assert group.id not in _sweep(papp)["orphaned_place_balances"]

    def test_the_dissolve_race_LEAVES_NO_memberless_balance_row_and_books_the_OWN_scope(
        self, papp, pgdb, raw
    ):
        """UPDATED: every figure below changed when the ladder landed.

        This used to be a CURRENT-BEHAVIOUR pin of the damage — a brand-new
        `bottle_balances` row minted for a group with no members, a DELIVERY
        ledger row stamped to it, both figures agreeing inside the orphan, and
        the nightly sweep firing `orphaned_place_balances`. It is now the
        per-scope ATTRIBUTION pin for the fixed behaviour, asserted at the same
        granularity, because Σ alone was blind to the defect and is equally blind
        to a regression.
        """
        group, a, b, late_order_id, ledger_id = (
            self._race_a_group_scoped_delivery_against_the_dissolve(papp, pgdb, raw)
        )

        # 1. NOTHING was minted for the memberless group; the dissolve's delete
        #    stands.
        assert _raw_balance_rows(pgdb, group_id=group.id) == [], (
            "a bottle_balances row exists for a group with no members — the "
            "orphan class §7.3 exists to eliminate has been recreated"
        )
        assert _members(pgdb, group.id) == []

        # 2. The delivery landed in the address's OWN scope, and the ledger row
        #    was stamped to the same place the balance moved — by construction,
        #    since both now come off one locked resolution.
        entry = BottleLedger.query.get(ledger_id)
        assert entry.idempotency_key == f"delivery:{late_order_id}"
        assert entry.address_group_id is None
        assert entry.address_id == a.id
        own = BottleScope.for_address(a.id)
        assert _pair(pgdb, own) == (Decimal("2.00"), Decimal("2.00"))

        # 3. Both live addresses reach their bottles.
        assert BottleTrackingService.get_place_balance(a.id) == Decimal("2.00")
        assert BottleTrackingService.get_place_balance(b.id) == Decimal("5.00")
        ledger_page = BottleTrackingService.get_place_ledger(a.id, page=1, per_page=50)
        assert ledger_id in [item.id for item in ledger_page["items"]], (
            "the customer whose door the bottles went through cannot see the "
            "delivery they signed for"
        )

        # 4. Conservation still holds — it always did, which is precisely why it
        #    could never have caught this.
        assert _sum_over(
            pgdb,
            [
                BottleScope.for_group(group.id),
                BottleScope.for_address(a.id),
                BottleScope.for_address(b.id),
            ],
        ) == Decimal("7.00")

        # 5. And the alarm that used to fire is silent.
        assert group.id not in _sweep(papp)["orphaned_place_balances"]


class TestADeliveryThatBlocksOnTheAbsorbMintsAWholeNewInvisibleScope:
    """Costume 2: the same defect on the JOIN path, and NOTHING is lost.

    Distinct from the interleave `test_a_CONCURRENT_DELIVERY_is_not_swallowed_
    from_the_place_BALANCE` pins: there the delivery commits BEFORE the absorb
    takes its lock and its balance is swallowed. Here it commits AFTER, so both
    figures are perfectly conserved — an entirely new, invisible scope is
    created instead.

    That also falsifies the comment at `business_app/tasks/customer_link_tasks.py`
    (lines ~107-115), which asserts `stranded_address_balances` can now only come
    from a direct DB edit, a pre-re-scoping restore, or a future write path. An
    ordinary concurrent delivery mints one TODAY, so a real production sweep hit
    would be misdiagnosed as a bad restore.
    """

    def _race_a_delivery_against_the_absorb(self, papp, pgdb, raw):
        """Park a delivery at an UNGROUPED address between `resolve_scope` and
        the FOR UPDATE, then group that address underneath it.

        Returns ``(group_id, address_a, address_b, late_order_id, ledger_id)``.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        _deliver(pgdb, oa, a, 3)
        late_order = _order(pgdb, oa, a)
        pgdb.session.commit()
        assert _pair(pgdb, BottleScope.for_address(a.id)) == (
            Decimal("3.00"),
            Decimal("3.00"),
        ), "setup: an ungrouped, funded address with its own row and its own history"

        probe = raw(autocommit=True)
        scope_resolved = threading.Event()
        row_absorbed = threading.Event()
        blocked: dict = {}
        sink: dict = {}

        def park_the_delivery(*_args, **_kwargs):
            scope_resolved.set()
            assert row_absorbed.wait(timeout=30), "the join never absorbed the joiner"

        def release_the_delivery(_result, *args, **kwargs):
            address_id = kwargs.get("address_id", args[0] if args else None)
            if address_id != a.id:
                return
            # The joiner's own balance row is DELETED but NOT COMMITTED and this
            # transaction holds it.
            row_absorbed.set()
            blocked["seen"] = _wait_until_a_backend_blocks_on_a_lock(probe)

        def deliver():
            from business_app import db as _db

            entry = BottleTrackingService().record_bottles_delivered(
                late_order.id, oa.id, a.id, Decimal("2.00")
            )
            _db.session.commit()
            return entry.id

        def join():
            group = CustomerLinkService().create_place_group(
                [a.id, b.id], acting_admin_id=admin.id, reason="same office"
            )
            return group.id

        with _hook(
            BottleTrackingService,
            "get_or_create_balance",
            before=park_the_delivery,
            thread_name="deliver",
        ), _hook(
            BottleTrackingService,
            "absorb_address_into_group",
            after=release_the_delivery,
            thread_name="join",
            once=False,
        ):
            deliverer = _in_app(papp, deliver, sink, "deliver")
            try:
                assert scope_resolved.wait(timeout=30), (
                    "the delivery never reached get_or_create_balance"
                )
                joiner = _in_app(papp, join, sink, "join")
                joiner.join(timeout=60)
            finally:
                row_absorbed.set()
                deliverer.join(timeout=60)

        group_id = _reraise(sink, "join")
        ledger_id = _reraise(sink, "deliver")
        assert blocked.get("seen") is True, (
            "the delivery never actually WAITED on the joiner's own balance row, "
            f"so this run did not exercise the lock window. Backends: "
            f"{_backend_activity(probe)}"
        )
        pgdb.session.expire_all()
        assert UserAddress.query.get(a.id).address_group_id == group_id
        return group_id, a, b, late_order.id, ledger_id

    def test_a_delivery_that_blocks_on_the_ABSORB_lands_in_the_place_it_joined(
        self, papp, pgdb, raw
    ):
        """FIXED — the xfail is gone.

        WAS: a delivery that resolved an address as UNGROUPED, then blocked on
        that address's own `bottle_balances` row while `create_place_group`
        absorbed and DELETED it, woke to find nothing and INSERTED a fresh
        own-scope row (address_id=A, address_group_id=NULL) plus a DELIVERY
        ledger row stamped NULL — for an address whose live pointer was by then
        the group. Nothing was lost and both figures agreed INSIDE the new
        scope, so every conservation and per-place oracle passed; the bottles
        were simply invisible, because `resolve_scope` sends every reader to the
        group.

        NOW: the delivery blocks on `addresses(A)` at rung 1, not on the money,
        and wakes with the join's COMMITTED `address_group_id` — so it resolves
        to the place and books there.
        """
        group_id, a, _b, _order_id, _ledger_id = self._race_a_delivery_against_the_absorb(
            papp, pgdb, raw
        )
        assert BottleTrackingService.get_place_balance(a.id) == Decimal("5.00"), (
            "the place holds only the absorbed 3.00; the concurrent +2 is in a "
            f"scope no reader resolves to: {_raw_balance_rows(pgdb, address_id=a.id)}"
        )
        assert _raw_balance_rows(pgdb, address_id=a.id) == [], (
            "a grouped address must not carry an own-scope balance row (§7.2)"
        )
        assert _sweep(papp)["stranded_address_balances"] == []

    def test_the_absorb_race_STRANDS_NOTHING_and_the_LEDGER_follows_the_balance(
        self, papp, pgdb, raw
    ):
        """UPDATED: every figure below changed when the ladder landed.

        This used to pin the damage — a fresh own-scope `bottle_balances` row and
        a NULL-stamped DELIVERY ledger row for an address that was by then
        GROUPED, both perfectly self-consistent, Σ conserved, and only the
        nightly `stranded_address_balances` bucket able to see it. It is now the
        per-scope ATTRIBUTION pin for the fixed behaviour, at the same
        granularity, because Σ was blind to the defect and is equally blind to a
        regression.
        """
        group_id, a, b, late_order_id, ledger_id = self._race_a_delivery_against_the_absorb(
            papp, pgdb, raw
        )
        own, place = BottleScope.for_address(a.id), BottleScope.for_group(group_id)

        # 1. A grouped address carries NO own-scope balance row (§7.2).
        assert _raw_balance_rows(pgdb, address_id=a.id) == []
        assert _pair(pgdb, own) == (Decimal("0.00"), Decimal("0.00"))

        # 2. The ledger row was stamped to the place the balance moved — one
        #    locked resolution feeds both.
        entry = BottleLedger.query.get(ledger_id)
        assert entry.idempotency_key == f"delivery:{late_order_id}"
        assert entry.address_group_id == group_id
        assert entry.address_id == a.id

        # 3. Every reader resolves to the place, and the place saw the +2.
        assert BottleTrackingService.get_place_balance(a.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(b.id) == Decimal("5.00")
        assert _pair(pgdb, place) == (Decimal("5.00"), Decimal("5.00"))
        ledger_page = BottleTrackingService.get_place_ledger(a.id, page=1, per_page=50)
        visible = [item.id for item in ledger_page["items"]]
        assert ledger_id in visible, (
            f"the concurrent delivery (ledger row {ledger_id}) is missing from "
            f"the history of the very address it was delivered to; that view "
            f"shows only {visible}"
        )

        # 4. NOTHING was lost — Σ over the two scopes is still the whole 5.00.
        assert _sum_over(pgdb, [own, place]) == Decimal("5.00")

        # 5. And the sweep bucket that used to catch it is empty.
        assert _sweep(papp)["stranded_address_balances"] == []


class TestReconcileReadsTheLedgerSumBeforeItTakesTheLock:
    """Costume 3: the ONE writer that moves a balance without a ledger entry.

    Every already-confirmed reconcile defect says "reconcile destroys a
    legitimately CARRIED figure". This one is different in kind: it destroys a
    figure the ledger itself fully explains, on a place that was CLEAN, and it
    does so by racing a delivery — which is precisely the moment an admin
    reaches for Reconcile, because a number looked wrong mid-route.

    No per-place oracle can see it: after the damage the place's stored figure
    and its ledger sum DISAGREE, but nothing in the system compares them except
    reconcile itself, and a second run would "fix" it back — so the operation is
    not even idempotent.
    """

    def _race_reconcile_against_a_committing_delivery(self, papp, pgdb, raw):
        """`reconcile_balance` reads SUM(ledger) at bottle_tracking_service.py
        :1892-1897 and only THEN calls `get_or_create_balance`. Park a delivery
        on that row so reconcile blocks between the two, and commit the delivery
        while it waits.

        Returns ``(owner, address, report)``.
        """
        owner = _user(pgdb)
        a = _addr(pgdb, owner)
        _deliver(pgdb, owner, a, 7)
        late_order = _order(pgdb, owner, a)
        pgdb.session.commit()
        scope = BottleScope.for_address(a.id)
        assert _pair(pgdb, scope) == (Decimal("7.00"), Decimal("7.00")), (
            "setup: a CLEAN place — stored and ledger agree exactly"
        )

        probe = raw(autocommit=True)
        holding, go = threading.Event(), threading.Event()
        sink: dict = {}

        def late_delivery():
            from business_app import db as _db

            entry = BottleTrackingService().record_bottles_delivered(
                late_order.id, owner.id, a.id, Decimal("3.00")
            )
            _db.session.flush()  # holds the place's balance row FOR UPDATE
            holding.set()
            assert go.wait(timeout=30)
            _db.session.commit()
            return entry.id

        def reconcile():
            return BottleTrackingService().reconcile_balance(a.id)

        deliverer = _in_app(papp, late_delivery, sink, "deliver")
        try:
            assert holding.wait(timeout=30), "the delivery never took its row lock"
            reconciler = _in_app(papp, reconcile, sink, "reconcile")
            try:
                # Reconcile has now run its unlocked SUM (7.00) and is waiting
                # for the very row the delivery holds. Only then is the delivery
                # allowed to commit 10.00 underneath it.
                assert _wait_until_a_backend_blocks_on_a_lock(probe), (
                    "reconcile never blocked on the place's balance row — it no "
                    "longer takes it FOR UPDATE, and this race is void. "
                    f"Backends: {_backend_activity(probe)}"
                )
            finally:
                go.set()
                reconciler.join(timeout=60)
        finally:
            go.set()
            deliverer.join(timeout=60)

        _reraise(sink, "deliver")
        report = _reraise(sink, "reconcile")
        pgdb.session.expire_all()
        return owner, a, report

    def test_reconcile_racing_a_delivery_is_a_NO_OP_on_a_clean_place(
        self, papp, pgdb, raw
    ):
        """FIXED — the xfail is gone.

        WAS: `reconcile_balance` evaluated SUM(bottle_ledger.quantity) BEFORE
        `get_or_create_balance` took the balance row FOR UPDATE. A delivery that
        committed while reconcile waited for that lock was compared against a
        PRE-delivery ledger sum, and reconcile — the only balance writer in the
        codebase that appends no ledger entry — assigned the stale figure. A
        CLEAN place (stored == ledger_sum == 7.00) ended up DRIFTED (stored 7.00,
        ledger 10.00) with a committed delivery silently eaten, and a second run
        flipped it back, so the operation was not even idempotent.

        NOW: every read is BELOW the lock. Holding the place's single balance row
        excludes every concurrent writer at that place, and rung 1 excludes the
        lifecycle's ledger re-stamps, so the two figures describe one world and
        this interleave is a no-op.
        """
        _owner, a, report = self._race_reconcile_against_a_committing_delivery(
            papp, pgdb, raw
        )
        scope = BottleScope.for_address(a.id)
        assert _pair(pgdb, scope) == (Decimal("10.00"), Decimal("10.00")), (
            "reconcile CREATED a drift on a place that had none, and destroyed a "
            "committed delivery to do it"
        )
        assert report["corrected"] is False
        assert report["discrepancy"] == 0.0

    def test_the_reconcile_race_REPORTS_the_TRUTH_and_is_IDEMPOTENT(
        self, papp, pgdb, raw
    ):
        """UPDATED: every figure below changed, including what the admin is TOLD.

        This used to pin the damage: the place went in CLEAN (7.00 / 7.00) and
        came out DRIFTED (stored 7.00, ledger 10.00); the report said
        `previous_balance: 10.0, recalculated_balance: 7.0, discrepancy: 3.0,
        corrected: True` — "I repaired a 3-bottle error" — while what actually
        happened was that three real, committed, ledger-backed bottles were
        deleted from the customer's balance; and an immediate second run flipped
        it straight back.

        The report is still the sharpest part, so it is still asserted field by
        field: both figures now describe the SAME world, the admin is told
        nothing was wrong, and a second run changes nothing.
        """
        _owner, a, report = self._race_reconcile_against_a_committing_delivery(
            papp, pgdb, raw
        )
        scope = BottleScope.for_address(a.id)

        assert _pair(pgdb, scope) == (Decimal("10.00"), Decimal("10.00"))
        assert report["previous_balance"] == 10.0
        assert report["recalculated_balance"] == 10.0, (
            "the balance row was taken FOR UPDATE BEFORE the ledger was summed, "
            "so both figures were read after the concurrent delivery committed"
        )
        assert report["discrepancy"] == 0.0
        assert report["corrected"] is False

        # Idempotent: an immediate second run is another no-op.
        again = BottleTrackingService().reconcile_balance(a.id)
        pgdb.session.expire_all()
        assert again["corrected"] is False
        assert again["recalculated_balance"] == 10.0
        assert _pair(pgdb, scope) == (Decimal("10.00"), Decimal("10.00"))


class TestTheNightlySweepReadsNoConsistentSnapshot:
    """`reconcile_customer_link_invariants` issues ~10 INDEPENDENT SELECTs with
    no transaction boundary and no snapshot pin, so a commit landing between two
    of them is seen by one and not the other.

    This is the ONLY automated alarm on the entire place layer, it runs
    unattended as a Celery beat task, and the documented operator response to
    `orphaned_place_balances` is the DESTRUCTIVE
    `POST /admin/bottles/reconcile/<address_id>`. A nightly false positive both
    trains the operator to ignore the one alarm guarding this whole bug class and
    points them at the button that destroys a drifted place's real balance.
    """

    @staticmethod
    def _pause_at_the_orphan_query(engine, thread_name, arrived, release):
        """Park the sweep's ORPHAN query — statement 4 — on a real cursor hook.

        Deterministic by construction: the callback fires on the SQL the check
        is built from, in the sweep's own thread, so "between statement 3 and
        statement 4" is a fact about the wire, not about scheduling. Returns the
        listener so the caller can detach it.
        """
        from sqlalchemy import event as sa_event

        def before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            if threading.current_thread().name != thread_name or arrived.is_set():
                return
            normalized = " ".join(statement.split()).lower()
            if (
                normalized.startswith("select bottle_balances.address_group_id")
                and "from bottle_balances" in normalized
                and "is not null" in normalized
            ):
                arrived.set()
                release.wait(timeout=30)

        sa_event.listen(engine, "before_cursor_execute", before_cursor_execute)
        return before_cursor_execute

    def _run_sweep(self, papp, pgdb, *, join_between: bool):
        """Two ungrouped funded addresses; group them (optionally MID-SWEEP).

        Returns ``(group_id, report)``.
        """
        from sqlalchemy import event as sa_event

        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        _deliver(pgdb, oa, a, 3)
        _deliver(pgdb, ob, b, 2)
        pgdb.session.commit()

        def make_the_place():
            group = CustomerLinkService().create_place_group(
                [a.id, b.id], acting_admin_id=admin.id, reason="same office"
            )
            pgdb.session.commit()
            return group.id

        sink: dict = {}
        if not join_between:
            group_id = make_the_place()
            sweeper = _in_app(papp, lambda: _sweep(papp), sink, "sweep")
            sweeper.join(timeout=60)
            return group_id, _reraise(sink, "sweep")

        engine = pgdb.engine
        arrived, release = threading.Event(), threading.Event()
        listener = self._pause_at_the_orphan_query(engine, "sweep", arrived, release)
        try:
            sweeper = _in_app(papp, lambda: _sweep(papp), sink, "sweep")
            try:
                assert arrived.wait(timeout=30), (
                    "the sweep never issued its orphaned_place_balances query — "
                    "the check was rewritten and this interleave names nothing"
                )
                group_id = make_the_place()
            finally:
                release.set()
                sweeper.join(timeout=60)
        finally:
            sa_event.remove(engine, "before_cursor_execute", listener)
        return group_id, _reraise(sink, "sweep")

    def test_CONTROL_the_sweep_reports_a_quiet_healthy_place_as_clean(
        self, papp, pgdb
    ):
        """Falsifiability control. Without it, the two tests below would pass
        just as happily if the orphan check had been deleted."""
        group_id, report = self._run_sweep(papp, pgdb, join_between=False)
        assert _members(pgdb, group_id), "the place really has live members"
        assert group_id not in report["orphaned_place_balances"]
        assert _raw_balance_rows(pgdb, group_id=group_id), (
            "the join created the place's balance row, so the orphan check DID "
            "have a row of this group's to consider"
        )

    def test_a_join_committing_MID_SWEEP_is_not_reported_as_an_orphaned_place(
        self, papp, pgdb
    ):
        """WAS a strict xfail. The false alarm is CLOSED: every orphan candidate
        is re-verified against the world at the end of the sweep
        (`_confirm_orphaned_place_balances`), so a place that joined between the
        membership read and the balance read answers "it has members now" and is
        dropped instead of being reported to an operator whose documented
        response is the DESTRUCTIVE reconcile."""
        group_id, report = self._run_sweep(papp, pgdb, join_between=True)
        assert _members(pgdb, group_id), "the place has two live members"
        assert group_id not in report["orphaned_place_balances"], (
            f"place group {group_id} has members {_members(pgdb, group_id)} and "
            "was still reported as orphaned, because the sweep read "
            "user_addresses before the join committed and bottle_balances after"
        )

    def test_the_mid_sweep_join_is_NO_LONGER_reported_as_an_orphan(self, papp, pgdb):
        """UPDATED — this pinned the false positive as today's behaviour
        (`group_id in report["orphaned_place_balances"]`) so it could not be
        lost, with a docstring saying that fixing the skew must change it. It
        did.

        The interleave is unchanged and still real — the balance read still sees
        a row the membership read could not — so what is asserted now is that
        the sweep's ANSWER no longer depends on it: the mid-sweep join reports
        exactly what a quiet re-run reports."""
        group_id, report = self._run_sweep(papp, pgdb, join_between=True)
        assert len(_members(pgdb, group_id)) == 2
        assert group_id not in report["orphaned_place_balances"]
        # The same answer a quiet re-run gives, which is the whole point: the
        # sweep is no longer sensitive to what commits underneath it.
        assert group_id not in _sweep(papp)["orphaned_place_balances"]


# =========================================================================== #
# 12. THE DELIVERED TRANSITION ITSELF — what the bottle invariants cannot see
# =========================================================================== #
#
# Both cases below leave every place-scoped figure PERFECT. What they corrupt is
# something no place test looks at: driver accountability, and the atomicity of
# the transition that moves the money and the bottles together.
# --------------------------------------------------------------------------- #


def _bottle_product(pgdb, per_unit="2"):
    """A real product whose items carry returnable bottles."""
    from business_app.models.product import Product, ProductCategory

    n = _uniq()
    category = ProductCategory(name=f"PG-Water-{n}", description="w", is_active=True)
    pgdb.session.add(category)
    pgdb.session.commit()
    product = Product(
        name=f"Pure Water 19L #{n}",
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
    pgdb.session.add(product)
    pgdb.session.commit()
    return product


def _order_with_bottles(pgdb, owner, address, product, *, quantity, status):
    """An order carrying returnable-bottle items, at the given status.

    Deliberately NOT a CASH order: the money half of the DELIVERED branch is
    exhaustively covered on SQLite by ``test_place_money_boundary_e2e.py``, and
    leaving ``payment_method`` unset keeps these tests pointed at the bottle and
    driver-accountability halves.
    """
    from business_app.models.order import OrderItem

    n = _uniq()
    order = Order(
        user_id=owner.id,
        order_number=f"ORD-PGDLV-{n:06d}",
        status=status,
        subtotal=Decimal("30000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("30000.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    pgdb.session.add(order)
    pgdb.session.flush()
    pgdb.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * Decimal(str(quantity)),
        )
    )
    pgdb.session.commit()
    return order


def _bind_order_to_a_fresh_session(pgdb, driver, order, *, loaded=100):
    """Open a real driver bottle session and bind the order to it."""
    from business_app.models.bottle import DriverBottleSessionOrder

    session = BottleTrackingService().open_bottle_session(
        driver_user_id=driver.id, bottles_loaded=loaded, actor_user_id=driver.id
    )
    pgdb.session.add(
        DriverBottleSessionOrder(
            session_id=session.id, order_id=order.id, accepted_by_driver_id=driver.id
        )
    )
    pgdb.session.commit()
    return session


class TestTwoConcurrentDeliveredTransitions:
    """The admin marks it delivered from the dropdown while the driver submits
    at the door — an everyday event, not a contrived race.

    ``update_order_status`` used to have no version column and no
    ``WHERE status = ...`` on its UPDATE, so both callers passed
    ``_is_valid_status_transition`` against the same pre-image and both ran the
    whole DELIVERED branch. The bottle LEDGER survived that intact, because every
    write it makes carries an idempotency key; the driver session tally sat
    OUTSIDE that protection and was credited twice.

    The transition is now CLAIMED with a compare-and-set UPDATE
    (``OrderService._claim_status_transition``), so the second caller matches 0
    rows and short-circuits before any side-effect runs.
    """

    def _race_two_delivered_transitions(self, papp, pgdb):
        """Both transitions enter with the order still at OUT_FOR_DELIVERY.

        The barrier sits on ``_is_valid_status_transition``, i.e. AFTER both
        threads have loaded the order and BEFORE either has claimed the status
        change — the exact pre-image both callers act on. A one-sided entry
        barrier is sound here because the outcome does not depend on which side
        finishes.

        The loser of the row-lock race is an idempotent NO-OP, not an error: it
        returns the order the winner committed. So both calls still return
        normally, and the assertion below still means what it always meant —
        neither side blew up, and neither side was rejected.

        Returns ``(order, driver, session_id, address, group)``.
        """
        from business_app.services.order_service import OrderService

        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        driver = _user(pgdb, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
        product = _bottle_product(pgdb, per_unit="2")
        order = _order_with_bottles(
            pgdb, oa, a, product, quantity=3, status=OrderStatus.OUT_FOR_DELIVERY
        )
        session = _bind_order_to_a_fresh_session(pgdb, driver, order, loaded=100)
        session_id = session.id
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("0.00"),
            Decimal("0.00"),
        )

        sink: dict = {}
        barrier = threading.Barrier(2, timeout=30)
        synced: set = set()
        sync_guard = threading.Lock()

        def meet_at_the_transition_check(*_args, **_kwargs):
            """Both threads have loaded the order and neither has flushed yet.

            Guarded per thread rather than globally: `_is_valid_status_transition`
            is reachable more than once per request through the delivery service,
            and a barrier that re-arms would park a thread forever.
            """
            me = threading.current_thread().name
            with sync_guard:
                if me in synced:
                    return
                synced.add(me)
            barrier.wait(timeout=30)

        def transition():
            from business_app import db as _db

            OrderService().update_order_status(
                order.id,
                OrderStatus.DELIVERED,
                updated_by=admin.id,
                bottles_returned=2,
            )
            _db.session.commit()
            return "delivered"

        with _hook(
            OrderService,
            "_is_valid_status_transition",
            before=meet_at_the_transition_check,
            once=False,
        ):
            first = _in_app(papp, transition, sink, "deliver_1")
            second = _in_app(papp, transition, sink, "deliver_2")
            first.join(timeout=90)
            second.join(timeout=90)

        # Both calls must have RETURNED NORMALLY. The race is now serialised by
        # `_claim_status_transition`, and the whole point of doing it with a
        # compare-and-set rather than a rejection is that the losing caller is a
        # silent idempotent no-op — an admin who clicks "Delivered" a second
        # apart from the driver must not be shown an error for an order that IS
        # delivered. An exception on either side is therefore still a failure.
        outcomes = [sink.get("deliver_1"), sink.get("deliver_2")]
        assert outcomes == ["delivered", "delivered"], (
            f"both transitions were expected to complete; got {outcomes}"
        )
        pgdb.session.expire_all()
        return order, driver, session_id, a, group

    def test_two_concurrent_DELIVERED_transitions_book_the_bottles_EXACTLY_ONCE(
        self, papp, pgdb
    ):
        """The half that HOLDS: `_create_ledger_entry`'s idempotency short-circuit
        makes the place immune to the double transition."""
        order, _driver, _session_id, a, group = self._race_two_delivered_transitions(
            papp, pgdb
        )
        delivery_rows = BottleLedger.query.filter_by(
            idempotency_key=f"delivery:{order.id}"
        ).all()
        return_rows = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"return:{order.id}:%")
        ).all()
        assert len(delivery_rows) == 1, (
            f"{len(delivery_rows)} DELIVERY ledger rows for one order"
        )
        assert len(return_rows) == 1, (
            f"{len(return_rows)} RETURN ledger rows for one order"
        )
        assert D(delivery_rows[0].quantity) == Decimal("6.00")
        assert D(return_rows[0].quantity) == Decimal("-2.00")
        assert delivery_rows[0].address_group_id == group.id
        # 3 units x 2 bottles per unit, less the 2 the customer handed back.
        assert BottleTrackingService.get_place_balance(a.id) == Decimal("4.00")
        assert _pair(pgdb, BottleScope.for_group(group.id)) == (
            Decimal("4.00"),
            Decimal("4.00"),
        )

    def test_two_concurrent_DELIVERED_transitions_tally_the_DRIVER_SESSION_ONCE(
        self, papp, pgdb
    ):
        """The half that used to BREAK — and it broke somewhere no place test looks.

        The driver-session tally is a bare ``+=``: it sits OUTSIDE
        ``_create_ledger_entry``'s idempotency short-circuit and carries no key of
        its own, so while the ledger deduped perfectly and the place landed on the
        right figure, ``bottles_delivered`` / ``bottles_collected_from_customers``
        were incremented TWICE. At session close ``compute_discrepancy()`` then
        accused the driver of losing the difference, and
        ``assert_delivery_within_session_capacity`` could block their next delivery.

        Closed one level up, where the duplication actually comes from:
        ``update_order_status`` now CLAIMS the transition with
        ``UPDATE orders SET status = <new> WHERE id = <id> AND status = <pre-image>``
        (``_claim_status_transition``). The second caller matches 0 rows and
        returns before any side-effect runs, so the tally — and everything else in
        the branch that is not idempotent — happens exactly once per transition
        rather than once per caller.
        """
        from business_app.models.bottle import DriverBottleSession

        order, driver, session_id, _a, _group = self._race_two_delivered_transitions(
            papp, pgdb
        )
        session = DriverBottleSession.query.get(session_id)
        assert session.bottles_delivered == 6, (
            f"the session was credited with {session.bottles_delivered} delivered "
            "bottles for a single 6-bottle order"
        )
        assert session.bottles_collected_from_customers == 2, (
            f"the session was credited with "
            f"{session.bottles_collected_from_customers} collected bottles for a "
            "single 2-bottle return"
        )

        # The consequence, driven through the real close: the driver loaded 100,
        # handed over 6, took 2 back, and returns 96 to the warehouse. That is
        # perfect accountability and must compute to zero.
        closed = BottleTrackingService().close_bottle_session(
            driver.id, 96, actor_user_id=driver.id
        )
        pgdb.session.commit()
        assert closed.discrepancy == 0, (
            f"the driver is accused of a discrepancy of {closed.discrepancy} "
            "bottles for a route they accounted for exactly"
        )


class TestTheWideExceptArmInTheDeliveredBottleBlock:
    """`except Exception` (order_service.py:1738) catches a strictly wider set
    than the `except ValidationError: raise` arm directly above it, and it is the
    arm where the code deliberately chooses to LOSE bottles.

    On SQLite — where the rest of this feature's suite lives — the swallow
    works: the following `db.session.commit()` succeeds and a DELIVERED order is
    written with no bottle record. On Postgres a failed statement ABORTS the
    transaction, so that same commit cannot succeed. Every conclusion the SQLite
    suite draws about this arm is therefore wrong for production, and the
    comment at :1733-1737 ("the outer transaction rolls back") is false for the
    narrow arm and vacuous for this one.

    The failure is produced by real production configuration, not a patch: an
    admin-settable `returnable_bottles_per_unit` (NUMERIC(12,2)) large enough
    that the order's bottle total overflows `bottle_balances.balance`, which is
    the same NUMERIC(12,2). Postgres answers 22003; SQLite has no such answer.
    """

    def test_a_NON_validation_error_from_the_bottle_block_ABORTS_the_whole_commit(
        self, papp, pgdb
    ):
        from business_app.services.order_service import OrderService

        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 4)
        product = _bottle_product(pgdb, per_unit="9999999999.99")
        order = _order_with_bottles(
            pgdb, oa, a, product, quantity=2, status=OrderStatus.OUT_FOR_DELIVERY
        )
        scope = BottleScope.for_group(group.id)
        assert _pair(pgdb, scope) == (Decimal("4.00"), Decimal("4.00"))

        sink: dict = {}

        def transition():
            return OrderService().update_order_status(
                order.id, OrderStatus.DELIVERED, updated_by=admin.id
            )

        # Run it on its OWN session: the point of the test is what happens to
        # that transaction, and sharing the fixture's session would let the
        # aborted state leak into the assertions below.
        thread = _in_app(papp, transition, sink, "transition")
        thread.join(timeout=90)
        outcome = sink.get("transition")

        assert isinstance(outcome, BaseException), (
            "the DataError was swallowed AND the commit succeeded — that is the "
            "SQLite outcome, and it means a DELIVERED order was written with no "
            "bottle record at all"
        )
        # 25P02 (in_failed_sql_transaction) or SQLAlchemy's own PendingRollbackError:
        # both say the same thing — the swallow left an unusable transaction.
        message = f"{type(outcome).__name__}: {outcome}"
        assert (
            _sqlstate(outcome) == "25P02"
            or "PendingRollbackError" in type(outcome).__name__
            or "current transaction is aborted" in message
        ), (
            "expected the post-swallow commit to fail on the aborted "
            f"transaction; got {message[:400]}"
        )

        # Whatever the caller saw, the PLACE must be untouched: the overflow
        # never reached bottle_balances, and no partial ledger row survived.
        pgdb.session.expire_all()
        assert _pair(pgdb, scope) == (Decimal("4.00"), Decimal("4.00"))
        assert BottleLedger.query.filter_by(order_id=order.id).count() == 0

        # THE DIVERGENCE, pinned. On Postgres the order is still
        # OUT_FOR_DELIVERY: the aborted transaction took the status change down
        # with it, the caller gets a 500, and the driver can retry. On SQLite —
        # where the rest of this feature's tests decide what this arm does — the
        # very same code path COMMITS a DELIVERED order with no bottle record,
        # which ORDER_STATUS_TRANSITIONS[DELIVERED] == [] makes unrepairable
        # forever. One `except Exception`, two opposite outcomes; only one of
        # them is production.
        assert Order.query.get(order.id).status == OrderStatus.OUT_FOR_DELIVERY, (
            "the delivery did NOT roll back whole — a DELIVERED order with no "
            "bottle record is the SQLite outcome, and it is unrepairable"
        )
        assert "PendingRollbackError" in type(outcome).__name__, (
            "expected SQLAlchemy's PendingRollbackError on the post-swallow "
            f"commit; got {type(outcome).__name__}. If this becomes a plain "
            "DataError the swallow was narrowed, which is the fix."
        )


# =========================================================================== #
# 13. THE ORACLES THEMSELVES — controls, and the randomised soak (design §6)
# =========================================================================== #
#
# §6 closes with a mandate, quoted because it is the whole reason this section
# exists: "Every new oracle ships with a committed known-bad control that makes
# it fire. A scope oracle that silently passes is worse than none, and this
# effort has already learned that the hard way."
#
# 13.1  makes the autouse reachability oracle's three BUCKETS (§6.1) go red on
#       demand.
# 13.1a makes the autouse FIXTURE ITSELF go red on demand — a separate claim,
#       because every control in 13.1 opts out of that fixture to do its work.
# 13.2 makes the deadlock counter the soak reads go up on demand.
# 13.3 is the soak (§6.5) — the only oracle here that does not depend on
#      somebody having thought of the right interleave.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 13.1 Known-bad controls for ORACLE 1 / ORACLE 2
# --------------------------------------------------------------------------- #


class TestTheReachabilityOracleCanActuallyGoRed:
    """Without these, `reachability_oracle` certifies a property it never checked.

    Each test builds ONE unreachable shape with raw SQL, calls the oracle's own
    helper directly, and asserts the bucket names the row it just made. The bad
    row is then removed inside the test, so the module-scoped database is handed
    back clean and no later test inherits a violation — and each carries
    `unreachable_by_design` so the autouse post-condition does not fire on the
    window in between.
    """

    @pytest.mark.unreachable_by_design
    def test_ORPHANED_a_group_balance_row_with_no_live_member_IS_REPORTED(
        self, papp, pgdb
    ):
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 4)
        pgdb.session.commit()

        clean = _reachability_report(pgdb)
        assert clean["orphaned_place_balances"] == [], (
            "a two-member place holding four bottles must be REACHABLE — if this "
            "is already dirty the control below proves nothing"
        )

        # Cut both members loose WITHOUT going through §7.3's dissolve. This is
        # the exact residue defect 9 produced: a balance row keyed to a group no
        # address points at any more.
        pgdb.session.execute(
            text("UPDATE addresses SET address_group_id = NULL WHERE address_group_id = :g"),
            {"g": group.id},
        )
        pgdb.session.commit()

        dirty = _reachability_report(pgdb)
        orphan_ids = [
            r[0]
            for r in pgdb.session.execute(
                text("SELECT id FROM bottle_balances WHERE address_group_id = :g"),
                {"g": group.id},
            ).all()
        ]
        assert orphan_ids, "setup: the place must own a balance row to orphan"
        assert set(orphan_ids) <= set(dirty["orphaned_place_balances"]), (
            "ORACLE 1's orphan bucket did NOT report a balance row whose group "
            "has zero live members. It cannot fail, so it certifies nothing."
        )
        assert _new_unreachable(clean, dirty)["orphaned_place_balances"] == sorted(orphan_ids)

        # Hand the database back exactly as clean as it was found.
        pgdb.session.execute(
            text("UPDATE addresses SET address_group_id = :g WHERE id IN (:a, :b)"),
            {"g": group.id, "a": a.id, "b": b.id},
        )
        pgdb.session.commit()
        assert _new_unreachable(clean, _reachability_report(pgdb)) == {
            "orphaned_place_balances": [],
            "stranded_address_balances": [],
            "stamp_incoherent_ledger_entries": [],
        }

    @pytest.mark.unreachable_by_design
    def test_STRANDED_an_address_row_whose_address_has_JOINED_a_place_IS_REPORTED(
        self, papp, pgdb
    ):
        owner = _user(pgdb)
        other = _user(pgdb)
        a, b = _addr(pgdb, owner), _addr(pgdb, other)
        admin = _admin(pgdb)
        _deliver(pgdb, owner, a, 3)
        group = _group(pgdb, admin, [a, b])
        pgdb.session.commit()

        clean = _reachability_report(pgdb)
        assert clean["stranded_address_balances"] == [], (
            "the join re-scoped A's own row onto the place (§7.2); if an "
            "address-keyed row survived the join this control is meaningless"
        )

        # The §7.2 shape: an address-keyed row for an address that is inside a
        # place. Every place-scoped read resolves PAST it, so its bottles are
        # invisible without being deleted.
        stranded_id = pgdb.session.execute(
            text(
                "INSERT INTO bottle_balances (address_id, address_group_id, balance, "
                "                             created_at, updated_at) "
                "VALUES (:a, NULL, 9.00, NOW(), NOW()) RETURNING id"
            ),
            {"a": a.id},
        ).scalar()
        pgdb.session.commit()

        dirty = _reachability_report(pgdb)
        assert stranded_id in dirty["stranded_address_balances"], (
            "ORACLE 1's stranded bucket did NOT report an address-keyed row at a "
            "GROUPED address — the §7.2 invisibility class, undetected"
        )
        assert _new_unreachable(clean, dirty)["stranded_address_balances"] == [stranded_id]

        pgdb.session.execute(
            text("DELETE FROM bottle_balances WHERE id = :i"), {"i": stranded_id}
        )
        pgdb.session.commit()
        assert stranded_id not in _reachability_report(pgdb)["stranded_address_balances"]

    @pytest.mark.unreachable_by_design
    def test_STAMP_INCOHERENCE_an_unstamped_ledger_row_at_a_GROUPED_address_IS_REPORTED(
        self, papp, pgdb
    ):
        """ORACLE 2's control — and the demonstration that ORACLE 1's balance
        buckets are blind to it. The balance side stays perfectly reachable
        throughout: only the LEDGER disagrees about which place saw the bottles.
        """
        owner, other = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, owner), _addr(pgdb, other)
        admin = _admin(pgdb)
        group = _group(pgdb, admin, [a, b])
        delivered = _deliver(pgdb, owner, a, 6)
        pgdb.session.commit()

        clean = _reachability_report(pgdb)
        assert clean["stamp_incoherent_ledger_entries"] == []

        # Defect 10's ledger half: an own-scope ledger row for an address whose
        # live pointer is the group.
        pgdb.session.execute(
            text("UPDATE bottle_ledger SET address_group_id = NULL WHERE id = :i"),
            {"i": delivered.id},
        )
        pgdb.session.commit()

        dirty = _reachability_report(pgdb)
        assert delivered.id in dirty["stamp_incoherent_ledger_entries"], (
            "ORACLE 2 did NOT report a ledger row stamped to no place at an "
            "address that IS in a place — defect 3's ledger half, undetected"
        )
        assert _new_unreachable(clean, dirty)["orphaned_place_balances"] == [], (
            "the point of ORACLE 2: the BALANCE side is untouched and reachable, "
            "so ORACLE 1 alone would have called this healthy"
        )
        assert _new_unreachable(clean, dirty)["stranded_address_balances"] == []

        pgdb.session.execute(
            text("UPDATE bottle_ledger SET address_group_id = :g WHERE id = :i"),
            {"g": group.id, "i": delivered.id},
        )
        pgdb.session.commit()
        assert delivered.id not in _reachability_report(pgdb)["stamp_incoherent_ledger_entries"]


# --------------------------------------------------------------------------- #
# 13.1a The ORACLE-OF-THE-ORACLE: the autouse WIRING is proven to fire
# --------------------------------------------------------------------------- #


class TestTheAutouseWiringItselfCanGoRed:
    """13.1 proves the three SQL buckets fire. It cannot prove the FIXTURE does.

    Every control in 13.1 carries `unreachable_by_design` — by construction they
    opt OUT of the autouse post-condition and call `_reachability_report`
    directly. So the thing that actually guards the other ~79 tests in this
    module — the fixture's assert, its second report call, and its marker
    polarity — is exercised by nothing in 13.1.

    That gap is not theoretical. Three one-line mutations leave 13.1 fully green
    while ORACLE 1 protects nothing:

      * drop the trailing `assert` — every test passes, forever;
      * INVERT the marker test (`is None`) — then the marked controls get the
        guard (they clean up, so they still pass) and every UNMARKED test is
        silently exempt. Green everywhere, guarding nothing. This is the nastiest
        of the three because the opt-out looks like it is working;
      * `yield` without re-reading — the delta is empty by construction.

    The two behavioural tests below drive the guard's real body through both of
    its branches; the third pins the polarity that no runtime assertion can
    catch, because its failure mode is universal green.
    """

    @pytest.mark.unreachable_by_design
    def test_the_GUARD_RAISES_for_an_UNMARKED_test_that_strands_a_balance_row(
        self, papp, pgdb
    ):
        """The path every unmarked test in this module runs under, driven by hand.

        `next()` is called exactly where pytest calls it: once for the baseline
        before the test body, once for the post-condition after it.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 2)
        pgdb.session.commit()

        guard = _reachability_guard(False, pgdb)
        next(guard)  # pytest's setup half: the baseline

        pgdb.session.execute(
            text("UPDATE addresses SET address_group_id = NULL WHERE address_group_id = :g"),
            {"g": group.id},
        )
        pgdb.session.commit()
        orphan_ids = [
            r[0]
            for r in pgdb.session.execute(
                text("SELECT id FROM bottle_balances WHERE address_group_id = :g"),
                {"g": group.id},
            ).all()
        ]
        assert orphan_ids, "setup: the place must own a balance row to orphan"

        with pytest.raises(AssertionError) as raised:
            next(guard)  # pytest's teardown half: the post-condition

        message = str(raised.value)
        assert "ORACLE 1" in message, (
            "the guard raised, but not with the oracle's own message — a bare "
            f"AssertionError here is unattributable at 3am: {message[:200]}"
        )
        assert "orphaned_place_balances" in message
        assert str(orphan_ids[0]) in message, (
            "the failure names no ROW ID. The message is the only artefact a "
            f"red run leaves behind: {message[:400]}"
        )

        pgdb.session.execute(
            text("UPDATE addresses SET address_group_id = :g WHERE id IN (:a, :b)"),
            {"g": group.id, "a": a.id, "b": b.id},
        )
        pgdb.session.commit()

    @pytest.mark.unreachable_by_design
    def test_the_GUARD_stays_SILENT_on_the_SAME_damage_when_the_test_is_EXEMPT(
        self, papp, pgdb
    ):
        """The opt-out half. Identical damage, marker branch: no raise.

        Without this, `unreachable_by_design` could be a no-op that happens to
        be tolerated because 13.1's controls tidy up after themselves — and the
        first control that genuinely could not tidy up would fail mysteriously.
        """
        admin = _admin(pgdb)
        oa, ob = _user(pgdb), _user(pgdb)
        a, b = _addr(pgdb, oa), _addr(pgdb, ob)
        group = _group(pgdb, admin, [a, b])
        _deliver(pgdb, oa, a, 2)
        pgdb.session.commit()

        guard = _reachability_guard(True, pgdb)
        next(guard)

        pgdb.session.execute(
            text("UPDATE addresses SET address_group_id = NULL WHERE address_group_id = :g"),
            {"g": group.id},
        )
        pgdb.session.commit()
        assert _reachability_report(pgdb)["orphaned_place_balances"], (
            "setup: the damage this exempt run must IGNORE has to actually exist, "
            "or the silence below proves nothing"
        )

        with pytest.raises(StopIteration):
            next(guard)

        pgdb.session.execute(
            text("UPDATE addresses SET address_group_id = :g WHERE id IN (:a, :b)"),
            {"g": group.id, "a": a.id, "b": b.id},
        )
        pgdb.session.commit()

    def test_the_FIXTURE_maps_the_MARKER_to_EXEMPT_and_not_the_INVERSE(self):
        """A shape pin, because this specific bug has NO runtime signature.

        Both tests above pass whichever way the fixture wires the marker: they
        select the branch themselves. An inverted fixture — guard the marked
        tests, exempt the unmarked ones — is green across this entire module and
        across every future test added to it, while ORACLE 1 checks nothing.
        There is no assertion that can observe that from inside a test run, so
        the polarity is pinned on the source. Same idiom as
        `test_bottle_place_lock_order.py`, for the same reason.
        """
        from pathlib import Path

        source = Path(__file__).read_text(encoding="utf-8")
        _, after = source.split("def reachability_oracle(request, pgdb):", 1)
        body = after.split("\n@pytest.fixture", 1)[0]

        assert "yield from _reachability_guard(" in body, (
            "the autouse fixture no longer delegates to `_reachability_guard`, so "
            "the two tests above are exercising a COPY of the post-condition and "
            "not the one the module actually runs under"
        )
        assert 'get_closest_marker("unreachable_by_design") is not None' in body, (
            "the fixture's exempt argument is not `marker is not None`. If this "
            "was inverted, every UNMARKED test in this module is silently exempt "
            "from ORACLE 1 and the whole suite stays green while checking nothing."
        )


# --------------------------------------------------------------------------- #
# 13.2 The deadlock counter — read from Postgres itself, and proven to MOVE
# --------------------------------------------------------------------------- #


def _deadlock_count(conn) -> int:
    """`pg_stat_database.deadlocks` for THIS database, as an integer.

    Two details that decide whether this number means anything:

    * `pg_stat_clear_snapshot()` — statistics are snapshotted per transaction
      and the snapshot is CACHED, so re-reading inside one transaction returns
      the first value forever. The caller polls; without this the poll is a
      loop over a constant.
    * `datname = current_database()` — the module's ephemeral database, not the
      whole cluster. Every other test in this container has its own database,
      so their deadlocks (if any) cannot leak into this delta, and ours cannot
      leak into theirs.

    A missing row would make `.fetchone()` None and the assertion vacuous; the
    caller asserts on an int, and 13.2 below proves the number actually moves.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_stat_clear_snapshot()")
        cur.execute("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
        row = cur.fetchone()
    assert row is not None, (
        "pg_stat_database has no row for the current database — the deadlock "
        "oracle would be reading nothing at all"
    )
    return int(row[0])


def _settled_deadlock_count(conn, *, at_least: int | None = None, timeout: float = 6.0) -> int:
    """Poll the counter until it stops moving (or reaches ``at_least``).

    Backends report their statistics to the shared collector with a minimum
    interval (1s on the Postgres this runs on), so reading the counter the
    instant the soak's last thread joins can miss a deadlock that HAS already
    happened. A snapshot taken too early is the difference between an oracle and
    a decoration, so this waits for the number to settle instead.
    """
    deadline = time.monotonic() + timeout
    seen = _deadlock_count(conn)
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        if at_least is not None and seen >= at_least:
            return seen
        time.sleep(0.25)
        now = _deadlock_count(conn)
        if now != seen:
            seen, stable_since = now, time.monotonic()
        elif at_least is None and time.monotonic() - stable_since >= 1.5:
            break
    return seen


class TestTheDeadlockOracleCanActuallyGoRed:
    """The soak's headline assertion is `deadlocks delta == 0`. A counter that
    never moves would satisfy that forever — including on the very 40P01 this
    ladder was built to remove. This induces a real one and watches it move.
    """

    def test_CONTROL_a_deliberate_ABBA_on_two_addresses_rows_MOVES_the_counter(
        self, papp, pgdb, raw
    ):
        owner = _user(pgdb)
        a, b = _addr(pgdb, owner), _addr(pgdb, owner)
        lo, hi = sorted((a.id, b.id))

        meter = raw(autocommit=True)
        before = _deadlock_count(meter)

        one, two = raw(), raw()
        took_lo, took_hi = threading.Event(), threading.Event()
        outcome: dict = {}

        def hold_then_cross(conn, first, second, mine, theirs, key):
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM addresses WHERE id = %s FOR UPDATE", (first,))
                    cur.fetchall()
                mine.set()
                assert theirs.wait(timeout=20), "the other side never took its row"
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM addresses WHERE id = %s FOR UPDATE", (second,))
                    cur.fetchall()
                conn.commit()
                outcome[key] = "committed"
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome[key] = exc
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass

        t1 = threading.Thread(
            target=hold_then_cross, args=(one, lo, hi, took_lo, took_hi, "lo_first"), daemon=True
        )
        t2 = threading.Thread(
            target=hold_then_cross, args=(two, hi, lo, took_hi, took_lo, "hi_first"), daemon=True
        )
        t1.start(); t2.start()
        t1.join(timeout=40); t2.join(timeout=40)

        victims = [
            v for v in outcome.values()
            if isinstance(v, BaseException) and getattr(v, "pgcode", None) == "40P01"
        ]
        assert victims, (
            "no 40P01 was raised by a textbook ABBA on two `addresses` rows. "
            f"Outcomes: {outcome}. Either the two threads never actually crossed "
            "or this Postgres is not detecting deadlocks — in both cases the "
            "soak's `delta == 0` assertion is worthless."
        )

        after = _settled_deadlock_count(meter, at_least=before + 1)
        assert after > before, (
            f"a REAL deadlock (40P01: {victims[0]}) did not move "
            f"pg_stat_database.deadlocks ({before} -> {after}). The soak's "
            "headline oracle reads a number that cannot change."
        )


# --------------------------------------------------------------------------- #
# 13.3 ORACLE 5 — THE RANDOMISED SOAK
# --------------------------------------------------------------------------- #
#
# Design §6.5, quoted because the rationale is the load-bearing part:
#
#   "K threads over a small pool of addresses and places issuing random
#    deliveries, returns, collections, adjustments, joins, removals and
#    reconciles for N seconds; then assert, in this order: (a)
#    `pg_stat_database.deadlocks` delta for the test database is exactly 0; (b)
#    zero unexpected 500s — named ValidationErrors are the expected loser
#    outcome; (c) oracle 1 empty; (d) oracle 2 clean; (e) per-scope
#    `stored == ledger_sum`; (f) Σ conserved. (e) and (f) go last, precisely
#    because they are the weak ones. Targeted races prove the races you thought
#    of; the soak is what finds the ordering nobody wrote down — which is
#    exactly how defect 2 got here."
#
# EVERY OTHER ORDERING CLAIM ABOUT THIS LADDER RESTS ON ENUMERATION — somebody
# read the paths and reasoned the order is consistent. That argument was
# already proven false once here: "of two concurrent transactions on one
# address, exactly one passes its fence" was accepted by multiple reviewers
# before an e2e test disproved it. This test is the one that does not need
# anyone to have guessed right.
#
# It is therefore built to be able to FAIL:
#   * the operation mix is RANDOM, not a script, and the seed is printed on
#     failure so any red run replays exactly;
#   * every thread runs REAL service methods in REAL transactions on a SMALL
#     shared pool, so the interleaves are genuinely contended;
#   * the deadlock count comes from Postgres, not from counting exceptions,
#     and 13.2 proves that number moves;
#   * `test_the_soak_HARNESS_itself_does_work` refuses a run that quietly did
#     nothing — the way a soak most often "passes".
# --------------------------------------------------------------------------- #

SOAK_SECONDS = float(os.environ.get("PLACE_SOAK_SECONDS", "8"))
SOAK_THREADS = int(os.environ.get("PLACE_SOAK_THREADS", "6"))
SOAK_ADDRESSES = int(os.environ.get("PLACE_SOAK_ADDRESSES", "6"))


class _SoakResult:
    def __init__(self, seed):
        self.seed = seed
        self.completed = collections.Counter()
        self.refused = collections.Counter()
        self.unexpected: list = []
        self.net_movement = Decimal("0.00")
        self.corrections: list = []
        self.lock = threading.Lock()

    def ok(self, op, movement=Decimal("0.00")):
        with self.lock:
            self.completed[op] += 1
            self.net_movement += movement

    def refusal(self, op, code):
        with self.lock:
            self.refused[f"{op}:{code}"] += 1

    def blew_up(self, op, exc):
        with self.lock:
            self.unexpected.append((op, type(exc).__name__, str(exc)[:400]))

    def correction(self, address_id, report):
        with self.lock:
            self.corrections.append((address_id, report))

    @property
    def total_completed(self):
        return sum(self.completed.values())


def _soak_worker(app, pool, admin_id, result, deadline, seed):
    """One thread's random walk. Own app context, own session, own RNG stream."""
    import random as _random

    from business_app import db as _db

    rng = _random.Random(seed)
    service = BottleTrackingService()
    links = CustomerLinkService()

    def a_random_address():
        return rng.choice(pool)

    def qty():
        return D(rng.choice(["1.00", "2.00", "0.50", "3.00"]))

    with app.app_context():
        while time.monotonic() < deadline:
            op = rng.choices(
                ["deliver", "give_back", "collect", "adjust", "group", "ungroup", "reconcile"],
                weights=[22, 16, 12, 12, 16, 16, 6],
            )[0]
            addr_id, owner_id = a_random_address()
            try:
                if op == "deliver":
                    order = Order(
                        user_id=owner_id,
                        order_number=f"ORD-SOAK-{uuid.uuid4().hex[:14]}",
                        status=OrderStatus.DELIVERED,
                        subtotal=Decimal("0.00"),
                        delivery_fee=Decimal("0.00"),
                        discount_amount=Decimal("0.00"),
                        loyalty_discount=Decimal("0.00"),
                        total_amount=Decimal("0.00"),
                        delivery_address_id=addr_id,
                        created_at=datetime.now(UTC),
                    )
                    _db.session.add(order)
                    _db.session.flush()
                    entry = service.record_bottles_delivered(
                        order.id, owner_id, addr_id, qty()
                    )
                    moved = D(entry.quantity)
                    _db.session.commit()
                    result.ok(op, moved)

                elif op == "give_back":
                    entry = service.record_bottles_returned(owner_id, addr_id, qty())
                    moved = D(entry.quantity)
                    _db.session.commit()
                    result.ok(op, moved)

                elif op == "collect":
                    entry = service.record_standalone_collection(
                        owner_id, addr_id, qty(), actor_user_id=admin_id
                    )
                    moved = D(entry.quantity)
                    _db.session.commit()
                    result.ok(op, moved)

                elif op == "adjust":
                    delta = qty() * (1 if rng.random() < 0.5 else -1)
                    entry = service.admin_adjust_balance(
                        user_id=None,
                        address_id=addr_id,
                        adjustment=delta,
                        actor_user_id=admin_id,
                        notes="soak",
                    )
                    moved = D(entry.quantity)
                    _db.session.commit()
                    result.ok(op, moved)

                elif op == "group":
                    partner_id, _ = a_random_address()
                    if partner_id == addr_id:
                        continue
                    # The read below is DELIBERATELY unlocked and may be stale
                    # by the time the service runs: that staleness is half the
                    # point. The loser gets a NAMED refusal, never a 500.
                    existing = _db.session.execute(
                        text("SELECT address_group_id FROM addresses WHERE id = :i"),
                        {"i": addr_id},
                    ).scalar()
                    if existing is None:
                        links.create_place_group(
                            [addr_id, partner_id], acting_admin_id=admin_id, reason="soak"
                        )
                    else:
                        links.add_addresses_to_group(
                            existing, [partner_id], acting_admin_id=admin_id, reason="soak"
                        )
                    _db.session.commit()
                    result.ok(op)

                elif op == "ungroup":
                    leaving = rng.choice([None, D("1.00")])
                    links.remove_address_from_group(
                        addr_id,
                        acting_admin_id=admin_id,
                        reason="soak",
                        **({} if leaving is None else {"bottles_leaving": leaving}),
                    )
                    _db.session.commit()
                    result.ok(op)

                elif op == "reconcile":
                    report = service.reconcile_balance(addr_id)
                    _db.session.commit()
                    if report.get("corrected"):
                        result.correction(addr_id, report)
                    result.ok(op)

            except ValidationError as refusal:
                # The EXPECTED loser outcome. `error_code` is the machine
                # contract the admin UI branches on, so it is what gets counted
                # — a refusal that arrives with no code is itself a finding.
                _db.session.rollback()
                result.refusal(op, getattr(refusal, "error_code", None) or "UNCODED")
            except BaseException as exc:  # noqa: BLE001 - the whole point of (b)
                try:
                    _db.session.rollback()
                except Exception:  # noqa: BLE001
                    pass
                result.blew_up(op, exc)
        _db.session.remove()


class TestTheRandomisedSoak:
    """§6.5. One soak run, six assertions, in the design's order."""

    @pytest.fixture(scope="class")
    def soak(self, place_pg_app, place_pg_url):
        """Run the soak ONCE and share the outcome with every assertion below.

        Class-scoped on purpose: the run is the expensive part and every
        assertion interrogates the SAME run. Splitting it into six soaks would
        multiply the wall clock and, worse, let five of them pass on a run where
        the sixth found something.
        """
        from business_app import db as _db

        seed = int(os.environ.get("PLACE_SOAK_SEED", uuid.uuid4().int % (2 ** 31)))

        with place_pg_app.app_context():
            admin = _admin(_db)
            pool = []
            for _ in range(SOAK_ADDRESSES):
                owner = _user(_db)
                address = _addr(_db, owner)
                pool.append((address.id, owner.id))
            admin_id = admin.id
            _db.session.commit()

            # An INDEPENDENT connection, autocommit: `pg_stat_database` is
            # snapshotted per transaction, so the meter must never share the
            # soak's. Built from the module fixture's URL, never from a literal.
            meter = psycopg2.connect(place_pg_url)
            meter.autocommit = True
            try:
                deadlocks_before = _deadlock_count(meter)
                baseline = _reachability_report(_db)
                opening_total = _soak_sigma(_db, pool)

                result = _SoakResult(seed)
                deadline = time.monotonic() + SOAK_SECONDS
                threads = [
                    threading.Thread(
                        target=_soak_worker,
                        args=(place_pg_app, pool, admin_id, result, deadline, seed + i),
                        name=f"soak-{i}",
                        daemon=True,
                    )
                    for i in range(SOAK_THREADS)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=SOAK_SECONDS + 120)
                assert not any(t.is_alive() for t in threads), (
                    f"a soak thread never finished (seed={seed}) — a hung "
                    "transaction is itself a locking finding, not a flake"
                )

                deadlocks_after = _settled_deadlock_count(meter)
            finally:
                meter.close()

            _db.session.rollback()
            _db.session.expire_all()
            yield {
                "seed": seed,
                "pool": pool,
                "result": result,
                "deadlocks_before": deadlocks_before,
                "deadlocks_after": deadlocks_after,
                "baseline": baseline,
                "after": _reachability_report(_db),
                "opening_total": opening_total,
                "closing_total": _soak_sigma(_db, pool),
            }
            _db.session.remove()

    # (a) ------------------------------------------------------------------ #
    def test_a_the_DEADLOCK_count_for_this_database_did_not_move(self, soak):
        """The single most direct evidence the confirmed 40P01 is gone.

        Read from Postgres, not inferred from exceptions: a deadlock the
        service swallowed and retried would still be counted here. 13.2 proves
        this number moves when a real deadlock happens.
        """
        delta = soak["deadlocks_after"] - soak["deadlocks_before"]
        assert delta == 0, (
            f"{delta} DEADLOCK(S) during the soak (seed={soak['seed']}, "
            f"{soak['result'].total_completed} operations, "
            f"{SOAK_THREADS} threads x {SOAK_SECONDS}s over "
            f"{len(soak['pool'])} addresses).\n"
            f"Completed: {dict(soak['result'].completed)}\n"
            f"Unexpected errors: {soak['result'].unexpected[:5]}\n"
            "Re-run with PLACE_SOAK_SEED to replay. The ladder claims a single "
            "total lock order; a deadlock is a counterexample to that claim, "
            "and it is an ordering NOBODY WROTE DOWN — which is exactly how the "
            "original 40P01 got here."
        )

    # (b) ------------------------------------------------------------------ #
    def test_b_every_LOSER_was_refused_by_NAME_and_nothing_else_blew_up(self, soak):
        """`ValidationError` with an `error_code` is the sanctioned loser
        outcome (design §7: "the second of two concurrent removals gets a named
        refusal where it used to 500 on a deadlock kill rendered as
        `ExternalServiceError`"). Anything else is a 500 in production."""
        result = soak["result"]
        assert result.unexpected == [], (
            f"{len(result.unexpected)} unexpected failure(s) during the soak "
            f"(seed={soak['seed']}). These are 500s on the driver app and the "
            f"admin dashboard:\n" + "\n".join(
                f"  {op}: {kind}: {msg}" for op, kind, msg in result.unexpected[:10]
            )
        )
        assert "UNCODED" not in "".join(result.refused), (
            "a refusal arrived with no `error_code`. The admin UI branches on "
            f"that field; a coderless refusal is unactionable: {dict(result.refused)}"
        )

    # (c) ------------------------------------------------------------------ #
    def test_c_ORACLE_1_is_empty_no_bottle_became_UNREACHABLE(self, soak):
        new = _new_unreachable(soak["baseline"], soak["after"])
        assert new["orphaned_place_balances"] == [], (
            f"the soak stranded balance rows on groups with no live members: "
            f"{new['orphaned_place_balances']} (seed={soak['seed']})"
        )
        assert new["stranded_address_balances"] == [], (
            f"the soak left address-keyed rows at addresses that are now inside "
            f"a place: {new['stranded_address_balances']} (seed={soak['seed']})"
        )

    # (d) ------------------------------------------------------------------ #
    def test_d_ORACLE_2_is_clean_no_ledger_row_lost_its_PLACE(self, soak):
        new = _new_unreachable(soak["baseline"], soak["after"])
        assert new["stamp_incoherent_ledger_entries"] == [], (
            "ledger rows stamped to NO place whose address IS in one: "
            f"{new['stamp_incoherent_ledger_entries']} (seed={soak['seed']}). "
            "The balance side can be perfectly reachable while the place's "
            "history silently loses a delivery."
        )

    # (e) ------------------------------------------------------------------ #
    def test_e_every_touched_scope_agrees_stored_equals_LEDGER_SUM(self, soak, pgdb):
        """Deliberately near-last: this is one of the two WEAK oracles. Every
        defect this file confirmed satisfies it.

        The reconcile results are asserted alongside, and they are the sharper
        half: `reconcile_balance` REPAIRS drift, so a soak that drifted and then
        happened to reconcile that place would show a clean (e) afterwards. A
        reconcile that reports `corrected` at all means a drift existed.
        """
        result = soak["result"]
        assert result.corrections == [], (
            "reconcile_balance found and CORRECTED drift during the soak — the "
            "drift existed, and only the repair hid it from the equality below: "
            f"{result.corrections[:5]} (seed={soak['seed']})"
        )
        drifted = []
        for scope in _soak_scopes(pgdb, soak["pool"]):
            stored, ledger = _pair(pgdb, scope)
            if stored != ledger:
                drifted.append((scope, stored, ledger))
        assert drifted == [], (
            f"per-scope stored != SUM(ledger) after the soak (seed={soak['seed']}): "
            f"{drifted}"
        )

    # (f) ------------------------------------------------------------------ #
    def test_f_SIGMA_over_the_pool_equals_the_movements_actually_RECORDED(self, soak):
        """The weakest oracle of the six, and it is last for that reason: every
        one of the thirteen defects this file confirmed CONSERVES.

        It is still not free: joins, splits and dissolves move bottles between
        scopes and must move NONE in or out, so a lifecycle operation that
        minted or destroyed a bottle shows up here and nowhere else. The
        expected figure is built from the quantity each ledger entry actually
        recorded, not from what the soak asked for.
        """
        expected = D(soak["opening_total"] + soak["result"].net_movement)
        assert D(soak["closing_total"]) == expected, (
            f"Sigma over the soak's scopes is {D(soak['closing_total'])}, but the "
            f"{soak['result'].total_completed} completed operations recorded "
            f"{soak['result'].net_movement} of movement on an opening "
            f"{soak['opening_total']} (seed={soak['seed']}). A join, split or "
            "dissolve minted or destroyed bottles."
        )

    # meta ------------------------------------------------------------------ #
    def test_the_soak_HARNESS_itself_does_work_and_exercises_the_LIFECYCLE(self, soak):
        """The way a soak most often "passes" is by doing nothing.

        A soak whose threads all died on the first call, or whose every
        lifecycle attempt was refused, would satisfy (a)-(f) trivially. This
        refuses such a run outright. The thresholds are deliberately low — this
        is a floor against a silently dead harness, not a performance
        assertion.
        """
        result = soak["result"]
        assert result.total_completed >= 20, (
            f"only {result.total_completed} operations completed in "
            f"{SOAK_SECONDS}s across {SOAK_THREADS} threads — the soak did not "
            f"exercise anything. Completed: {dict(result.completed)}; "
            f"refused: {dict(result.refused)}"
        )
        assert result.completed["deliver"] > 0, "no delivery ever completed"
        lifecycle = result.completed["group"] + result.completed["ungroup"]
        assert lifecycle > 0, (
            "not one join or removal COMPLETED, so the soak never contended the "
            f"lifecycle rungs at all. Refused: {dict(result.refused)}"
        )
        assert result.refused, (
            "not a single operation was refused across the whole soak. On a "
            "shared pool of six addresses that means the threads never actually "
            "collided — check the harness before trusting the green above."
        )
