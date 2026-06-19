"""Unit E — bonus-amount SSOT (owner decision 2026-06-14).

signup / referral / birthday bonus amounts come from the admin-editable
LoyaltyProgram DB columns (100 / 50 / 25; referee = referral // 2), NOT a
hardcoded action dict or the REFERRAL_BONUS_POINTS config.
"""

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType


@pytest.fixture
def service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def program(db):
    prog = LoyaltyProgram(
        name="Default",
        description="d",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        signup_bonus=100,
        referral_bonus=50,
        birthday_bonus=25,
    )
    db.session.add(prog)
    db.session.commit()
    return prog


@pytest.mark.unit
class TestBonusAmountSSOT:
    def test_get_action_points_referral_reads_db_column(self, service, program, db):
        assert service.get_action_points("referral_signup") == 50  # not the hardcoded 500

    def test_get_action_points_birthday_reads_db_column(self, service, program, db):
        assert service.get_action_points("birthday_bonus") == 25  # not the hardcoded 200

    def test_get_action_points_reflects_admin_edit(self, service, program, db):
        program.referral_bonus = 75
        program.birthday_bonus = 40
        db.session.commit()
        assert service.get_action_points("referral_signup") == 75
        assert service.get_action_points("birthday_bonus") == 40

    def test_referrer_and_referee_bonus_from_db(self, service, program, db):
        assert service.get_referrer_bonus_points() == 50  # not config 500
        assert service.get_referee_bonus_points() == 25  # referral // 2

    def test_get_or_create_does_not_grant_welcome_bonus(self, service, program, db, sample_user, monkeypatch):
        """get_or_create must be a pure get-or-create (no read-path ledger mutation)."""
        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        program.signup_bonus = 100
        db.session.commit()

        acc = service.get_or_create_loyalty_account(sample_user.id)
        db.session.refresh(acc)
        assert acc.current_balance == 0  # no grant from a get

    def test_welcome_bonus_granted_once(self, service, program, db, sample_user, monkeypatch):
        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        program.signup_bonus = 100
        db.session.commit()

        granted = service.grant_welcome_bonus(sample_user.id)
        assert granted == 100
        acc = service.get_or_create_loyalty_account(sample_user.id)
        db.session.refresh(acc)
        assert acc.current_balance == 100

        # Idempotent: a second call must NOT re-grant.
        assert service.grant_welcome_bonus(sample_user.id) == 0
        bonus_count = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).count()
        assert bonus_count == 1

    def test_no_welcome_bonus_when_signup_bonus_zero(self, service, program, db, sample_user, monkeypatch):
        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        program.signup_bonus = 0
        db.session.commit()

        assert service.grant_welcome_bonus(sample_user.id) == 0

    def test_birthday_bonus_granted_once_to_users_with_birthday_today(self, service, program, db, sample_user, monkeypatch):
        from datetime import datetime, timezone

        monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
        program.birthday_bonus = 25
        program.signup_bonus = 0  # isolate the birthday grant from the welcome grant
        db.session.commit()

        today = datetime.now(timezone.utc)
        sample_user.date_of_birth = today.replace(year=2000)
        db.session.commit()

        result = service.grant_birthday_bonuses()
        assert result["granted"] == 1

        bonus = LoyaltyTransaction.query.filter_by(
            user_id=sample_user.id, transaction_type=LoyaltyTransactionType.BONUS
        ).all()
        assert len(bonus) == 1
        assert bonus[0].points == 25

        # Idempotent within the same year.
        result2 = service.grant_birthday_bonuses()
        assert result2["granted"] == 0
