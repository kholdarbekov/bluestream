"""Postgres proof that BottleTrackingService.get_or_create_balance row-locks.

``get_or_create_balance`` funnels every bottle-balance mutation (delivery,
return, standalone collection, admin adjustment). Under READ COMMITTED, a
plain SELECT-then-read-modify-write lets two concurrent events on the same
PLACE read the same balance and the later commit silently overwrites the
earlier delta (lost update — bottles vanish). The fix adds ``SELECT ... FOR
UPDATE`` locking plus a conflict-safe ``INSERT ... ON CONFLICT DO NOTHING``
create path (backed by the existing ``uq_bottle_balance_addr`` /
``uq_bottle_balance_group`` unique constraints — exactly one of the two is
active per place, per ``BottleScope``).

Every address here is deliberately left UNGROUPED, so its place is the
address itself (``BottleScope.for_address``) and ``get_or_create_balance``
takes a single ``address_id`` argument.

These tests run against a real, fully-migrated Postgres database (``pg_app``/
``pg_db``) because SQLite silently accepts ``FOR UPDATE`` as a no-op and has
no real row locking — exactly the gap that let the lost-update bug through.
"""

import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event

from business_app.models.bottle import BottleBalance
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, UserRole, UserType


def _make_customer(pg_db, *, phone="+998900000301"):
    user = User(
        email=f"bottle.lock.{phone[-4:]}@example.com",
        phone=phone,
        password_hash=hash_password("CustPassword123!"),
        first_name="Bottle",
        last_name="Lock",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add(user)
    pg_db.session.flush()
    return user


def _make_address(pg_db, user):
    address = UserAddress(
        user_id=user.id,
        full_address="1 Bottle Lock Street, Tashkent",
        street_address="1 Bottle Lock Street",
        city="Tashkent",
        latitude=41.2995,
        longitude=69.2401,
        is_default=True,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    return address


@pytest.mark.integration
class TestGetOrCreateBalanceLocking:
    def test_locks_existing_row_with_select_for_update(self, pg_app, pg_db):
        """The SELECT that fetches an existing balance row must carry FOR UPDATE.

        This is the direct regression guard: it FAILS against the original
        plain-SELECT implementation and PASSES once ``.with_for_update()`` is
        added. We capture the real SQL sent to Postgres via a
        ``before_cursor_execute`` engine event rather than asserting on
        Python-level query construction, so it proves actual wire behavior.
        """
        customer = _make_customer(pg_db)
        address = _make_address(pg_db, customer)
        # Seed one existing place balance through the real ledger-write path
        # (not a bare ORM construction) so the row is exactly what production
        # would leave behind.
        BottleTrackingService()._create_ledger_entry(
            user_id=customer.id, address_id=address.id,
            event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2.00"),
        )
        pg_db.session.commit()
        existing = BottleBalance.query.filter_by(address_id=address.id).one()
        existing_id = existing.id

        statements = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = pg_db.engine
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            result = BottleTrackingService.get_or_create_balance(address.id)
            pg_db.session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        assert result.id == existing_id

        locking_selects = [
            s
            for s in statements
            if "bottle_balances" in s and "FOR UPDATE" in s.upper()
        ]
        assert locking_selects, (
            "Expected at least one SELECT ... FOR UPDATE against "
            f"bottle_balances; captured statements: {statements}"
        )

    def test_creates_zero_balance_row_exactly_once(self, pg_app, pg_db):
        """A brand-new place gets a single zero-balance row, and a second call
        returns that same row (no duplicate, no error) — the ON CONFLICT DO
        NOTHING + re-select path is idempotent."""
        customer = _make_customer(pg_db)
        address = _make_address(pg_db, customer)
        pg_db.session.commit()

        first = BottleTrackingService.get_or_create_balance(address.id)
        pg_db.session.commit()

        assert first.balance == Decimal("0.00")
        rows = BottleBalance.query.filter_by(address_id=address.id).all()
        assert len(rows) == 1

        second = BottleTrackingService.get_or_create_balance(address.id)
        pg_db.session.commit()

        assert second.id == first.id
        rows_after = BottleBalance.query.filter_by(address_id=address.id).all()
        assert len(rows_after) == 1

    def test_concurrent_creates_do_not_raise_or_duplicate(self, pg_app, pg_db):
        """Two real concurrent transactions racing to create the same brand-new
        balance row must not raise IntegrityError and must not leave two rows —
        this exercises the INSERT ... ON CONFLICT DO NOTHING branch for real,
        rather than merely asserting on query construction."""
        customer = _make_customer(pg_db)
        address = _make_address(pg_db, customer)
        pg_db.session.commit()

        barrier = threading.Barrier(2)
        errors = []

        def worker():
            with pg_app.app_context():
                from business_app import db as thread_db

                try:
                    barrier.wait(timeout=5)
                    BottleTrackingService.get_or_create_balance(address.id)
                    thread_db.session.commit()
                except Exception as exc:  # pragma: no cover - failure path asserted below
                    thread_db.session.rollback()
                    errors.append(exc)
                finally:
                    thread_db.session.remove()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Unexpected errors from concurrent create: {errors}"

        pg_db.session.expire_all()
        rows = BottleBalance.query.filter_by(address_id=address.id).all()
        assert len(rows) == 1

    def test_sequential_ledger_writes_sum_to_correct_balance(self, pg_app, pg_db):
        """Guard against the fix accidentally breaking normal accumulation:
        a sequence of real ledger writes through the public mutation path
        must still add up correctly."""
        customer = _make_customer(pg_db)
        address = _make_address(pg_db, customer)
        pg_db.session.commit()

        svc = BottleTrackingService()
        svc.set_initial_balance(customer.id, address.id, Decimal("5"), actor_user_id=customer.id)
        svc.admin_adjust_balance(
            customer.id, address.id, Decimal("3"), actor_user_id=customer.id, notes="t1"
        )
        svc.admin_adjust_balance(
            customer.id, address.id, Decimal("-2"), actor_user_id=customer.id, notes="t2"
        )

        pg_db.session.expire_all()
        balance = BottleBalance.query.filter_by(address_id=address.id).first()
        assert balance.balance == Decimal("6.00")  # 5 + 3 - 2

    def test_concurrent_adjustments_do_not_lose_updates(self, pg_app, pg_db):
        """The core regression this task fixes: two concurrent read-modify-write
        mutations on the SAME existing balance row must both land — the row
        lock serialises them instead of one clobbering the other."""
        customer = _make_customer(pg_db)
        address = _make_address(pg_db, customer)
        svc = BottleTrackingService()
        svc.set_initial_balance(customer.id, address.id, Decimal("10"), actor_user_id=customer.id)

        barrier = threading.Barrier(2)
        errors = []

        def worker(delta, tag):
            with pg_app.app_context():
                from business_app import db as thread_db

                try:
                    barrier.wait(timeout=5)
                    BottleTrackingService().admin_adjust_balance(
                        customer.id,
                        address.id,
                        Decimal(delta),
                        actor_user_id=customer.id,
                        notes=f"concurrent-{tag}",
                    )
                except Exception as exc:  # pragma: no cover - failure path asserted below
                    thread_db.session.rollback()
                    errors.append(exc)
                finally:
                    thread_db.session.remove()

        threads = [
            threading.Thread(target=worker, args=(Decimal("3"), "a")),
            threading.Thread(target=worker, args=(Decimal("5"), "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Unexpected errors from concurrent adjustment: {errors}"

        pg_db.session.expire_all()
        balance = BottleBalance.query.filter_by(address_id=address.id).first()
        assert balance.balance == Decimal("18.00")  # 10 + 3 + 5, no lost update
