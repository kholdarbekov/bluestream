"""Unit A — FIFO lot ledger + expiry consolidation.

These tests pin the mathematically-correct point ledger:
  * each positive earn transaction is a "lot" with a ``remaining_points`` balance
  * redemptions consume the OLDEST live lots first (FIFO)
  * expiry only expires the UNSPENT remainder of a lot (never double-counts a
    lot that was already spent before it expired)
  * available-point reads are PURE (no writes / no commit side effects)
  * expiry is consolidated, increments ``total_expired`` + ``last_expiry_check``,
    and floors the cached balance at 0
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTransaction
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def secondary_user(db):
    user = User(
        email="second@example.com",
        phone="+998901234599",
        password_hash=hash_password("TestPassword123!"),
        first_name="Second",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for tests",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        points_expiry_days=365,
    )
    db.session.add(prog)
    db.session.commit()
    return prog


@pytest.fixture
def account(db, sample_user, program):
    acc = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=program.id,
        total_earned=0,
        total_redeemed=0,
        total_expired=0,
        current_balance=0,
        current_tier="Bronze",
        points_to_next_tier=0,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _earn_lot(db, user_id, points, *, age_days, ttl_days=365, remaining=None, expired=False):
    """Create a positive earn lot with controlled created_at / expires_at."""
    now = datetime.now(timezone.utc)
    txn = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=points,
        description="earn",
        remaining_points=points if remaining is None else remaining,
        expires_at=now + timedelta(days=ttl_days),
        is_expired=expired,
    )
    db.session.add(txn)
    db.session.flush()
    # created_at is set by TimestampMixin; override for deterministic FIFO order
    txn.created_at = now - timedelta(days=age_days)
    db.session.commit()
    return txn


@pytest.mark.unit
class TestFifoLedger:
    def test_award_points_initializes_remaining_points(self, loyalty_service, account, db, monkeypatch):
        monkeypatch.setattr(loyalty_service, "_check_tier_upgrade", lambda _a: None)
        monkeypatch.setattr(loyalty_service, "_send_points_notification", Mock())

        txn = loyalty_service.award_points(
            user_id=account.user_id, points=250, description="earn", action_type=LoyaltyActionType.PURCHASE
        )

        assert txn.remaining_points == 250

    def test_award_points_uses_program_expiry_window(self, loyalty_service, account, db, monkeypatch):
        """expires_at must come from LoyaltyProgram.points_expiry_days (DB SSOT),
        not the undefined LOYALTY_POINTS_EXPIRY_DAYS config (which pinned 365)."""
        monkeypatch.setattr(loyalty_service, "_check_tier_upgrade", lambda _a: None)
        monkeypatch.setattr(loyalty_service, "_send_points_notification", Mock())
        account.program.points_expiry_days = 30
        db.session.commit()

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        txn = loyalty_service.award_points(account.user_id, 100, "earn")

        exp = txn.expires_at
        if exp.tzinfo is not None:
            exp = exp.astimezone(timezone.utc).replace(tzinfo=None)
        delta_days = (exp - before).days
        assert 29 <= delta_days <= 30

    def test_redeem_consumes_oldest_lot_first(self, loyalty_service, account, db, monkeypatch):
        old_lot = _earn_lot(db, account.user_id, 200, age_days=30)
        new_lot = _earn_lot(db, account.user_id, 100, age_days=1)
        account.current_balance = 300
        db.session.commit()

        loyalty_service.deduct_points(user_id=account.user_id, points=200, description="redeem")

        db.session.refresh(old_lot)
        db.session.refresh(new_lot)
        assert old_lot.remaining_points == 0
        assert new_lot.remaining_points == 100
        assert loyalty_service.get_available_points(account.user_id) == 100

    def test_redeem_partially_consumes_second_lot(self, loyalty_service, account, db, monkeypatch):
        old_lot = _earn_lot(db, account.user_id, 200, age_days=30)
        new_lot = _earn_lot(db, account.user_id, 100, age_days=1)
        account.current_balance = 300
        db.session.commit()

        loyalty_service.deduct_points(user_id=account.user_id, points=250, description="redeem")

        db.session.refresh(old_lot)
        db.session.refresh(new_lot)
        assert old_lot.remaining_points == 0
        assert new_lot.remaining_points == 50
        assert loyalty_service.get_available_points(account.user_id) == 50

    def test_expiry_does_not_double_count_already_spent_lot(self, loyalty_service, account, db, monkeypatch):
        """The headline correctness fix: spend a lot, then let it expire -> nothing left to expire."""
        old_lot = _earn_lot(db, account.user_id, 200, age_days=30)
        new_lot = _earn_lot(db, account.user_id, 100, age_days=1)
        account.current_balance = 300
        db.session.commit()

        # Redeem 200 -> consumes old_lot fully (FIFO)
        loyalty_service.deduct_points(user_id=account.user_id, points=200, description="redeem")

        # Now the old (already-spent) lot reaches its expiry date.
        old_lot.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.session.commit()

        expired = loyalty_service._expire_user_points(account.user_id)
        db.session.commit()
        db.session.refresh(account)

        assert expired == 0  # the spent lot has no unspent remainder to expire
        assert account.current_balance == 100  # not driven negative, not double-counted
        assert account.total_expired == 0
        assert loyalty_service.get_available_points(account.user_id) == 100

    def test_expiry_expires_unspent_remainder(self, loyalty_service, account, db):
        lot = _earn_lot(db, account.user_id, 200, age_days=400, ttl_days=-1)  # already past expiry
        account.current_balance = 200
        db.session.commit()

        expired = loyalty_service._expire_user_points(account.user_id)
        db.session.commit()
        db.session.refresh(account)
        db.session.refresh(lot)

        assert expired == 200
        assert lot.is_expired is True
        assert lot.remaining_points == 0
        assert account.current_balance == 0
        assert account.total_expired == 200
        assert account.last_expiry_check is not None

    def test_get_available_points_is_pure_no_write(self, loyalty_service, account, db):
        """Reading available points must not flag/expire or commit anything."""
        lot = _earn_lot(db, account.user_id, 200, age_days=400, ttl_days=-1)  # past expiry, not yet flagged
        account.current_balance = 200
        db.session.commit()

        available = loyalty_service.get_available_points(account.user_id)

        db.session.refresh(lot)
        db.session.refresh(account)
        assert available == 0  # time-expired lot excluded from spendable
        assert lot.is_expired is False  # NOT mutated by a read
        assert account.current_balance == 200  # cache untouched by a read
        assert account.total_expired == 0

    def test_reverse_earnings_award_lot_has_expiry(self, loyalty_service, account, db):
        """A positive order-edit adjustment is a spendable lot and MUST carry an
        expiry, otherwise it can never be swept and leaks forever."""
        result = loyalty_service.reverse_earnings(
            user_id=account.user_id,
            order_id=1,
            old_points_earned=0,
            new_points_earned=50,
            commit=True,
        )
        txn = LoyaltyTransaction.query.get(result["transaction_id"])
        assert txn.points == 50
        assert txn.remaining_points == 50
        assert txn.expires_at is not None

    def test_migration_backfill_reconstructs_remaining_points(self, db, account):
        """The one-time FIFO backfill must reconstruct lot remainders + reconcile."""
        import importlib.util
        import os

        user_id = account.user_id
        now = datetime.now(timezone.utc)

        def _raw_lot(points, *, age_days, type_=LoyaltyTransactionType.EARNED, expired=False):
            txn = LoyaltyTransaction(
                user_id=user_id,
                transaction_type=type_,
                points=points,
                description="legacy",
                remaining_points=None,  # pre-migration legacy row
                expires_at=now + timedelta(days=365),
                is_expired=expired,
            )
            db.session.add(txn)
            db.session.flush()
            txn.created_at = now - timedelta(days=age_days)
            return txn

        lot1 = _raw_lot(300, age_days=50)
        lot2 = _raw_lot(200, age_days=20)
        old_expired = _raw_lot(100, age_days=400, expired=True)
        db.session.add(
            LoyaltyTransaction(
                user_id=user_id,
                transaction_type=LoyaltyTransactionType.REDEEMED,
                points=-250,
                description="legacy redeem",
            )
        )
        account.current_balance = 999  # deliberately stale
        account.total_expired = 0
        db.session.commit()

        mig_path = os.path.join(
            os.getcwd(),
            "business_app/migrations/versions/e7d2a9c4b1f3_loyalty_fifo_remaining_points.py",
        )
        spec = importlib.util.spec_from_file_location("mig_e7d2a9c4b1f3", mig_path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)

        mig._backfill_remaining_points(db.session.connection())
        db.session.commit()

        for obj in (lot1, lot2, old_expired, account):
            db.session.refresh(obj)

        assert lot1.remaining_points == 50  # 300 - 250 consumed FIFO
        assert lot2.remaining_points == 200  # untouched
        assert old_expired.remaining_points == 0  # historically expired -> not spendable
        assert account.current_balance == 250  # 50 + 200, reconciled to live lots
        # total_expired is intentionally not backfilled (can't reconstruct reliably)
        assert account.total_expired == 0

    def test_expire_points_floors_balance_and_isolates_per_user(self, loyalty_service, db, program, sample_user, secondary_user):
        # user A: one expired lot
        a = LoyaltyPoints(user_id=sample_user.id, program_id=program.id, current_balance=200, total_earned=200)
        b = LoyaltyPoints(user_id=secondary_user.id, program_id=program.id, current_balance=100, total_earned=100)
        db.session.add_all([a, b])
        db.session.commit()
        _earn_lot(db, sample_user.id, 200, age_days=400, ttl_days=-1)
        _earn_lot(db, secondary_user.id, 100, age_days=400, ttl_days=-1)

        result = loyalty_service.expire_points()

        db.session.refresh(a)
        db.session.refresh(b)
        assert result["total_expired_points"] == 300
        assert result["affected_users"] == 2
        assert a.current_balance == 0
        assert b.current_balance == 0
        assert a.total_expired == 200
        assert b.total_expired == 100
