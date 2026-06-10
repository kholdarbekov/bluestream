"""Regression tests for phone normalization in LoyaltyService.gift_points_by_phone.

A recipient stored in canonical E.164 (`+998901234567`) must be resolvable when
the caller passes a different surface format (`90 123 45 67`, `+998 90 123 45 67`).
Before the fix, the raw `filter_by(phone=recipient_phone)` lookup compared the
caller's surface string against the stored E.164 and silently missed the match.
"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for tests",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def sender_with_balance(db, loyalty_program):
    """Sender user holding a loyalty balance large enough to gift from."""
    sender = User(
        email="sender@example.com",
        phone="+998901111111",
        password_hash=hash_password("SenderPassword123!"),
        first_name="Sender",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(sender)
    db.session.commit()

    account = LoyaltyPoints(
        user_id=sender.id,
        program_id=loyalty_program.id,
        total_earned=1000,
        total_redeemed=0,
        total_expired=0,
        current_balance=1000,
        current_tier="Bronze",
        points_to_next_tier=500,
    )
    db.session.add(account)
    db.session.commit()
    return sender


@pytest.fixture
def recipient_e164(db):
    """Recipient stored in canonical E.164 form."""
    recipient = User(
        email="recipient@example.com",
        phone="+998901234567",
        password_hash=hash_password("RecipientPassword123!"),
        first_name="Recipient",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(recipient)
    db.session.commit()
    return recipient


@pytest.mark.unit
@pytest.mark.parametrize("surface_phone", ["90 123 45 67", "+998 90 123 45 67"])
def test_gift_points_by_phone_resolves_recipient_across_surface_formats(
    loyalty_service, sender_with_balance, recipient_e164, surface_phone
):
    """Caller's non-canonical surface format resolves to the E.164-stored recipient."""
    # Stub the inner transfer so the test targets the recipient lookup, not the
    # full earn/deduct machinery.
    loyalty_service.gift_points = Mock(return_value="ok")

    result = loyalty_service.gift_points_by_phone(
        sender_id=sender_with_balance.id,
        recipient_phone=surface_phone,
        points_amount=100,
    )

    assert result == "ok"
    loyalty_service.gift_points.assert_called_once()
    # The resolved recipient is the E.164-stored user, proving normalization ran.
    assert loyalty_service.gift_points.call_args.kwargs["recipient_id"] == recipient_e164.id


@pytest.mark.unit
def test_prefix_pre_fix_raw_lookup_would_miss(db, recipient_e164):
    """Document the pre-fix behavior: a raw filter_by on the surface format misses."""
    surface_phone = "+998 90 123 45 67"
    # Pre-fix code path: User.query.filter_by(phone=<surface string>) -> no match.
    assert User.query.filter_by(phone=surface_phone).first() is None
    # Post-fix path normalizes first, then matches.
    from business_app.utils.validators import normalize_phone_number

    normalized = normalize_phone_number(surface_phone)
    assert normalized == "+998901234567"
    assert User.query.filter_by(phone=normalized).first().id == recipient_e164.id


@pytest.mark.unit
def test_gift_points_by_phone_rejects_unnormalizable_phone(loyalty_service, sender_with_balance):
    """An un-normalizable phone raises ValidationError instead of filter_by(phone=None)."""
    with pytest.raises(ValidationError):
        loyalty_service.gift_points_by_phone(
            sender_id=sender_with_balance.id,
            recipient_phone="not-a-phone",
            points_amount=100,
        )
