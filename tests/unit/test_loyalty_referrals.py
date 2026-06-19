"""P2-5 — referral subsystem rebuild.

Referral codes are persisted on User (SSOT). process_referral creates a PENDING
ReferralProgram row with the bonus amounts snapshotted from LoyaltyProgram, and
sets referee.referred_by_user_id. Bonuses are granted later (by the cron) once
the referee's first order is both DELIVERED and fully paid (is_paid).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTransaction, ReferralProgram
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, UserRole, UserType


@pytest.fixture
def service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(
        name="Default", description="d", is_active=True, is_default=True,
        uzs_per_point=250, signup_bonus=0, referral_bonus=50, birthday_bonus=25,
    )
    db.session.add(prog)
    db.session.commit()
    return prog


def _user(db, email, phone):
    u = User(
        email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
        first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.mark.unit
class TestReferrals:
    def test_get_user_referral_code_persists_and_is_stable(self, service, db, sample_user):
        code = service.get_user_referral_code(sample_user.id)
        assert code
        assert User.query.get(sample_user.id).referral_code == code
        assert service.get_user_referral_code(sample_user.id) == code  # stable

    def test_telegram_register_applies_deep_link_referral_code(self, client, program, db):
        # A new bot user arriving via t.me/<bot>?start=ref_CODE: the bot forwards
        # referral_code to telegram-register, which must create the pending referral.
        referrer = _user(db, "ref-tg@e.com", "+998900000010")
        code = LoyaltyService().get_user_referral_code(referrer.id)

        resp = client.post(
            "/api/v1/auth/telegram-register",
            json={
                "telegram_id": 778899001,
                "first_name": "New",
                "last_name": "Referee",
                "language_code": "ru",
                "referral_code": code,
            },
        )

        assert resp.status_code == 201
        new_user = User.query.filter_by(telegram_id="778899001").first()
        assert new_user is not None
        assert new_user.referred_by_user_id == referrer.id
        assert ReferralProgram.query.filter_by(referee_id=new_user.id, referrer_id=referrer.id).count() == 1

    def test_telegram_register_grants_welcome_bonus(self, client, service, program, db):
        # Telegram registration must grant the welcome/signup bonus, like the web
        # and phone registration paths — it previously didn't (the route omitted it).
        program.signup_bonus = 100
        db.session.commit()

        resp = client.post(
            "/api/v1/auth/telegram-register",
            json={"telegram_id": 778899050, "first_name": "WB", "language_code": "en"},
        )

        assert resp.status_code == 201
        new_user = User.query.filter_by(telegram_id="778899050").first()
        assert new_user is not None
        assert service.get_available_points(new_user.id) == 100
        bonuses = LoyaltyTransaction.query.filter_by(
            user_id=new_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).all()
        assert any(
            (t.extra_data or {}).get("action_type") == LoyaltyActionType.WELCOME_BONUS.value
            for t in bonuses
        )

    def test_telegram_register_succeeds_when_referral_code_invalid(self, client, db):
        # Bad/invalid codes must not block registration (non-fatal).
        resp = client.post(
            "/api/v1/auth/telegram-register",
            json={
                "telegram_id": 778899002,
                "first_name": "New",
                "language_code": "en",
                "referral_code": "DOES-NOT-EXIST",
            },
        )

        assert resp.status_code == 201
        new_user = User.query.filter_by(telegram_id="778899002").first()
        assert new_user is not None
        assert new_user.referred_by_user_id is None

    def test_process_referral_creates_pending_with_persisted_amounts(self, service, program, db):
        referrer = _user(db, "ref@e.com", "+998900000001")
        referee = _user(db, "ree@e.com", "+998900000002")
        referrer.referral_code = "REFABC"
        db.session.commit()

        result = service.process_referral("REFABC", referee.id)

        assert result["status"] == "pending"
        ref = ReferralProgram.query.filter_by(referee_id=referee.id).first()
        assert ref is not None
        assert ref.status == "pending"
        assert ref.referrer_id == referrer.id
        assert ref.referrer_bonus_points == 50
        assert ref.referee_bonus_points == 25  # referral // 2
        assert User.query.get(referee.id).referred_by_user_id == referrer.id
        # No points granted yet (pending until first delivered order).
        assert service.get_available_points(referrer.id) == 0

    def test_process_referral_rejects_invalid_code(self, service, program, db):
        referee = _user(db, "ree2@e.com", "+998900000003")
        with pytest.raises(ValidationError):
            service.process_referral("NOPE", referee.id)

    def test_process_referral_rejects_self_referral(self, service, program, db):
        u = _user(db, "self@e.com", "+998900000004")
        u.referral_code = "SELF1"
        db.session.commit()
        with pytest.raises(ValidationError):
            service.process_referral("SELF1", u.id)

    def test_process_referral_rejects_double_referral(self, service, program, db):
        r1 = _user(db, "r1@e.com", "+998900000005")
        r2 = _user(db, "r2@e.com", "+998900000006")
        referee = _user(db, "ree3@e.com", "+998900000007")
        r1.referral_code = "R1CODE"
        r2.referral_code = "R2CODE"
        db.session.commit()
        service.process_referral("R1CODE", referee.id)
        with pytest.raises(ConflictError):
            service.process_referral("R2CODE", referee.id)

    def test_referrer_can_refer_multiple_referees(self, service, program, db):
        """A referrer's code is reused across referees — no unique-constraint cap at 1."""
        referrer = _user(db, "multi@e.com", "+998900000020")
        r1 = _user(db, "m1@e.com", "+998900000021")
        r2 = _user(db, "m2@e.com", "+998900000022")
        referrer.referral_code = "MULTI1"
        db.session.commit()

        service.process_referral("MULTI1", r1.id)
        service.process_referral("MULTI1", r2.id)  # must NOT raise/collide

        assert ReferralProgram.query.filter_by(referrer_id=referrer.id).count() == 2

    def test_earlier_non_delivered_order_does_not_block_referral_payout(self, service, program, db, monkeypatch):
        """An earlier pending/cancelled order must not freeze the referral; a later
        delivered order still pays out."""
        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        referrer = _user(db, "blk@e.com", "+998900000023")
        referee = _user(db, "blkr@e.com", "+998900000024")
        referrer.referral_code = "BLKCODE"
        db.session.commit()
        service.process_referral("BLKCODE", referee.id)

        now = datetime.now(timezone.utc)
        pending_order = Order(
            user_id=referee.id, order_number="ORD-PEND", status=OrderStatus.PENDING,
            subtotal=Decimal("10000.00"), delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("10000.00"), created_at=now - timedelta(days=2),
        )
        db.session.add(pending_order)
        db.session.commit()

        # Earliest order is PENDING -> no payout, and must not pin first_order_id.
        assert service.process_pending_referrals()["processed_count"] == 0
        assert ReferralProgram.query.filter_by(referee_id=referee.id).first().status == "pending"

        delivered = Order(
            user_id=referee.id, order_number="ORD-DELIV", status=OrderStatus.DELIVERED,
            is_paid=True, paid_at=now,
            subtotal=Decimal("12000.00"), delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("12000.00"), created_at=now,
        )
        db.session.add(delivered)
        db.session.commit()

        assert service.process_pending_referrals()["processed_count"] == 1
        assert service.get_available_points(referrer.id) == 50

    def test_pending_referral_rewards_on_first_delivered_order(self, service, program, db, monkeypatch):
        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        referrer = _user(db, "rr@e.com", "+998900000008")
        referee = _user(db, "re@e.com", "+998900000009")
        referrer.referral_code = "RRCODE"
        db.session.commit()
        service.process_referral("RRCODE", referee.id)

        # Referee places a delivered AND fully paid order -> referral becomes eligible.
        order = Order(
            user_id=referee.id, order_number="ORD-REF-1", status=OrderStatus.DELIVERED,
            is_paid=True, paid_at=datetime.now(timezone.utc),
            subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("15000.00"), created_at=datetime.now(timezone.utc),
        )
        db.session.add(order)
        db.session.commit()

        result = service.process_pending_referrals()
        assert result["processed_count"] == 1
        assert result["total_points_awarded"] == 75  # 50 + 25
        assert service.get_available_points(referrer.id) == 50
        assert service.get_available_points(referee.id) == 25
        assert ReferralProgram.query.filter_by(referee_id=referee.id).first().status == "completed"

    def test_delivered_but_unpaid_order_does_not_complete_referral(self, service, program, db, monkeypatch):
        """A delivered order that is NOT fully paid (e.g. COD with cash collection
        still pending) must NOT complete the referral. Only Delivered + is_paid
        qualifies; once payment lands the referral pays out."""
        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        referrer = _user(db, "unpaid-rr@e.com", "+998900000030")
        referee = _user(db, "unpaid-re@e.com", "+998900000031")
        referrer.referral_code = "UNPAIDC"
        db.session.commit()
        service.process_referral("UNPAIDC", referee.id)

        now = datetime.now(timezone.utc)
        delivered_unpaid = Order(
            user_id=referee.id, order_number="ORD-DELIV-UNPAID", status=OrderStatus.DELIVERED,
            is_paid=False,
            subtotal=Decimal("12000.00"), delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("12000.00"), created_at=now - timedelta(days=1),
        )
        db.session.add(delivered_unpaid)
        db.session.commit()

        # Delivered but unpaid -> stays pending, no payout, first_order_id not pinned.
        assert service.process_pending_referrals()["processed_count"] == 0
        ref = ReferralProgram.query.filter_by(referee_id=referee.id).first()
        assert ref.status == "pending"
        assert ref.first_order_id is None
        assert service.get_available_points(referrer.id) == 0

        # Once that order is fully paid, the referral completes.
        delivered_unpaid.is_paid = True
        delivered_unpaid.paid_at = now
        db.session.commit()

        assert service.process_pending_referrals()["processed_count"] == 1
        completed = ReferralProgram.query.filter_by(referee_id=referee.id).first()
        assert completed.status == "completed"
        assert completed.first_order_id == delivered_unpaid.id
        assert service.get_available_points(referrer.id) == 50
