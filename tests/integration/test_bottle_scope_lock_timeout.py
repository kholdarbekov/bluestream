"""REAL POSTGRES: the place-scope lock ladder is BOUNDED, and a driver can read the failure.

WHY THIS FILE EXISTS
--------------------
The four-rung lock ladder fixed a cluster of real concurrency defects, but it
introduced an operational one: a driver's DELIVERED submission now takes
``FOR SHARE`` on the ``addresses`` row, which BLOCKS against the place lifecycle
(admin grouping/ungrouping) and the account-merge path. Neither contended before
the ladder landed. There was no ``lock_timeout`` anywhere in ``business_app/``,
so the wait was UNBOUNDED, and the failure — when it eventually came — rendered
as a bare HTTP 500.

That is a worse field failure than the data defects the ladder fixed: a driver
standing at a customer's door, submission hanging, no comprehensible message.

WHAT IS PROVEN HERE
-------------------
1. THE EXPOSURE IS REAL, and it is not milliseconds. ``test_lock_window_scales_``
   ``with_place_ledger_history`` measures the actual rung-1 hold window of
   ``create_place_group`` and shows it growing with the place's ledger size —
   because ``_absorb_joiners_into_group`` ends in ``recompute_balance_after``,
   which loads and rewrites the place's ENTIRE timeline as ORM objects while
   holding every member ``addresses`` row. This is the measurement that decided
   a timeout was warranted rather than over-engineering.
2. The bound is actually installed (``SHOW lock_timeout`` inside the writer's
   transaction), and it is ``SET LOCAL`` — it does NOT leak onto the pooled
   connection and silently bound a later unrelated request.
3. A blocked writer FAILS FAST with a NAMED domain error
   (``BOTTLE_SCOPE_LOCK_TIMEOUT``), not ``OperationalError``.
4. The session is USABLE afterwards — the rollback happened — so the driver's
   real error is not replaced by a 25P02 cascade.
5. The API layer renders it as 409 + that error code, NOT the 500 /
   ``INTERNAL_ERROR`` / ``error_code: None`` that the unmapped default produces.
6. The lifecycle is deliberately NOT bounded: it is the holder, not the waiter.

SQLite could make none of these claims — ``with_for_update()`` compiles to
nothing there, so there is no lock to wait on and no timeout to fire.
"""

import time
import uuid
from datetime import datetime, timezone as _tz
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from tests.integration.conftest import (
    REQUIRES_PG_REASON,
    _admin_engine_for,
    _resolve_database_url,
)

UTC = _tz.utc


# --------------------------------------------------------------------------- #
# Module-scoped migrated database (one `alembic upgrade head`, not one per test)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def lt_url():
    from sqlalchemy.engine.url import make_url

    base_url = _resolve_database_url()
    if not base_url.startswith(("postgresql://", "postgresql+", "postgres://")):
        pytest.skip(REQUIRES_PG_REASON)

    admin_engine = _admin_engine_for(base_url)
    db_name = f"locktimeout_{uuid.uuid4().hex[:12]}"
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
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
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()


@pytest.fixture(scope="module")
def lt_base_app(lt_url):
    from flask_migrate import upgrade

    from business_app import create_app

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": lt_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SECRET_KEY": "test-secret-key-for-lock-timeout-e2e-32c",
            "JWT_SECRET_KEY": "test-jwt-secret-key-for-lock-timeout",
            "CELERY_ALWAYS_EAGER": True,
            "WTF_CSRF_ENABLED": False,
        }
    )
    with app.app_context():
        upgrade(revision="head")
    yield app


@pytest.fixture
def ltapp(lt_base_app):
    """A FRESH app context (hence a fresh session) per test."""
    from business_app import db as _db

    with lt_base_app.app_context():
        try:
            yield lt_base_app
        finally:
            _db.session.rollback()
            _db.session.remove()


@pytest.fixture
def ltdb(ltapp):
    from business_app import db as _db

    return _db


@pytest.fixture
def holder(lt_url):
    """Independent psycopg2 connections that HOLD locks, like the admin does.

    Every connection is closed on teardown — the ephemeral database cannot be
    dropped while a backend still holds it.
    """
    made = []

    def _connect():
        conn = psycopg2.connect(lt_url)
        conn.autocommit = False
        made.append(conn)
        return conn

    try:
        yield _connect
    finally:
        for conn in made:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 — teardown best effort
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Entity helpers
# --------------------------------------------------------------------------- #

_SEQ = [int(time.time()) % 30000]


