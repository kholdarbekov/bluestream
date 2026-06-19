"""Unit tests for the Commit-A foundation of the admin order-edit feature.

Covers:
  - LoyaltyService.reverse_earnings (clamp-to-balance, uncollectible audit)
  - DriverReconciliationService.reopen_session (cash session reopen)
  - BottleTrackingService.reopen_session (bottle session reopen)
  - OrderEditHistory model can be persisted
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.bottle import DriverBottleSession
from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTransaction
from business_app.models.order import OrderEditHistory
from business_app.models.payment import DriverCashSession
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.exceptions import ConflictError, ValidationError
from shared.enums import DriverBottleSessionStatus, DriverCashSessionStatus


# -------------------------------------------------------------------------
# LoyaltyService.reverse_earnings
# -------------------------------------------------------------------------


def _seed_loyalty_account(user_id: int, *, balance: int, earned: int = None) -> LoyaltyPoints:
    program = LoyaltyProgram.query.filter_by(is_default=True).first()
    if not program:
        program = LoyaltyProgram(
            name="Default",
            description="Default program",
            is_active=True,
            is_default=True,
            uzs_per_point=250,
        )
        _db.session.add(program)
        _db.session.commit()
    account = LoyaltyPoints(
        user_id=user_id,
        program_id=program.id,
        total_earned=earned if earned is not None else balance,
        total_redeemed=0,
        current_balance=balance,
        current_tier="Bronze",
        points_to_next_tier=0,
    )
    _db.session.add(account)
    _db.session.commit()
    return account


def test_reverse_earnings_no_diff_is_noop(db, sample_user):
    _seed_loyalty_account(sample_user.id, balance=100, earned=100)
    service = LoyaltyService()

    result = service.reverse_earnings(
        user_id=sample_user.id,
        order_id=1,
        old_points_earned=50,
        new_points_earned=50,
    )

    assert result["diff"] == 0
    assert result["clawback"] == 0
    assert result["uncollectible"] == 0
    assert result["transaction_id"] is None
    assert LoyaltyTransaction.query.filter_by(user_id=sample_user.id).count() == 0


def test_reverse_earnings_clamps_when_balance_insufficient(db, sample_user):
    # User earned 50 originally, has only 20 left (spent 30). Edit drops
    # earnings to 10 → diff is 40 but only 20 collectible.
    _seed_loyalty_account(sample_user.id, balance=20, earned=50)
    service = LoyaltyService()

    result = service.reverse_earnings(
        user_id=sample_user.id,
        order_id=42,
        old_points_earned=50,
        new_points_earned=10,
        clamp=True,
    )

    assert result["diff"] == 40
    assert result["clawback"] == 20
    assert result["uncollectible"] == 20
    assert result["transaction_id"] is not None

    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    assert account.current_balance == 0  # clamped, never negative
    # M1: a clawback reverses earning, so it reduces lifetime total_earned
    # (symmetric with the order-edit award branch) — NOT total_redeemed, which is
    # reserved for actual reward redemptions.
    assert account.total_redeemed == 0
    assert account.total_earned == 30  # 50 earned - 20 clawed back

    txn = LoyaltyTransaction.query.get(result["transaction_id"])
    assert txn.points == -20
    assert txn.transaction_type == LoyaltyTransactionType.ADJUSTMENT
    assert txn.order_id == 42
    assert txn.extra_data["uncollectible"] == 20
    assert txn.extra_data["clamped"] is True


def test_reverse_earnings_full_clawback_when_balance_sufficient(db, sample_user):
    _seed_loyalty_account(sample_user.id, balance=100, earned=100)
    service = LoyaltyService()

    result = service.reverse_earnings(
        user_id=sample_user.id,
        order_id=7,
        old_points_earned=80,
        new_points_earned=30,
    )

    assert result["diff"] == 50
    assert result["clawback"] == 50
    assert result["uncollectible"] == 0

    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    assert account.current_balance == 50


def test_reverse_earnings_awards_when_new_exceeds_old(db, sample_user):
    _seed_loyalty_account(sample_user.id, balance=10, earned=20)
    service = LoyaltyService()

    result = service.reverse_earnings(
        user_id=sample_user.id,
        order_id=9,
        old_points_earned=20,
        new_points_earned=55,
    )

    assert result["diff"] == -35
    assert result["clawback"] == 0
    assert result["uncollectible"] == 0
    assert result["award"] == 35

    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    assert account.current_balance == 45
    assert account.total_earned == 55


def test_reverse_earnings_rejects_negative_inputs(db, sample_user):
    _seed_loyalty_account(sample_user.id, balance=10)
    service = LoyaltyService()
    with pytest.raises(ValidationError):
        service.reverse_earnings(
            user_id=sample_user.id,
            order_id=1,
            old_points_earned=-1,
            new_points_earned=5,
        )


# -------------------------------------------------------------------------
# DriverReconciliationService.reopen_session
# -------------------------------------------------------------------------


def _seed_cash_session(driver_user_id: int, *, status: DriverCashSessionStatus) -> DriverCashSession:
    session = DriverCashSession(
        driver_user_id=driver_user_id,
        status=status,
        session_started_at=datetime.now(UTC) - timedelta(hours=2),
        expected_cash=Decimal("100000.00"),
        gross_cash_collected=Decimal("100000.00"),
        expected_cash_on_hand=Decimal("100000.00"),
        declared_cash=Decimal("100000.00"),
        verified_cash=Decimal("100000.00"),
        submitted_at=datetime.now(UTC),
        verified_at=datetime.now(UTC),
        verification_notes="Looks good",
        verification_reason_code="cash_count_matched",
        blocked_from_cod=False,
    )
    _db.session.add(session)
    _db.session.commit()
    return session


def test_reopen_cash_session_clears_verification_and_increments_count(
    db, delivery_driver, admin_user
):
    session = _seed_cash_session(delivery_driver.id, status=DriverCashSessionStatus.VERIFIED)
    service = DriverReconciliationService()

    reopened = service.reopen_session(
        session_id=session.id,
        actor_user_id=admin_user.id,
        reason="order #42 quantity adjusted post-delivery",
    )

    assert reopened.status == DriverCashSessionStatus.OPEN
    assert reopened.reopen_count == 1
    assert reopened.reopened_by_user_id == admin_user.id
    assert reopened.reopened_reason == "order #42 quantity adjusted post-delivery"
    assert reopened.submitted_at is None
    assert reopened.verified_at is None
    assert reopened.verification_notes is None
    assert reopened.verification_reason_code is None


def test_reopen_cash_session_blocks_when_active_session_exists(
    db, delivery_driver, admin_user
):
    verified = _seed_cash_session(delivery_driver.id, status=DriverCashSessionStatus.VERIFIED)
    active = DriverCashSession(
        driver_user_id=delivery_driver.id,
        status=DriverCashSessionStatus.OPEN,
        session_started_at=datetime.now(UTC),
    )
    _db.session.add(active)
    _db.session.commit()

    service = DriverReconciliationService()
    with pytest.raises(ConflictError):
        service.reopen_session(
            session_id=verified.id,
            actor_user_id=admin_user.id,
            reason="retroactive adjustment",
        )


def test_reopen_cash_session_rejects_non_reopenable_status(
    db, delivery_driver, admin_user
):
    session = DriverCashSession(
        driver_user_id=delivery_driver.id,
        status=DriverCashSessionStatus.OPEN,
        session_started_at=datetime.now(UTC),
    )
    _db.session.add(session)
    _db.session.commit()
    service = DriverReconciliationService()
    with pytest.raises(ValidationError):
        service.reopen_session(
            session_id=session.id,
            actor_user_id=admin_user.id,
            reason="cannot reopen already-open session",
        )


def test_reopen_cash_session_requires_reason(db, delivery_driver, admin_user):
    session = _seed_cash_session(delivery_driver.id, status=DriverCashSessionStatus.VERIFIED)
    service = DriverReconciliationService()
    with pytest.raises(ValidationError):
        service.reopen_session(
            session_id=session.id,
            actor_user_id=admin_user.id,
            reason="   ",
        )


# -------------------------------------------------------------------------
# BottleTrackingService.reopen_session
# -------------------------------------------------------------------------


def _seed_bottle_session(
    driver_user_id: int, *, status: DriverBottleSessionStatus
) -> DriverBottleSession:
    is_open = status == DriverBottleSessionStatus.OPEN
    session = DriverBottleSession(
        driver_user_id=driver_user_id,
        status=status,
        bottles_loaded=20,
        bottles_delivered=18,
        bottles_collected_from_customers=12,
        bottles_returned_to_warehouse=None if is_open else 14,
        closed_at=None if is_open else datetime.now(UTC),
        closed_by_user_id=None if is_open else driver_user_id,
        discrepancy=None if is_open else 0,
        started_at=datetime.now(UTC) - timedelta(hours=4),
    )
    _db.session.add(session)
    _db.session.commit()
    return session


def test_reopen_bottle_session_resets_close_state(db, delivery_driver, admin_user):
    session = _seed_bottle_session(delivery_driver.id, status=DriverBottleSessionStatus.CLOSED)
    service = BottleTrackingService()

    reopened = service.reopen_session(
        session_id=session.id,
        actor_user_id=admin_user.id,
        reason="customer added 2 bottles after delivery",
    )

    assert reopened.status == DriverBottleSessionStatus.OPEN
    assert reopened.reopen_count == 1
    assert reopened.reopened_by_user_id == admin_user.id
    assert reopened.reopened_reason == "customer added 2 bottles after delivery"
    assert reopened.closed_at is None
    assert reopened.closed_by_user_id is None
    assert reopened.bottles_returned_to_warehouse is None
    assert reopened.discrepancy is None
    assert reopened.force_closed is False


def test_reopen_bottle_session_blocks_when_driver_has_other_open(
    db, delivery_driver, admin_user
):
    closed = _seed_bottle_session(delivery_driver.id, status=DriverBottleSessionStatus.CLOSED)
    active = DriverBottleSession(
        driver_user_id=delivery_driver.id,
        status=DriverBottleSessionStatus.OPEN,
        bottles_loaded=10,
        started_at=datetime.now(UTC),
    )
    _db.session.add(active)
    _db.session.commit()
    service = BottleTrackingService()
    with pytest.raises(ConflictError):
        service.reopen_session(
            session_id=closed.id,
            actor_user_id=admin_user.id,
            reason="x",
        )


def test_reopen_bottle_session_rejects_open_status(db, delivery_driver, admin_user):
    session = _seed_bottle_session(delivery_driver.id, status=DriverBottleSessionStatus.OPEN)
    service = BottleTrackingService()
    with pytest.raises(ValidationError):
        service.reopen_session(
            session_id=session.id,
            actor_user_id=admin_user.id,
            reason="cannot reopen open",
        )


def test_reopen_bottle_session_idempotent_count_across_multiple_reopens(
    db, delivery_driver, admin_user
):
    """Reopening, re-closing, and reopening again should increment reopen_count
    each time without corrupting session state."""
    service = BottleTrackingService()

    # First reopen
    session = _seed_bottle_session(delivery_driver.id, status=DriverBottleSessionStatus.CLOSED)
    service.reopen_session(
        session_id=session.id,
        actor_user_id=admin_user.id,
        reason="first order edit",
    )
    assert session.reopen_count == 1
    assert session.status == DriverBottleSessionStatus.OPEN

    # Re-close it (set fields back to CLOSED-like state)
    session.status = DriverBottleSessionStatus.CLOSED
    session.closed_at = datetime.now(UTC)
    session.bottles_returned_to_warehouse = 14
    session.discrepancy = 0
    _db.session.commit()

    # Second reopen
    service.reopen_session(
        session_id=session.id,
        actor_user_id=admin_user.id,
        reason="second order edit",
    )
    assert session.reopen_count == 2
    assert session.status == DriverBottleSessionStatus.OPEN
    assert session.closed_at is None
    assert session.bottles_returned_to_warehouse is None


def test_reopen_bottle_session_clears_force_close_flag(db, delivery_driver, admin_user):
    session = _seed_bottle_session(
        delivery_driver.id, status=DriverBottleSessionStatus.FORCE_CLOSED
    )
    session.force_closed = True
    session.force_close_reason = "abandoned"
    _db.session.commit()
    service = BottleTrackingService()

    reopened = service.reopen_session(
        session_id=session.id,
        actor_user_id=admin_user.id,
        reason="re-tally for order edit",
    )

    assert reopened.status == DriverBottleSessionStatus.OPEN
    assert reopened.force_closed is False


# -------------------------------------------------------------------------
# OrderEditHistory persistence smoke test
# -------------------------------------------------------------------------


def test_order_edit_history_persists_round_trip(db, sample_order, admin_user):
    entry = OrderEditHistory(
        order_id=sample_order.id,
        edited_by_user_id=admin_user.id,
        reason="customer added 1 bottle",
        diff={
            "items_before": [{"product_id": 1, "quantity": 4}],
            "items_after": [{"product_id": 1, "quantity": 5}],
            "totals_before": {"total_amount": 18000},
            "totals_after": {"total_amount": 22500},
            "cascade_summary": {"loyalty": {"award": 18}},
        },
        is_post_delivery=False,
    )
    _db.session.add(entry)
    _db.session.commit()

    rehydrated = OrderEditHistory.query.filter_by(order_id=sample_order.id).first()
    assert rehydrated is not None
    assert rehydrated.edited_by_user_id == admin_user.id
    assert rehydrated.is_post_delivery is False
    assert rehydrated.diff["items_after"][0]["quantity"] == 5
    payload = rehydrated.to_dict()
    assert payload["reason"] == "customer added 1 bottle"
    assert payload["diff"]["cascade_summary"]["loyalty"]["award"] == 18
