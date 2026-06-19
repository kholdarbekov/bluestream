"""Comprehensive characterization unit tests for LoyaltyService.

These are regression/characterization tests of the EXISTING loyalty earn/redeem/
expire/reverse logic. They assert what the code ACTUALLY does today. Notification
side effects are always monkeypatched to no-ops.

Coverage:
- FIFO consumption (_consume_lots_fifo / deduct_points)
- available-points read path (get_available_points + LoyaltyPoints.calculate_current_balance)
- award_points earn/bonus semantics + expiry stamping
- expire_points / _expire_user_points sweep semantics
- reverse_earnings clawback/award branches
- calculate_qualifying_points trailing-window + type filtering
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyTransaction,
)
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.exceptions import ValidationError

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    """All LoyaltyService notification hooks are no-ops for every test here."""
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    monkeypatch.setattr(LoyaltyService, "_send_tier_upgrade_notification", lambda *a, **k: None)
    monkeypatch.setattr(LoyaltyService, "_send_points_expiry_notification", lambda *a, **k: None)


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for tests",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        points_expiry_days=365,
        signup_bonus=100,
        referral_bonus=50,
        birthday_bonus=25,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def account(db, sample_user, loyalty_program):
    """Bare account with no lots; helper builders seed lots/balance explicitly."""
    acc = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=loyalty_program.id,
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


def _lot(
    user_id,
    points,
    *,
    remaining=None,
    created_at,
    expires_at=FAR_FUTURE,
    is_expired=False,
    txn_type=LoyaltyTransactionType.EARNED,
    description="lot",
):
    """Create + persist an earn lot with a fully-controlled created_at (for FIFO order)."""
    lot = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=txn_type,
        points=points,
        remaining_points=remaining,
        description=description,
        expires_at=expires_at,
        is_expired=is_expired,
    )
    _db.session.add(lot)
    _db.session.flush()
    # created_at is auto-stamped by TimestampMixin; override so ordering is deterministic.
    lot.created_at = created_at
    _db.session.commit()
    return lot


# ---------------------------------------------------------------------------
# FIFO: _consume_lots_fifo / deduct_points
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestFifoConsumption:
    def test_consume_lots_oldest_first_across_two_lots(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        old = _lot(sample_user.id, 100, remaining=100, created_at=base, description="old")
        new = _lot(sample_user.id, 100, remaining=100, created_at=base + timedelta(days=1), description="new")

        consumed = loyalty_service._consume_lots_fifo(sample_user.id, 100)
        _db.session.commit()

        _db.session.refresh(old)
        _db.session.refresh(new)
        assert consumed == 100
        # Oldest lot drained first; the newer lot is untouched.
        assert old.remaining_points == 0
        assert new.remaining_points == 100

    def test_partial_consumption_leaves_correct_remainder_on_boundary_lot(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        old = _lot(sample_user.id, 100, remaining=100, created_at=base, description="old")
        new = _lot(sample_user.id, 100, remaining=100, created_at=base + timedelta(days=1), description="new")

        loyalty_service._consume_lots_fifo(sample_user.id, 150)
        _db.session.commit()

        _db.session.refresh(old)
        _db.session.refresh(new)
        assert old.remaining_points == 0
        # Boundary lot keeps 100 - 50 = 50.
        assert new.remaining_points == 50

    def test_consumes_across_three_lots(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        l1 = _lot(sample_user.id, 40, remaining=40, created_at=base)
        l2 = _lot(sample_user.id, 40, remaining=40, created_at=base + timedelta(days=1))
        l3 = _lot(sample_user.id, 40, remaining=40, created_at=base + timedelta(days=2))

        consumed = loyalty_service._consume_lots_fifo(sample_user.id, 100)
        _db.session.commit()

        for lot in (l1, l2, l3):
            _db.session.refresh(lot)
        assert consumed == 100
        assert l1.remaining_points == 0
        assert l2.remaining_points == 0
        assert l3.remaining_points == 20  # 100 - 40 - 40 = 20 drawn from l3

    def test_null_remaining_falls_back_to_points(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        legacy = _lot(sample_user.id, 80, remaining=None, created_at=base, description="legacy")

        consumed = loyalty_service._consume_lots_fifo(sample_user.id, 30)
        _db.session.commit()

        _db.session.refresh(legacy)
        assert consumed == 30
        # NULL remaining treated as full points (80), drawn down to 50.
        assert legacy.remaining_points == 50

    def test_consume_zero_or_negative_is_noop(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        lot = _lot(sample_user.id, 50, remaining=50, created_at=base)

        assert loyalty_service._consume_lots_fifo(sample_user.id, 0) == 0
        assert loyalty_service._consume_lots_fifo(sample_user.id, -5) == 0
        _db.session.refresh(lot)
        assert lot.remaining_points == 50

    def test_deduct_points_raises_when_available_less_than_requested(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 100, remaining=100, created_at=base)
        account.current_balance = 100
        _db.session.commit()

        with pytest.raises(ValidationError, match="Insufficient points"):
            loyalty_service.deduct_points(sample_user.id, 200, "too much")

    def test_deduct_points_draws_lots_and_updates_counters(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        old = _lot(sample_user.id, 100, remaining=100, created_at=base)
        new = _lot(sample_user.id, 100, remaining=100, created_at=base + timedelta(days=1))
        account.current_balance = 200
        account.total_redeemed = 0
        _db.session.commit()

        tx = loyalty_service.deduct_points(sample_user.id, 120, "redeem", skip_notification=True)

        _db.session.refresh(account)
        _db.session.refresh(old)
        _db.session.refresh(new)
        assert tx.points == -120
        assert tx.transaction_type == LoyaltyTransactionType.REDEEMED
        assert account.current_balance == 80
        assert account.total_redeemed == 120
        # FIFO: oldest lot emptied, boundary lot drawn to 80.
        assert old.remaining_points == 0
        assert new.remaining_points == 80
        # Available points read path agrees with the cached balance.
        assert loyalty_service.get_available_points(sample_user.id) == 80


# ---------------------------------------------------------------------------
# Available points read path
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestAvailablePoints:
    def test_equals_sum_of_remaining_of_live_lots(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 100, remaining=60, created_at=base)
        _lot(sample_user.id, 50, remaining=50, created_at=base + timedelta(days=1))

        assert loyalty_service.get_available_points(sample_user.id) == 110

    def test_excludes_is_expired_lots(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 100, remaining=100, created_at=base)
        _lot(sample_user.id, 200, remaining=200, created_at=base + timedelta(days=1), is_expired=True)

        assert loyalty_service.get_available_points(sample_user.id) == 100

    def test_excludes_lots_past_expires_at(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        _lot(sample_user.id, 100, remaining=100, created_at=base, expires_at=now + timedelta(days=10))
        # Past expiry but not yet flagged is_expired -> still excluded from available.
        _lot(
            sample_user.id,
            300,
            remaining=300,
            created_at=base + timedelta(days=1),
            expires_at=now - timedelta(days=1),
        )

        assert loyalty_service.get_available_points(sample_user.id) == 100

    def test_ignores_negative_transactions_no_double_subtraction(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # Earn lot already drawn down to 70 by a prior redemption...
        _lot(sample_user.id, 100, remaining=70, created_at=base)
        # ...and the negative REDEEMED row (points<0) must NOT be subtracted again.
        neg = LoyaltyTransaction(
            user_id=sample_user.id,
            transaction_type=LoyaltyTransactionType.REDEEMED,
            points=-30,
            remaining_points=None,
            description="redeemed",
        )
        _db.session.add(neg)
        _db.session.commit()

        assert loyalty_service.get_available_points(sample_user.id) == 70

    def test_null_remaining_legacy_lot_counts_as_points(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 90, remaining=None, created_at=base)

        assert loyalty_service.get_available_points(sample_user.id) == 90

    def test_model_calculate_current_balance_matches_service(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        _lot(sample_user.id, 100, remaining=60, created_at=base)
        _lot(sample_user.id, 40, remaining=40, created_at=base + timedelta(days=1))
        _lot(sample_user.id, 200, remaining=200, created_at=base + timedelta(days=2), is_expired=True)
        _lot(
            sample_user.id,
            500,
            remaining=500,
            created_at=base + timedelta(days=3),
            expires_at=now - timedelta(days=1),
        )

        account.current_balance = 0
        account.calculate_current_balance()
        _db.session.commit()

        # 60 + 40 live; expired-flag and past-expiry lots excluded -> 100.
        assert account.current_balance == 100
        assert account.current_balance == loyalty_service.get_available_points(sample_user.id)


# ---------------------------------------------------------------------------
# award_points
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestAwardPoints:
    def test_rejects_non_positive(self, loyalty_service, account, sample_user):
        with pytest.raises(ValidationError, match="Points must be positive"):
            loyalty_service.award_points(sample_user.id, 0, "zero")
        with pytest.raises(ValidationError, match="Points must be positive"):
            loyalty_service.award_points(sample_user.id, -10, "neg")

    def test_purchase_creates_earned_lot_and_bumps_totals(
        self, loyalty_service, account, sample_user
    ):
        tx = loyalty_service.award_points(
            sample_user.id, 150, "Purchase points", action_type=LoyaltyActionType.PURCHASE
        )

        _db.session.refresh(account)
        assert tx.transaction_type == LoyaltyTransactionType.EARNED
        assert tx.points == 150
        assert tx.remaining_points == 150
        assert account.current_balance == 150
        assert account.total_earned == 150

    @pytest.mark.parametrize(
        "action_type",
        [
            LoyaltyActionType.REFERRAL,
            LoyaltyActionType.BIRTHDAY_BONUS,
            LoyaltyActionType.WELCOME_BONUS,
        ],
    )
    def test_bonus_action_types_create_bonus_transaction(
        self, loyalty_service, account, sample_user, action_type
    ):
        tx = loyalty_service.award_points(sample_user.id, 80, "bonus", action_type=action_type)

        assert tx.transaction_type == LoyaltyTransactionType.BONUS
        assert tx.remaining_points == 80
        assert (tx.extra_data or {}).get("action_type") == action_type.value

    def test_expires_at_defaults_to_program_window(self, loyalty_service, account, sample_user):
        before = datetime.now(timezone.utc)
        tx = loyalty_service.award_points(sample_user.id, 10, "expiry check")
        after = datetime.now(timezone.utc)

        expires = tx.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        # program.points_expiry_days == 365.
        assert before + timedelta(days=365) - timedelta(minutes=1) <= expires
        assert expires <= after + timedelta(days=365) + timedelta(minutes=1)


# ---------------------------------------------------------------------------
# expire_points / _expire_user_points
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestExpiry:
    def test_fully_unspent_lapsed_lot_expires_completely(
        self, loyalty_service, account, sample_user
    ):
        now = datetime.now(timezone.utc)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        lapsed = _lot(
            sample_user.id,
            200,
            remaining=200,
            created_at=base,
            expires_at=now - timedelta(days=1),
        )
        account.current_balance = 200
        account.total_expired = 0
        _db.session.commit()

        expired = loyalty_service._expire_user_points(sample_user.id)
        _db.session.commit()

        _db.session.refresh(lapsed)
        _db.session.refresh(account)
        assert expired == 200
        assert lapsed.is_expired is True
        assert lapsed.remaining_points == 0
        assert account.total_expired == 200
        assert account.current_balance == 0

    def test_partially_spent_lapsed_lot_expires_only_remaining(
        self, loyalty_service, account, sample_user
    ):
        now = datetime.now(timezone.utc)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # Lot of 200 already spent down to 60 (140 redeemed earlier).
        lapsed = _lot(
            sample_user.id,
            200,
            remaining=60,
            created_at=base,
            expires_at=now - timedelta(days=1),
        )
        account.current_balance = 60
        account.total_expired = 0
        _db.session.commit()

        expired = loyalty_service._expire_user_points(sample_user.id)
        _db.session.commit()

        _db.session.refresh(lapsed)
        _db.session.refresh(account)
        # Only the unspent 60 expires (no double-count of the already-spent 140).
        assert expired == 60
        assert lapsed.is_expired is True
        assert lapsed.remaining_points == 0
        assert account.total_expired == 60
        assert account.current_balance == 0

    def test_not_yet_expired_lot_untouched(self, loyalty_service, account, sample_user):
        now = datetime.now(timezone.utc)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        live = _lot(
            sample_user.id,
            100,
            remaining=100,
            created_at=base,
            expires_at=now + timedelta(days=30),
        )
        account.current_balance = 100
        _db.session.commit()

        expired = loyalty_service._expire_user_points(sample_user.id)
        _db.session.commit()

        _db.session.refresh(live)
        _db.session.refresh(account)
        assert expired == 0
        assert live.is_expired is False
        assert live.remaining_points == 100
        assert account.current_balance == 100

    def test_current_balance_floored_at_zero(self, loyalty_service, account, sample_user):
        now = datetime.now(timezone.utc)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(
            sample_user.id,
            300,
            remaining=300,
            created_at=base,
            expires_at=now - timedelta(days=1),
        )
        # Cached balance is artificially low (e.g. drift); floor must hold at 0.
        account.current_balance = 50
        _db.session.commit()

        loyalty_service._expire_user_points(sample_user.id)
        _db.session.commit()

        _db.session.refresh(account)
        assert account.current_balance == 0

    def test_expire_points_batch_sweeps_affected_user(self, loyalty_service, account, sample_user):
        now = datetime.now(timezone.utc)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        lapsed = _lot(
            sample_user.id,
            120,
            remaining=120,
            created_at=base,
            expires_at=now - timedelta(days=1),
        )
        account.current_balance = 120
        _db.session.commit()

        result = loyalty_service.expire_points()

        _db.session.refresh(lapsed)
        assert result["total_expired_points"] == 120
        assert result["affected_users"] == 1
        assert lapsed.is_expired is True

    def test_get_points_expiring_soon_within_window(self, loyalty_service, account, sample_user):
        now = datetime.now(timezone.utc)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # Inside the 7-day window.
        _lot(
            sample_user.id,
            100,
            remaining=100,
            created_at=base,
            expires_at=now + timedelta(days=3),
        )
        # Beyond the window -> excluded.
        _lot(
            sample_user.id,
            500,
            remaining=500,
            created_at=base + timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )

        rows = loyalty_service.get_points_expiring_soon(days=7)
        by_user = {r["user_id"]: r for r in rows}
        assert sample_user.id in by_user
        # Only the in-window lot's points are reported.
        assert by_user[sample_user.id]["expiring_points"] == 100


# ---------------------------------------------------------------------------
# reverse_earnings
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestReverseEarnings:
    def test_zero_diff_is_noop(self, loyalty_service, account, sample_user):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 100, remaining=100, created_at=base)
        account.current_balance = 100
        account.total_earned = 100
        _db.session.commit()

        result = loyalty_service.reverse_earnings(
            sample_user.id, order_id=1, old_points_earned=50, new_points_earned=50
        )

        assert result["diff"] == 0
        assert result["clawback"] == 0
        assert result["award"] == 0
        assert result["transaction_id"] is None
        # No ADJUSTMENT row created.
        assert (
            LoyaltyTransaction.query.filter_by(
                user_id=sample_user.id, transaction_type=LoyaltyTransactionType.ADJUSTMENT
            ).count()
            == 0
        )

    def test_positive_diff_claws_back_full_when_balance_sufficient(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 200, remaining=200, created_at=base)
        account.current_balance = 200
        account.total_earned = 200
        _db.session.commit()

        result = loyalty_service.reverse_earnings(
            sample_user.id, order_id=7, old_points_earned=100, new_points_earned=40
        )

        _db.session.refresh(account)
        assert result["diff"] == 60
        assert result["clawback"] == 60
        assert result["uncollectible"] == 0
        assert account.current_balance == 140
        assert account.total_earned == 140
        adj = LoyaltyTransaction.query.get(result["transaction_id"])
        assert adj.transaction_type == LoyaltyTransactionType.ADJUSTMENT
        assert adj.points == -60

    def test_positive_diff_clamp_reports_uncollectible(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # User earned 100 but only 30 remain spendable.
        _lot(sample_user.id, 100, remaining=30, created_at=base)
        account.current_balance = 30
        account.total_earned = 100
        _db.session.commit()

        result = loyalty_service.reverse_earnings(
            sample_user.id, order_id=9, old_points_earned=100, new_points_earned=20, clamp=True
        )

        _db.session.refresh(account)
        assert result["diff"] == 80
        assert result["clawback"] == 30  # only what was available
        assert result["uncollectible"] == 50
        assert account.current_balance == 0
        # total_earned reduced only by the clawed-back amount.
        assert account.total_earned == 70

    def test_positive_diff_no_clamp_allows_full_clawback(
        self, loyalty_service, account, sample_user
    ):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _lot(sample_user.id, 100, remaining=30, created_at=base)
        account.current_balance = 30
        account.total_earned = 100
        _db.session.commit()

        result = loyalty_service.reverse_earnings(
            sample_user.id, order_id=11, old_points_earned=100, new_points_earned=20, clamp=False
        )

        _db.session.refresh(account)
        assert result["diff"] == 80
        assert result["clawback"] == 80
        assert result["uncollectible"] == 0
        # Balance goes negative (available 30 - 80).
        assert account.current_balance == -50
        assert account.total_earned == 20

    def test_negative_diff_awards_extra_as_positive_adjustment_lot(
        self, loyalty_service, account, sample_user
    ):
        account.current_balance = 100
        account.total_earned = 100
        _db.session.commit()

        result = loyalty_service.reverse_earnings(
            sample_user.id, order_id=13, old_points_earned=40, new_points_earned=100
        )

        _db.session.refresh(account)
        assert result["diff"] == -60
        assert result["award"] == 60
        assert result["clawback"] == 0
        assert account.current_balance == 160
        assert account.total_earned == 160
        adj = LoyaltyTransaction.query.get(result["transaction_id"])
        assert adj.transaction_type == LoyaltyTransactionType.ADJUSTMENT
        assert adj.points == 60
        assert adj.remaining_points == 60  # new spendable lot
        assert adj.expires_at is not None  # carries an expiry so the sweep can reclaim it

    def test_rejects_negative_totals(self, loyalty_service, account, sample_user):
        with pytest.raises(ValidationError, match="non-negative"):
            loyalty_service.reverse_earnings(
                sample_user.id, order_id=1, old_points_earned=-1, new_points_earned=0
            )
        with pytest.raises(ValidationError, match="non-negative"):
            loyalty_service.reverse_earnings(
                sample_user.id, order_id=1, old_points_earned=0, new_points_earned=-5
            )


# ---------------------------------------------------------------------------
# calculate_qualifying_points
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestQualifyingPoints:
    def test_sums_positive_earned_and_bonus_within_window(
        self, loyalty_service, account, sample_user
    ):
        now = datetime.now(timezone.utc)
        _lot(
            sample_user.id,
            100,
            remaining=100,
            created_at=now - timedelta(days=10),
            txn_type=LoyaltyTransactionType.EARNED,
        )
        _lot(
            sample_user.id,
            50,
            remaining=50,
            created_at=now - timedelta(days=20),
            txn_type=LoyaltyTransactionType.BONUS,
        )

        assert loyalty_service.calculate_qualifying_points(sample_user.id) == 150

    def test_excludes_transactions_older_than_window(self, loyalty_service, account, sample_user):
        now = datetime.now(timezone.utc)
        _lot(
            sample_user.id,
            100,
            remaining=100,
            created_at=now - timedelta(days=10),
            txn_type=LoyaltyTransactionType.EARNED,
        )
        # 400 days old -> outside the 365-day window.
        _lot(
            sample_user.id,
            999,
            remaining=999,
            created_at=now - timedelta(days=400),
            txn_type=LoyaltyTransactionType.EARNED,
        )

        assert loyalty_service.calculate_qualifying_points(sample_user.id) == 100

    def test_excludes_adjustment_refund_and_clawback_rows(
        self, loyalty_service, account, sample_user
    ):
        now = datetime.now(timezone.utc)
        _lot(
            sample_user.id,
            100,
            remaining=100,
            created_at=now - timedelta(days=5),
            txn_type=LoyaltyTransactionType.EARNED,
        )
        # Positive ADJUSTMENT (refund) row -> excluded from tier qualification.
        refund = LoyaltyTransaction(
            user_id=sample_user.id,
            transaction_type=LoyaltyTransactionType.ADJUSTMENT,
            points=50,
            remaining_points=50,
            description="reward refund",
        )
        # Negative ADJUSTMENT (clawback) row -> also excluded (points<0).
        clawback = LoyaltyTransaction(
            user_id=sample_user.id,
            transaction_type=LoyaltyTransactionType.ADJUSTMENT,
            points=-30,
            description="order edit clawback",
        )
        _db.session.add_all([refund, clawback])
        _db.session.commit()

        assert loyalty_service.calculate_qualifying_points(sample_user.id) == 100

    def test_excludes_expired_type_rows(self, loyalty_service, account, sample_user):
        now = datetime.now(timezone.utc)
        _lot(
            sample_user.id,
            100,
            remaining=100,
            created_at=now - timedelta(days=5),
            txn_type=LoyaltyTransactionType.EARNED,
        )
        # EXPIRED-type row -> not EARNED/BONUS, excluded.
        expired_row = LoyaltyTransaction(
            user_id=sample_user.id,
            transaction_type=LoyaltyTransactionType.EXPIRED,
            points=200,
            remaining_points=0,
            description="expired",
        )
        _db.session.add(expired_row)
        _db.session.commit()

        assert loyalty_service.calculate_qualifying_points(sample_user.id) == 100