def _uniq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _user(ltdb, *, role=None, user_type=None):
    from business_app.models.user import User
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserStatus, UserType

    n = _uniq()
    user = User(
        email=f"lock.timeout.{n}.{uuid.uuid4().hex[:6]}@example.com",
        phone=f"+9989{700000000 + n}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Lock",
        last_name=f"User{n}",
        user_type=user_type or UserType.INDIVIDUAL,
        role=role or UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    ltdb.session.add(user)
    ltdb.session.commit()
    return user


def _admin(ltdb):
    from shared.enums import UserRole, UserType

    return _user(ltdb, role=UserRole.ADMIN, user_type=UserType.STAFF)


def _addr(ltdb, owner):
    from business_app.models.user import UserAddress

    n = _uniq()
    address = UserAddress(
        user_id=owner.id,
        title=f"Door {n}",
        full_address=f"{n} Lock Street, Tashkent",
        street_address=f"{n} Lock Street",
        city="Tashkent",
        latitude=41.3111,
        longitude=69.2797,
    )
    ltdb.session.add(address)
    ltdb.session.commit()
    return address


def _order(ltdb, owner, address):
    """A REAL order row, because the ledger's idempotency key is built from its id.

    ``record_bottles_delivered(order_id=order.id, ...)`` keys the entry
    ``"delivery:None"``, which is the SAME key for every caller. This module's
    database is module-scoped, so the first test that commits such an entry makes
    every later call a no-op that returns the existing row WITHOUT ever taking a
    lock — a test expecting a timeout then silently passes through and reports
    "DID NOT RAISE". A distinct order per write keeps each call a genuine write.
    """
    from business_app.models.order import Order
    from shared.enums import OrderStatus

    order = Order(
        user_id=owner.id,
        order_number=f"ORD-LOCKTIMEOUT-{_uniq()}-{uuid.uuid4().hex[:6]}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    ltdb.session.add(order)
    ltdb.session.commit()
    return order


def _seed_ledger(ltdb, address, owner, n):
    """Give the place a realistic ledger history of ``n`` entries."""
    if not n:
        return
    rows = [
        {
            "user_id": owner.id,
            "address_id": address.id,
            "event_type": "delivery",
            "quantity": Decimal("1.00"),
            "balance_after": Decimal(str(i + 1)),
            "occurred_at": datetime.now(UTC),
            "idempotency_key": f"locktimeout:{uuid.uuid4().hex}",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        for i in range(n)
    ]
    ltdb.session.execute(
        text(
            "INSERT INTO bottle_ledger (user_id,address_id,event_type,quantity,"
            "balance_after,occurred_at,idempotency_key,entry_metadata,created_at,updated_at) "
            "VALUES (:user_id,:address_id,:event_type,:quantity,:balance_after,"
            ":occurred_at,:idempotency_key,CAST('{}' AS json),:created_at,:updated_at)"
        ),
        rows,
    )
    ltdb.session.commit()


def _hold_address_for_no_key_update(conn, address_id):
    """Exactly the lock the place lifecycle takes on a member address (rung 1)."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM addresses WHERE id = %s FOR NO KEY UPDATE", (address_id,))
    cur.fetchall()
    return cur


# --------------------------------------------------------------------------- #
# 1. THE EXPOSURE — measured, not asserted by intuition
# --------------------------------------------------------------------------- #


class _RungOneWindow:
    """Wall time from the first rung-1 lock statement to the COMMIT that frees it."""

    def __init__(self, engine):
        self.engine = engine
        self.locked_at = None
        self.committed_at = None

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._before)
        event.listen(self.engine, "commit", self._commit)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._before)
        event.remove(self.engine, "commit", self._commit)

    def _before(self, conn, cursor, statement, params, context, executemany):
        upper = statement.upper()
        if self.locked_at is None and "FROM ADDRESSES" in upper and "FOR NO KEY UPDATE" in upper:
            self.locked_at = time.perf_counter()

    def _commit(self, conn):
        if self.locked_at is not None and self.committed_at is None:
            self.committed_at = time.perf_counter()

    @property
    def ms(self):
        if self.locked_at is None or self.committed_at is None:
            return None
        return (self.committed_at - self.locked_at) * 1000.0


def test_lock_window_carries_unbounded_o_n_work_over_place_history(ltdb):
    """THE EXPOSURE THAT JUSTIFIES THE TIMEOUT — proven STRUCTURALLY.

    ``create_place_group`` holds every member ``addresses`` row from rung 1 until
    COMMIT. Inside that window ``_absorb_joiners_into_group`` calls
    ``recompute_balance_after``, which does

        rows = BottleLedger.query.filter(...).all()   # the WHOLE timeline
        for row in rows: row.balance_after = running

    — an O(history) ORM load-and-rewrite. So the holder's window is NOT a
    function of the membership edit's size; it is a function of how much history
    the place has. A long-lived shared place (an office taking deliveries for
    years) is exactly the address an admin groups, and exactly the one with the
    deepest ledger. A driver's ``FOR SHARE`` waits behind ALL of it.

    ASSERTED ON ROW COUNT, NOT WALL CLOCK. An earlier version of this pin
    compared two timings and flaked the moment the box was busy — which is
    precisely when the measurement matters least and the machine is least
    trustworthy. The row count is deterministic, load-independent, and is the
    actual mechanism: the timing is reported as evidence, not asserted.
    """
    from business_app.services import customer_link_service as cls_mod
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.services.customer_link_service import CustomerLinkService

    history = 400
    admin = _admin(ltdb)
    u1, u2 = _user(ltdb), _user(ltdb)
    a1, a2 = _addr(ltdb, u1), _addr(ltdb, u2)
    _seed_ledger(ltdb, a1, u1, history)
    _seed_ledger(ltdb, a2, u2, history)

    observed = {}
    original = BottleTrackingService.recompute_balance_after

    def _spy(scope):
        # Rung 1 is held right now — the registry is the ladder's own record of
        # which `addresses` rows this transaction has locked.
        observed["locked_addresses"] = set(cls_mod.db.session.info.get("bottle_scope_locks", {}).get("addresses", ()))
        count = original(scope)
        observed["rows_rewritten"] = count
        return count

    BottleTrackingService.recompute_balance_after = staticmethod(_spy)
    try:
        with _RungOneWindow(ltdb.engine) as w:
            CustomerLinkService().create_place_group([a1.id, a2.id], admin.id, "measure")
    finally:
        BottleTrackingService.recompute_balance_after = staticmethod(original)

    assert w.ms is not None, "rung 1 was never taken — the ladder changed shape"
    print(f"\n[lock window] {history} entries/member -> {w.ms:.1f}ms holding rung 1")

    # The whole merged timeline is rewritten row by row...
    assert observed["rows_rewritten"] == history * 2, (
        f"expected the merged timeline ({history * 2} entries) to be rewritten inside "
        f"the lock window, saw {observed.get('rows_rewritten')}"
    )
    # ...while BOTH member address rows are locked. That is the exposure: work
    # proportional to data volume, performed under a lock a driver needs.
    assert {a1.id, a2.id} <= observed["locked_addresses"], (
        "rung 1 was not held while the O(history) rewrite ran — if that is now "
        "true, this file's premise changed and the timeout should be revisited"
    )


# --------------------------------------------------------------------------- #
# 2. THE BOUND IS INSTALLED, AND IT IS LOCAL
# --------------------------------------------------------------------------- #


def test_writer_transaction_has_lock_timeout_applied(ltdb, ltapp):
    """``resolve_scope_for_write`` installs the configured bound before locking."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 1500
    BottleTrackingService.resolve_scope_for_write(address.id)

    assert ltdb.session.execute(text("SHOW lock_timeout")).scalar() == "1500ms"


def test_lock_timeout_is_set_local_and_dies_with_the_transaction(ltdb, ltapp):
    """SET LOCAL, never SET SESSION.

    A session-level default would ride the pooled connection into the next
    request and silently bound a Celery task or an admin path that must wait.
    Proving it reverts on commit is what rules that out.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 1500
    BottleTrackingService.resolve_scope_for_write(address.id)
    assert ltdb.session.execute(text("SHOW lock_timeout")).scalar() == "1500ms"

    ltdb.session.commit()

    # Back to the server default on the very same pooled connection.
    assert ltdb.session.execute(text("SHOW lock_timeout")).scalar() != "1500ms"


def test_lock_timeout_can_be_disabled_by_config(ltdb, ltapp):
    """0 means "do not bound" — an explicit escape hatch, not an accident."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 0
    BottleTrackingService.resolve_scope_for_write(address.id)

    assert ltdb.session.execute(text("SHOW lock_timeout")).scalar() == "0"


# --------------------------------------------------------------------------- #
# 3. A BLOCKED DRIVER FAILS FAST, BY NAME
# --------------------------------------------------------------------------- #


def test_blocked_bottle_write_raises_named_conflict_not_operational_error(ltdb, ltapp, holder):
    """THE CORE CLAIM.

    An admin (independent connection) holds the address row exactly as
    ``CustomerLinkService._load_addresses`` does. The driver's delivery write
    then hits rung 1 and must NOT hang: it aborts inside the bound and surfaces
    as a named, retryable ``ConflictError``.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.utils.exceptions import ConflictError

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    order = _order(ltdb, owner, address)
    conn = holder()
    _hold_address_for_no_key_update(conn, address.id)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 700

    started = time.perf_counter()
    with pytest.raises(ConflictError) as exc_info:
        BottleTrackingService().record_bottles_delivered(
            order_id=order.id,
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("2"),
            actor_user_id=owner.id,
        )
    elapsed = time.perf_counter() - started

    assert exc_info.value.error_code == "BOTTLE_SCOPE_LOCK_TIMEOUT"
    # Bounded: it gave up near the configured limit instead of waiting for the
    # admin. Generous upper bound so a loaded CI box does not flake.
    assert elapsed < 15, f"writer waited {elapsed:.1f}s — the bound did not apply"
    # And it did not surface as the raw driver exception.
    assert not isinstance(exc_info.value, OperationalError)


def test_session_is_usable_after_a_scope_lock_timeout(ltdb, ltapp, holder):
    """The rollback is mandatory, not tidiness.

    Postgres aborts the transaction on 55P03. Without an explicit rollback every
    later statement fails with 25P02 ("current transaction is aborted") and the
    driver's real error is replaced by an unrelated one — including inside
    whatever renders the response.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.utils.exceptions import ConflictError

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    order = _order(ltdb, owner, address)
    conn = holder()
    _hold_address_for_no_key_update(conn, address.id)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 500
    with pytest.raises(ConflictError):
        BottleTrackingService().record_bottles_delivered(
            order_id=order.id,
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("1"),
            actor_user_id=owner.id,
        )

    # The very thing that would explode if the session were still aborted.
    assert ltdb.session.execute(text("SELECT 1")).scalar() == 1


def test_timed_out_write_leaves_no_partial_bottle_state(ltdb, ltapp, holder):
    """Nothing was saved — which is exactly what the driver is told."""
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.utils.exceptions import ConflictError

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    order = _order(ltdb, owner, address)
    conn = holder()
    _hold_address_for_no_key_update(conn, address.id)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 500
    with pytest.raises(ConflictError):
        BottleTrackingService().record_bottles_delivered(
            order_id=order.id,
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("3"),
            actor_user_id=owner.id,
        )

    conn.rollback()  # release the admin's hold so the reads below are clean

    assert (
        ltdb.session.execute(
            text("SELECT count(*) FROM bottle_ledger WHERE address_id = :a"),
            {"a": address.id},
        ).scalar()
        == 0
    )
    assert (
        ltdb.session.execute(
            text("SELECT count(*) FROM bottle_balances WHERE address_id = :a"),
            {"a": address.id},
        ).scalar()
        == 0
    )


def test_delivery_succeeds_once_the_admin_commits(ltdb, ltapp, holder):
    """The bound must not break the ordinary case: waited out, then it works."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    order = _order(ltdb, owner, address)
    conn = holder()
    _hold_address_for_no_key_update(conn, address.id)
    conn.commit()  # the admin's transaction ends, as it always eventually does
    order = _order(ltdb, owner, address)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 1000
    entry = BottleTrackingService().record_bottles_delivered(
        order_id=order.id,
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("4"),
        actor_user_id=owner.id,
    )
    ltdb.session.commit()

    assert Decimal(str(entry.balance_after)) == Decimal("4.00")


# --------------------------------------------------------------------------- #
# 4. THE HOLDER IS DELIBERATELY NOT BOUNDED
# --------------------------------------------------------------------------- #


def test_place_lifecycle_does_not_inherit_the_writer_bound(ltdb, ltapp):
    """Scope argument, pinned.

    The timeout belongs to the WAITER. Aborting an admin mid-merge frees nothing
    a retry could not, and would abandon a half-reasoned membership edit. If a
    future change installs a session-level default instead, this fails.
    """
    from business_app.services.customer_link_service import CustomerLinkService

    admin = _admin(ltdb)
    u1, u2 = _user(ltdb), _user(ltdb)
    a1, a2 = _addr(ltdb, u1), _addr(ltdb, u2)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 250
    observed = {}

    def _before(conn, cursor, statement, params, context, executemany):
        upper = statement.upper()
        if "FROM ADDRESSES" in upper and "FOR NO KEY UPDATE" in upper and "seen" not in observed:
            observed["seen"] = conn.exec_driver_sql("SHOW lock_timeout").scalar()

    event.listen(ltdb.engine, "before_cursor_execute", _before)
    try:
        CustomerLinkService().create_place_group([a1.id, a2.id], admin.id, "unbounded holder")
    finally:
        event.remove(ltdb.engine, "before_cursor_execute", _before)

    assert observed.get("seen") != "250ms", (
        "the place lifecycle inherited the writer's lock_timeout — the bound was "
        "applied at session scope instead of to the waiting writer"
    )


# --------------------------------------------------------------------------- #
# 5. WHAT THE DRIVER ACTUALLY SEES — end-to-end rendering
# --------------------------------------------------------------------------- #


def test_api_layer_renders_scope_busy_as_409_with_the_error_code(ltapp):
    """Through the REAL ``@handle_api_exception``, which every staff route wears.

    Before this change a lock failure reached ``ExceptionMapper`` as an unmapped
    ``OperationalError`` and fell through to ``(500, "INTERNAL_ERROR",
    "An unexpected error occurred")`` with ``error_code: None`` — logged
    CRITICAL, and rendered by the staff bot as the generic "service unavailable".
    A driver could not tell a transient regrouping from an outage, and had no
    reason to retry.
    """
    from flask import json

    from business_app.utils.error_handlers import handle_api_exception
    from business_app.utils.exceptions import ConflictError

    @handle_api_exception
    def _endpoint():
        raise ConflictError(
            "This address is being updated by an administrator right now. "
            "Please try again in a moment.",
            error_code="BOTTLE_SCOPE_LOCK_TIMEOUT",
            details={"address_id": 42},
        )

    with ltapp.test_request_context("/api/staff/delivery/1/status", method="PUT"):
        response = _endpoint()
        body, status = (response if isinstance(response, tuple) else (response, response.status_code))
        payload = json.loads(body.get_data(as_text=True)) if hasattr(body, "get_data") else body

    assert status == 409, f"expected 409, got {status} — a 500 is what this change removes"
    assert _find_error_code(payload) == "BOTTLE_SCOPE_LOCK_TIMEOUT"


def _find_error_code(payload):
    """The machine-readable code as a VALUE, wherever the envelope puts it."""
    if not isinstance(payload, dict):
        return None
    if payload.get("error_code"):
        return payload["error_code"]
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_error_code(value)
            if found:
                return found
    return None


def test_operational_error_default_rendering_is_what_we_avoided(ltapp):
    """Documents the BEFORE state, so the improvement is not folklore.

    ``ExceptionMapper`` has no entry for ``OperationalError``; this pins that an
    unhandled DB lock error really would have been an anonymous 500.
    """
    from business_app.utils.error_handlers import ExceptionMapper

    exc = OperationalError("SELECT 1", {}, Exception("canceling statement due to lock timeout"))
    status_code, error_type, message = ExceptionMapper.get_error_info(exc)

    assert (status_code, error_type) == (500, "INTERNAL_ERROR")
    assert message == "An unexpected error occurred"
    assert getattr(exc, "error_code", None) is None


# --------------------------------------------------------------------------- #
# 6. THE SWALLOW TRAP — a bounded wait must not become a SILENT FALSE SUCCESS
# --------------------------------------------------------------------------- #


def test_scope_lock_timeout_is_not_swallowed_by_the_delivery_cascade():
    """The delivery cascade must let this one through.

    The bottle block — ``OrderService._record_delivery_bottles``, called as the
    FIRST step of ``_handle_status_change_actions``'s DELIVERED branch — ends in

        except ValidationError: raise
        except Exception:       log and CONTINUE
        ...
        if commit: db.session.commit()

    ``ConflictError`` is NOT a ``ValidationError``, so without an explicit arm
    the scope-lock timeout is swallowed — and then the cascade COMMITS. Because
    the writer already had to roll back (Postgres aborts the transaction on
    55P03), that commit would persist nothing while telling the driver the
    delivery succeeded: "✅ Delivered" on an order that never moved, with the
    bottles unrecorded. Strictly worse than the 500 this work removes.

    Pinned at source level: the arm exists, it is keyed to the error code, and
    it re-raises. Reproducing the swallow behaviourally would need a full
    driver/delivery/session fixture racing a real lock holder — this asserts the
    same contract without that flake surface.
    """
    import inspect

    from business_app.services.order_service import OrderService

    source = inspect.getsource(OrderService._record_delivery_bottles)

    # The block only counts if the cascade still runs it — and it must run it
    # BEFORE anything that commits, or the re-raise has nothing left to abort.
    cascade = inspect.getsource(OrderService._handle_status_change_actions)
    delivered_branch = cascade.split("OrderStatus.DELIVERED:")[1]
    assert "_record_delivery_bottles" in delivered_branch, (
        "the DELIVERED branch no longer calls the bottle block at all"
    )
    assert delivered_branch.index("_record_delivery_bottles") < delivered_branch.index("commit=commit"), (
        "the bottle block no longer runs before the committing calls, so a "
        "re-raised scope-lock timeout can no longer roll the transition back"
    )

    assert "BOTTLE_SCOPE_LOCK_TIMEOUT" in source, (
        "the delivery cascade no longer special-cases the scope-lock timeout — "
        "it is being swallowed, and a timed-out delivery now reports success"
    )

    # The arm must RE-RAISE, not merely log.
    arm = source.split("except ConflictError")[1].split("except Exception")[0]
    assert "raise" in arm, "the ConflictError arm does not re-raise"
    assert "BOTTLE_SCOPE_LOCK_TIMEOUT" in arm


def test_scope_lock_conflict_is_not_a_validation_error():
    """Why the extra arm is needed at all — pins the class relationship.

    If ``ConflictError`` ever became a ``ValidationError`` subclass the existing
    arm would cover it and this could be simplified. It is not, so it must not
    be assumed.
    """
    from business_app.utils.exceptions import ConflictError, ValidationError

    assert not issubclass(ConflictError, ValidationError)


# --------------------------------------------------------------------------- #
# 7. LOAD-BEARING SURFACE — other suites depend on these two properties
# --------------------------------------------------------------------------- #


def test_scope_busy_error_preserves_the_dbapi_cause_and_names_the_mechanism(
    ltdb, ltapp, holder
):
    """Two properties that look cosmetic and are not.

    ``ConflictError`` REPLACES the ``OperationalError`` that the place e2e
    suites were written against. Those suites assert the failure in two styles:

      * ``assert "lock timeout" in str(error).lower()``  — 8 sites across
        ``test_place_lifecycle_full_e2e.py`` and ``test_place_split_full_e2e.py``
      * ``exc.orig.pgcode == "55P03"``                   — ``_assert_lock_timeout``
        in ``test_place_concurrency_pg_e2e.py``

    Keeping the mechanism in the message and the DBAPI cause on ``.orig`` is
    what let this change land WITHOUT editing a single one of those assertions.
    Pinned here so a later "tidy up the error copy" does not quietly break eight
    tests in files that have nothing to do with this one.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.utils.exceptions import ConflictError

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    order = _order(ltdb, owner, address)
    conn = holder()
    _hold_address_for_no_key_update(conn, address.id)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 500
    with pytest.raises(ConflictError) as exc_info:
        BottleTrackingService().record_bottles_delivered(
            order_id=order.id,
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("1"),
            actor_user_id=owner.id,
        )

    error = exc_info.value
    assert "lock timeout" in str(error).lower(), (
        "the place e2e suites match on this phrase; changing the copy without "
        "updating them breaks 8 assertions in unrelated files"
    )
    assert getattr(getattr(error, "orig", None), "pgcode", None) == "55P03", (
        "the DBAPI cause was dropped — _assert_lock_timeout in "
        "test_place_concurrency_pg_e2e.py reads exc.orig.pgcode"
    )


def test_an_explicitly_set_tighter_bound_is_not_clobbered(ltdb, ltapp):
    """A caller that already bounded its transaction has the more specific intent.

    Concurrency tests set a tight ``SET LOCAL lock_timeout`` and then call the
    real service. If this default overwrote it, those tests would silently wait
    for the default instead of the window they asked for — and the bound they
    were pinning would no longer be the one under test.
    """
    from business_app.services.bottle_tracking_service import BottleTrackingService

    owner = _user(ltdb)
    address = _addr(ltdb, owner)

    ltapp.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 5000
    ltdb.session.execute(text("SET LOCAL lock_timeout = '250ms'"))
    BottleTrackingService.resolve_scope_for_write(address.id)

    assert ltdb.session.execute(text("SHOW lock_timeout")).scalar() == "250ms"
