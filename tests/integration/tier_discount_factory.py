"""Fixtures for the COD tier-discount tests.

Every tier percentage used by a test is seeded here, by the test. Production's
thresholds and rates differ from dev's and an older migration seeded a third
set, so no assertion in this feature's suite may reference a DB-resident
number.
"""

from datetime import UTC, datetime
from decimal import Decimal

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.utils.constants import LoyaltyTransactionType


def seed_program(db):
    program = LoyaltyProgram(name="Tier discount program", is_active=True, is_default=True)
    db.session.add(program)
    db.session.commit()
    return program


def seed_tier(db, program, *, name, rate: Decimal, min_points: int = 0, display_order: int = 0):
    """One LoyaltyTierConfig row at a TEST-OWNED discount rate."""
    tier = LoyaltyTierConfig(
        program_id=program.id,
        name=name,
        display_order=display_order,
        min_points=min_points,
        discount_percentage=float(rate),
        is_active=True,
    )
    db.session.add(tier)
    db.session.commit()
    return tier


def seed_account(db, user, program, *, qualifying_points: int = 0, balance: int = 0):
    """A loyalty account, optionally backed by a real EARNED lot so
    calculate_qualifying_points (365-day rolling window) sees the points."""
    account = LoyaltyPoints(
        user_id=user.id,
        program_id=program.id,
        total_earned=max(qualifying_points, balance),
        current_balance=balance,
    )
    db.session.add(account)
    db.session.flush()
    if qualifying_points or balance:
        lot = LoyaltyTransaction(
            user_id=user.id,
            transaction_type=LoyaltyTransactionType.EARNED,
            points=max(qualifying_points, balance),
            remaining_points=balance,
            description="tier seed",
        )
        lot.expires_at = datetime(2999, 1, 1, tzinfo=UTC)
        db.session.add(lot)
    db.session.commit()
    return account


def verify_phone(db, user):
    """@require_verification("phone") reads the phone_verified PROPERTY, which
    derives from phone_verified_at — not from is_verified."""
    user.phone_verified_at = datetime.now(UTC)
    db.session.commit()


def post_order(app, headers, *, product_id, address_id, payment_method, quantity=2, **extra):
    """The body the customer clients actually send.

    CreateOrderRequest declares a TOP-LEVEL ``delivery_address_id`` and has no
    ``delivery_address`` field at all; a nested address object is silently
    dropped by Pydantic and the endpoint 400s on "address not found" before
    reaching the code under test. A FRESH test client per call — the
    session-scoped one leaks JWT cookies.
    """
    body = {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "delivery_address_id": address_id,
        "payment_method": payment_method,
    }
    body.update(extra)
    return app.test_client().post("/api/v1/orders/", json=body, headers=headers)
