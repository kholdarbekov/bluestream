"""Configurable surprise rewards — nightly batch.

Surprise rewards are shared by a midnight Celery task that scans the day's orders
which are DELIVERED and fully PAID *within that delivery day* (prepaid or COD paid
the same day qualify; COD paid the next day does not). Awards go to INDIVIDUAL
customers only, one roll per eligible user per day, gated by a per-user cooldown
and a global per-day cap, with chance %, amounts, cooldown, and cap configurable
on LoyaltyProgram. Each award is a BONUS transaction (action_type=surprise_reward).
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from business_app.models.delivery import Delivery
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTransaction
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType

TZ = ZoneInfo(DISPLAY_TIMEZONE)
DAY = date(2026, 6, 10)


def _bl(y, mo, d, h=12):
    """A business-local wall-clock time as an aware UTC datetime."""
    return datetime(y, mo, d, h, tzinfo=TZ).astimezone(timezone.utc)


IN_DAY = _bl(2026, 6, 10, 12)      # noon of day D
PREV_DAY = _bl(2026, 6, 9, 12)     # day D-1 (prepaid, paid before delivery day)
NEXT_DAY = _bl(2026, 6, 11, 1)     # 01:00 of day D+1 (COD paid next day)
BEFORE_DAY = _bl(2026, 6, 8, 12)   # day D-2 (delivered earlier)


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture(autouse=True)
def _silence(loyalty_service, monkeypatch):
    monkeypatch.setattr(loyalty_service, "_send_points_notification", lambda *a, **k: None)
    monkeypatch.setattr(loyalty_service, "_send_tier_upgrade_notification", lambda *a, **k: None)
    return loyalty_service


@pytest.fixture
def program(db):
    p = LoyaltyProgram(
        name="Default Program", description="d", is_active=True, is_default=True,
        uzs_per_point=250, points_expiry_days=365, signup_bonus=100, referral_bonus=50, birthday_bonus=25,
        surprise_enabled=True, surprise_chance_percent=5, surprise_amounts="50,100,200",
        surprise_cooldown_days=7, surprise_daily_cap=5,
    )
    db.session.add(p)
    db.session.commit()
    return p


def _user(db, suffix, user_type=UserType.INDIVIDUAL):
    u = User(
        email=f"sr{suffix}@e.com", phone=f"+99890000{suffix:04d}",
        password_hash=hash_password("TestPassword123!"),
        first_name="S", last_name=str(suffix),
        user_type=user_type, role=UserRole.CUSTOMER, is_verified=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _order(db, user, *, number, delivered_at=IN_DAY, paid_at=IN_DAY,
           status=OrderStatus.DELIVERED, is_paid=True):
    """An order plus its Delivery row — the delivery day comes from Delivery.delivered_at."""
    o = Order(
        user_id=user.id, order_number=number, status=status, is_paid=is_paid, paid_at=paid_at,
    )
    db.session.add(o)
    db.session.commit()
    if delivered_at is not None:
        d = Delivery(
            order_id=o.id, scheduled_date=delivered_at,
            scheduled_time_slot="09:00-12:00", delivered_at=delivered_at,
            status=DeliveryStatus.DELIVERED,
        )
        db.session.add(d)
        db.session.commit()
    return o


def _force_win(monkeypatch, amount=100):
    monkeypatch.setattr("random.random", lambda: 0.0)
    monkeypatch.setattr("random.choice", lambda seq: amount)


def _force_lose(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.999)


@pytest.mark.unit
class TestSurpriseStoredAsBonus:
    def test_surprise_action_maps_to_bonus_transaction(self, loyalty_service, program, db):
        u = _user(db, 1)
        txn = loyalty_service.award_points(
            u.id, 100, "Surprise Reward! Thanks for being loyal 💙",
            LoyaltyActionType.SURPRICE_REWARD,
        )
        assert txn.transaction_type == LoyaltyTransactionType.BONUS


@pytest.mark.unit
class TestProcessDailySurpriseRewards:
    def test_prepaid_delivered_in_day_wins(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch, amount=200)
        u = _user(db, 2)
        _order(db, u, number="SR-PRE", delivered_at=IN_DAY, paid_at=PREV_DAY)  # paid before delivery day
        result = loyalty_service.process_daily_surprise_rewards(for_date=DAY)
        assert result["awarded"] == 1
        assert loyalty_service.get_available_points(u.id) == 200
        txn = LoyaltyTransaction.query.filter_by(user_id=u.id, transaction_type=LoyaltyTransactionType.BONUS).first()
        assert (txn.extra_data or {}).get("action_type") == LoyaltyActionType.SURPRICE_REWARD.value

    def test_cod_paid_same_day_wins(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch, amount=50)
        u = _user(db, 3)
        _order(db, u, number="SR-COD", delivered_at=IN_DAY, paid_at=IN_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 1
        assert loyalty_service.get_available_points(u.id) == 50

    def test_cod_paid_next_day_excluded(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch)
        u = _user(db, 4)
        _order(db, u, number="SR-LATE", delivered_at=IN_DAY, paid_at=NEXT_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0
        assert loyalty_service.get_available_points(u.id) == 0

    def test_delivered_before_day_excluded(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch)
        u = _user(db, 5)
        _order(db, u, number="SR-OLD", delivered_at=BEFORE_DAY, paid_at=BEFORE_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0

    def test_unpaid_excluded(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch)
        u = _user(db, 6)
        _order(db, u, number="SR-UNPAID", delivered_at=IN_DAY, paid_at=None, is_paid=False)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0

    def test_entity_customer_excluded(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch)
        u = _user(db, 7, user_type=UserType.ENTITY)
        _order(db, u, number="SR-ENT", delivered_at=IN_DAY, paid_at=IN_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0

    def test_disabled_program_awards_nothing(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch)
        program.surprise_enabled = False
        db.session.commit()
        u = _user(db, 8)
        _order(db, u, number="SR-OFF", delivered_at=IN_DAY, paid_at=IN_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0

    def test_lose_awards_nothing(self, loyalty_service, program, db, monkeypatch):
        _force_lose(monkeypatch)
        u = _user(db, 9)
        _order(db, u, number="SR-LOSE", delivered_at=IN_DAY, paid_at=IN_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0

    def test_cooldown_excludes_recent_winner(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch)
        u = _user(db, 10)
        # Prior surprise won recently (1 day ago) -> within the cooldown window
        # (cooldown is measured from now, the award moment).
        prior = LoyaltyTransaction(
            user_id=u.id, points=100, transaction_type=LoyaltyTransactionType.BONUS,
            description="Surprise Reward! Thanks for being loyal 💙",
            extra_data={"action_type": LoyaltyActionType.SURPRICE_REWARD.value},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.session.add(prior)
        db.session.commit()
        _order(db, u, number="SR-CD", delivered_at=IN_DAY, paid_at=IN_DAY)
        assert loyalty_service.process_daily_surprise_rewards(for_date=DAY)["awarded"] == 0

    def test_one_roll_per_user_per_day(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch, amount=100)
        u = _user(db, 11)
        _order(db, u, number="SR-A", delivered_at=IN_DAY, paid_at=IN_DAY)
        _order(db, u, number="SR-B", delivered_at=_bl(2026, 6, 10, 15), paid_at=IN_DAY)
        result = loyalty_service.process_daily_surprise_rewards(for_date=DAY)
        assert result["awarded"] == 1  # not 2
        assert loyalty_service.get_available_points(u.id) == 100

    def test_global_daily_cap_limits_total(self, loyalty_service, program, db, monkeypatch):
        _force_win(monkeypatch, amount=50)
        program.surprise_daily_cap = 1
        db.session.commit()
        u1 = _user(db, 12)
        u2 = _user(db, 13)
        _order(db, u1, number="SR-CAP1", delivered_at=IN_DAY, paid_at=IN_DAY)
        _order(db, u2, number="SR-CAP2", delivered_at=_bl(2026, 6, 10, 14), paid_at=IN_DAY)
        result = loyalty_service.process_daily_surprise_rewards(for_date=DAY)
        assert result["awarded"] == 1  # cap reached after the first
