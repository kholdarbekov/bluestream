"""Unit tests for LoyaltyService referrals, gifting, bonuses, and streaks.

Characterization tests of the EXISTING LoyaltyService behavior. They assert what
the code actually does today (see business_app/services/loyalty_service.py).

Notification side effects are monkeypatched to no-ops in every test that awards
or deducts points so no Celery task is enqueued.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyTransaction,
    ReferralProgram,
)
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.password_security import hash_password
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus, UserRole, UserType


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture(autouse=True)
def _silence_notifications(loyalty_service, monkeypatch):
    """Make every notification path a no-op so no Celery task is enqueued."""
    monkeypatch.setattr(loyalty_service, "_send_points_notification", lambda *a, **k: None)
    monkeypatch.setattr(loyalty_service, "_send_tier_upgrade_notification", lambda *a, **k: None)
    monkeypatch.setattr(loyalty_service, "_send_points_expiry_notification", lambda *a, **k: None)
    return loyalty_service


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


def _make_user(db, *, suffix, phone=None, referral_code=None, date_of_birth=None):
    """Create an extra verified customer (sample_user covers user #1)."""
    user = User(
        email=f"loyalty{suffix}@example.com",
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="Loyalty",
        last_name=str(suffix),
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        referral_code=referral_code,
        date_of_birth=date_of_birth,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _seed_account_with_lot(db, program, user, points):
    """Give ``user`` a loyalty account backed by a real far-future EARNED lot."""
    account = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        total_earned=points,
        total_redeemed=0,
        total_expired=0,
        current_balance=points,
        current_tier="Bronze",
        points_to_next_tier=0,
    )
    db.session.add(account)
    db.session.flush()
    lot = LoyaltyTransaction(
        user_id=user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=points,
        remaining_points=points,
        description="seed",
        expires_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
    )
    db.session.add(lot)
    db.session.commit()
    return account


def _delivered_order(db, user, *, number):
    # Referral qualification requires DELIVERED + fully paid (is_paid).
    order = Order(
        user_id=user.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        is_paid=True,
    )
    db.session.add(order)
    db.session.commit()
    return order


# ---------------------------------------------------------------------------
# process_referral
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestProcessReferral:
    def test_invalid_code_raises_validation_error(self, loyalty_service, db, sample_user):
        with pytest.raises(ValidationError, match="Invalid referral code"):
            loyalty_service.process_referral("NOPE123", sample_user.id)

    def test_self_referral_raises_validation_error(self, loyalty_service, db, sample_user):
        sample_user.referral_code = "REFSELF1"
        db.session.commit()
        with pytest.raises(ValidationError, match="Cannot refer yourself"):
            loyalty_service.process_referral("REFSELF1", sample_user.id)

    def test_missing_referee_raises_not_found(self, loyalty_service, db, sample_user):
        sample_user.referral_code = "REFCODE1"
        db.session.commit()
        with pytest.raises(NotFoundError, match="Referee user not found"):
            loyalty_service.process_referral("REFCODE1", 999999)

    def test_already_referred_raises_conflict(self, loyalty_service, db, sample_user, loyalty_program):
        sample_user.referral_code = "REFCODE2"
        db.session.commit()
        referee = _make_user(db, suffix="ref_dup")
        referee.referred_by_user_id = sample_user.id
        db.session.commit()
        with pytest.raises(ConflictError, match="already used a referral code"):
            loyalty_service.process_referral("REFCODE2", referee.id)

    def test_success_creates_pending_referral_and_snapshots_bonuses(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        sample_user.referral_code = "REFCODE3"
        db.session.commit()
        referee = _make_user(db, suffix="ref_new")

        result = loyalty_service.process_referral("REFCODE3", referee.id)

        assert result["status"] == "pending"
        # referrer bonus = referral_bonus (50); referee bonus = referral_bonus // 2 (25)
        assert result["referrer_points"] == 50
        assert result["referee_points"] == 25

        referral = ReferralProgram.query.get(result["referral_id"])
        assert referral.status == "pending"
        assert referral.referrer_id == sample_user.id
        assert referral.referee_id == referee.id
        assert referral.referral_code == "REFCODE3"
        assert referral.referrer_bonus_points == 50
        assert referral.referee_bonus_points == 25

        db.session.refresh(referee)
        assert referee.referred_by_user_id == sample_user.id

    def test_success_does_not_award_points_until_delivery(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        sample_user.referral_code = "REFCODE4"
        db.session.commit()
        referee = _make_user(db, suffix="ref_pending")

        loyalty_service.process_referral("REFCODE4", referee.id)

        # No BONUS transactions are created at referral time (pending until delivery).
        bonus_txns = LoyaltyTransaction.query.filter_by(
            transaction_type=LoyaltyTransactionType.BONUS
        ).count()
        assert bonus_txns == 0


# ---------------------------------------------------------------------------
# process_pending_referrals
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestProcessPendingReferrals:
    def test_grants_both_bonuses_when_referee_has_delivered_order(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        sample_user.referral_code = "REFDONE1"
        db.session.commit()
        referee = _make_user(db, suffix="ref_delivered")
        loyalty_service.process_referral("REFDONE1", referee.id)
        _delivered_order(db, referee, number="ORD-REF-D1")

        result = loyalty_service.process_pending_referrals()

        assert result["processed_count"] == 1
        # 50 (referrer) + 25 (referee)
        assert result["total_points_awarded"] == 75

        referral = ReferralProgram.query.filter_by(referee_id=referee.id).first()
        assert referral.status == "completed"
        assert referral.completed_at is not None
        assert referral.first_order_id is not None

        referrer_account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        referee_account = LoyaltyPoints.query.filter_by(user_id=referee.id).first()
        assert referrer_account.current_balance == 50
        assert referee_account.current_balance == 25

        # Referral awards are BONUS transactions (REFERRAL action type).
        referrer_bonus = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).first()
        assert referrer_bonus.points == 50

    def test_referral_without_delivered_order_stays_pending(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        sample_user.referral_code = "REFPEND1"
        db.session.commit()
        referee = _make_user(db, suffix="ref_no_order")
        loyalty_service.process_referral("REFPEND1", referee.id)
        # A non-delivered order does NOT qualify.
        order = Order(user_id=referee.id, order_number="ORD-REF-P1", status=OrderStatus.PENDING)
        db.session.add(order)
        db.session.commit()

        result = loyalty_service.process_pending_referrals()

        assert result["processed_count"] == 0
        referral = ReferralProgram.query.filter_by(referee_id=referee.id).first()
        assert referral.status == "pending"
        assert referral.completed_at is None


# ---------------------------------------------------------------------------
# referral code + statistics + points earned
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestReferralCodeAndStatistics:
    def test_get_user_referral_code_returns_existing_stable_code(self, loyalty_service, db, sample_user):
        sample_user.referral_code = "REFSTBL1"
        db.session.commit()
        assert loyalty_service.get_user_referral_code(sample_user.id) == "REFSTBL1"

    def test_get_user_referral_code_generates_and_persists_when_missing(
        self, loyalty_service, db, sample_user
    ):
        assert sample_user.referral_code is None
        code = loyalty_service.get_user_referral_code(sample_user.id)
        assert code.startswith("REF")
        db.session.refresh(sample_user)
        assert sample_user.referral_code == code
        # Stable across calls.
        assert loyalty_service.get_user_referral_code(sample_user.id) == code

    def test_get_user_referral_code_missing_user_raises_not_found(self, loyalty_service, db):
        with pytest.raises(NotFoundError, match="User not found"):
            loyalty_service.get_user_referral_code(999999)

    def test_get_referral_statistics_counts_completed_pending_and_points(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        completed = ReferralProgram(
            referrer_id=sample_user.id,
            referee_id=_make_user(db, suffix="stat_a").id,
            referral_code="X",
            status="completed",
            referrer_bonus_points=50,
        )
        pending = ReferralProgram(
            referrer_id=sample_user.id,
            referee_id=_make_user(db, suffix="stat_b").id,
            referral_code="X",
            status="pending",
        )
        db.session.add_all([completed, pending])
        # A referral BONUS transaction with "referral" in the description counts.
        db.session.add(
            LoyaltyTransaction(
                user_id=sample_user.id,
                transaction_type=LoyaltyTransactionType.BONUS,
                points=50,
                description="Referral bonus for user #2",
            )
        )
        db.session.commit()

        stats = loyalty_service.get_referral_statistics(sample_user.id)

        assert stats["total_referrals"] == 1
        assert stats["pending_referrals"] == 1
        assert stats["points_earned_from_referrals"] == 50

    def test_get_referral_points_earned_returns_zero_without_referral(
        self, loyalty_service, db, sample_user
    ):
        other = _make_user(db, suffix="rpe_none")
        assert loyalty_service.get_referral_points_earned(sample_user.id, other.id) == 0

    def test_get_referral_points_earned_returns_transaction_points(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        referee = _make_user(db, suffix="rpe_has")
        db.session.add(
            ReferralProgram(
                referrer_id=sample_user.id,
                referee_id=referee.id,
                referral_code="X",
                status="completed",
            )
        )
        db.session.add(
            LoyaltyTransaction(
                user_id=sample_user.id,
                transaction_type=LoyaltyTransactionType.BONUS,
                points=50,
                description="Referral bonus for user",
                extra_data={"action_type": LoyaltyActionType.REFERRAL.value},
            )
        )
        db.session.commit()

        assert loyalty_service.get_referral_points_earned(sample_user.id, referee.id) == 50


# ---------------------------------------------------------------------------
# gift_points
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGiftPoints:
    def test_moves_points_from_sender_to_recipient(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 500)
        recipient = _make_user(db, suffix="gift_recv")

        txn = loyalty_service.gift_points(sample_user.id, recipient.id, 200, "Happy birthday")

        sender_account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        recipient_account = LoyaltyPoints.query.filter_by(user_id=recipient.id).first()

        # Sender drawn down via a REDEEMED transaction; recipient credited via BONUS.
        assert sender_account.current_balance == 300
        assert sender_account.total_redeemed == 200
        assert recipient_account.current_balance == 200
        assert recipient_account.total_earned == 200

        assert txn.points == 200
        assert txn.transaction_type == LoyaltyTransactionType.BONUS  # awarded as WELCOME_BONUS

        deduction = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.REDEEMED
        ).first()
        assert deduction.points == -200

    def test_gift_transactions_are_not_attributed_to_an_order(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        """A gift has no order, so neither leg may populate the orders.id FK.

        Regression: gift_points routed the recipient's user id into deduct_points'
        reference_id, which is written straight into LoyaltyTransaction.order_id — so a
        gift was either rejected by the FK or silently attributed to an unrelated order
        that happened to share the id.
        """
        _seed_account_with_lot(db, loyalty_program, sample_user, 500)
        recipient = _make_user(db, suffix="gift_order_fk")

        credit = loyalty_service.gift_points(sample_user.id, recipient.id, 200, "Happy birthday")

        deduction = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.REDEEMED
        ).first()
        assert deduction.order_id is None
        assert credit.order_id is None

    def test_insufficient_sender_balance_raises_validation_error(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 100)
        recipient = _make_user(db, suffix="gift_poor")

        with pytest.raises(ValidationError, match="Insufficient points"):
            loyalty_service.gift_points(sample_user.id, recipient.id, 200, "too much")


# ---------------------------------------------------------------------------
# gift_points_by_phone
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGiftPointsByPhone:
    def test_non_positive_amount_raises_validation_error(self, loyalty_service, db, sample_user):
        with pytest.raises(ValidationError, match="Points amount must be positive"):
            loyalty_service.gift_points_by_phone(sample_user.id, "+998901112233", 0)

    def test_insufficient_balance_raises_validation_error(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 50)
        with pytest.raises(ValidationError, match="Insufficient points"):
            loyalty_service.gift_points_by_phone(sample_user.id, "+998901112233", 200)

    def test_invalid_phone_raises_validation_error(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 500)
        with pytest.raises(ValidationError):
            loyalty_service.gift_points_by_phone(sample_user.id, "not-a-phone", 100)

    def test_unknown_recipient_raises_not_found(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 500)
        # Valid UZ mobile that no user owns.
        with pytest.raises(NotFoundError, match="Recipient not found"):
            loyalty_service.gift_points_by_phone(sample_user.id, "+998907654321", 100)

    def test_recipient_equals_sender_raises_validation_error(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 500)
        # sample_user.phone is +998901234567 in conftest.
        with pytest.raises(ValidationError, match="Cannot gift to self"):
            loyalty_service.gift_points_by_phone(sample_user.id, "+998901234567", 100)

    def test_success_resolves_recipient_by_phone_and_delegates(
        self, loyalty_service, db, sample_user, loyalty_program
    ):
        _seed_account_with_lot(db, loyalty_program, sample_user, 500)
        recipient = _make_user(db, suffix="phone_recv", phone="+998907654321")

        txn = loyalty_service.gift_points_by_phone(sample_user.id, "+998907654321", 150, "gift")

        assert txn.points == 150
        recipient_account = LoyaltyPoints.query.filter_by(user_id=recipient.id).first()
        sender_account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        assert recipient_account.current_balance == 150
        assert sender_account.current_balance == 350


# ---------------------------------------------------------------------------
# grant_welcome_bonus
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGrantWelcomeBonus:
    def test_grants_signup_bonus_once(self, loyalty_service, db, sample_user, loyalty_program):
        granted = loyalty_service.grant_welcome_bonus(sample_user.id)

        assert granted == 100  # program.signup_bonus
        account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        assert account.current_balance == 100
        assert account.total_earned == 100

        bonus = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).first()
        assert bonus.points == 100
        assert (bonus.extra_data or {}).get("action_type") == LoyaltyActionType.WELCOME_BONUS.value

    def test_second_call_is_idempotent_no_op(self, loyalty_service, db, sample_user, loyalty_program):
        loyalty_service.grant_welcome_bonus(sample_user.id)

        second = loyalty_service.grant_welcome_bonus(sample_user.id)

        assert second == 0
        account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        assert account.current_balance == 100  # unchanged
        bonus_count = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).count()
        assert bonus_count == 1

    def test_returns_zero_when_no_bonus_configured(self, loyalty_service, db, sample_user):
        program = LoyaltyProgram(
            name="No-bonus Program",
            is_active=True,
            is_default=True,
            signup_bonus=0,
        )
        db.session.add(program)
        db.session.commit()

        assert loyalty_service.grant_welcome_bonus(sample_user.id) == 0
        bonus_count = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).count()
        assert bonus_count == 0


# ---------------------------------------------------------------------------
# grant_birthday_bonuses
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGrantBirthdayBonuses:
    def test_grants_bonus_to_birthday_user_only(self, loyalty_service, db, loyalty_program):
        business_tz = ZoneInfo(DISPLAY_TIMEZONE)
        today_local = datetime.now(business_tz)
        # A user whose month/day match today (any year) -> birthday today.
        birthday_user = _make_user(
            db,
            suffix="bday_today",
            date_of_birth=datetime(1990, today_local.month, today_local.day, tzinfo=business_tz),
        )
        # A user whose birthday is clearly NOT today (offset by ~6 months).
        other_local = today_local + timedelta(days=183)
        non_birthday_user = _make_user(
            db,
            suffix="bday_other",
            date_of_birth=datetime(1990, other_local.month, other_local.day, tzinfo=business_tz),
        )

        result = loyalty_service.grant_birthday_bonuses()

        assert result["granted"] == 1
        bday_account = LoyaltyPoints.query.filter_by(user_id=birthday_user.id).first()
        assert bday_account is not None
        assert bday_account.current_balance == 25  # program.birthday_bonus

        # Non-birthday user gets nothing (no account created by an award).
        non_bday_account = LoyaltyPoints.query.filter_by(user_id=non_birthday_user.id).first()
        assert non_bday_account is None

    def test_idempotent_within_same_year(self, loyalty_service, db, loyalty_program):
        business_tz = ZoneInfo(DISPLAY_TIMEZONE)
        today_local = datetime.now(business_tz)
        _make_user(
            db,
            suffix="bday_idem",
            date_of_birth=datetime(1985, today_local.month, today_local.day, tzinfo=business_tz),
        )

        first = loyalty_service.grant_birthday_bonuses()
        second = loyalty_service.grant_birthday_bonuses()

        assert first["granted"] == 1
        assert second["granted"] == 0  # already granted this calendar year

    def test_returns_zero_when_bonus_not_configured(self, loyalty_service, db):
        business_tz = ZoneInfo(DISPLAY_TIMEZONE)
        today_local = datetime.now(business_tz)
        program = LoyaltyProgram(
            name="No-birthday Program",
            is_active=True,
            is_default=True,
            birthday_bonus=0,
        )
        db.session.add(program)
        _make_user(
            db,
            suffix="bday_nocfg",
            date_of_birth=datetime(1992, today_local.month, today_local.day, tzinfo=business_tz),
        )
        db.session.commit()

        assert loyalty_service.grant_birthday_bonuses() == {"granted": 0}


# Streak earning is now rule-based; see tests/unit/test_loyalty_streak_rules.py
