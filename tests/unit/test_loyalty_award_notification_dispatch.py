"""D1 — post-commit SSOT dispatch of AquaCoins award notifications."""

from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.loyalty import LoyaltyProgram
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType


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
        points_expiry_days=365,
    )
    db.session.add(prog)
    db.session.commit()
    return prog


@pytest.mark.unit
def test_commit_false_award_enqueues_once_after_outer_commit(db, program, sample_user):
    service = LoyaltyService()
    with patch(
        "business_app.services.loyalty_service.LoyaltyService._send_points_notification"
    ) as mock_notify:
        # commit=False: the award lives inside the caller's transaction.
        service.award_points(
            sample_user.id,
            30,
            "Streak bonus",
            LoyaltyActionType.STREAK_BONUS,
            commit=False,
        )
        # Nothing dispatched yet — no commit has happened.
        assert mock_notify.call_count == 0
        # The pending entry is parked on the session.
        assert len(db.session.info.get("pending_loyalty_award_notifications", [])) == 1

        db.session.commit()  # outer transaction commits -> after_commit fires

    assert mock_notify.call_count == 1
    args, kwargs = mock_notify.call_args
    assert args[0] == sample_user.id          # user_id
    assert args[1] == 30                       # points
    assert kwargs.get("reason") == "streak_bonus"
    assert kwargs.get("balance") == 30         # new balance after award
    # Drained — a second commit must not re-dispatch.
    db.session.commit()
    assert mock_notify.call_count == 1


@pytest.mark.unit
def test_rolled_back_award_enqueues_nothing(db, program, sample_user):
    service = LoyaltyService()
    with patch(
        "business_app.services.loyalty_service.LoyaltyService._send_points_notification"
    ) as mock_notify:
        service.award_points(
            sample_user.id,
            30,
            "Streak bonus",
            LoyaltyActionType.STREAK_BONUS,
            commit=False,
        )
        db.session.rollback()  # after_rollback must discard the pending entry
        db.session.commit()

    assert mock_notify.call_count == 0
    assert db.session.info.get("pending_loyalty_award_notifications", []) == []


@pytest.mark.unit
def test_commit_true_award_dispatches_once(db, program, sample_user):
    service = LoyaltyService()
    with patch(
        "business_app.services.loyalty_service.LoyaltyService._send_points_notification"
    ) as mock_notify:
        service.award_points(
            sample_user.id,
            40,
            "Welcome bonus",
            LoyaltyActionType.WELCOME_BONUS,
            commit=True,
        )
    assert mock_notify.call_count == 1
    args, kwargs = mock_notify.call_args
    assert args[0] == sample_user.id
    assert args[1] == 40
    assert kwargs.get("reason") == "welcome_bonus"
    assert kwargs.get("balance") == 40
