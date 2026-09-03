"""GET /api/v1/loyalty/tiers must publish the tier discount's payment condition.

This route is genuinely unauthenticated (no jwt_required, no
require_loyalty_eligible) and Redis-cached for 3600 s. /my-loyalty renders
`discount_percentage` straight from it, so the percentage and the condition have
to leave the server together — the browser must not hold a second copy of the
sentence that can drift from the backend rule.

The cache key includes the language (utils/decorators.py cache_response), so
the sentence is safe to translate per request.
"""

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
from business_app.models.translation import Translation


@pytest.fixture
def program(db):
    row = LoyaltyProgram(
        name="Aqua Club", is_active=True, is_default=True, uzs_per_point=250,
        signup_bonus=200, referral_bonus=500, birthday_bonus=200,
        points_expiry_days=365, min_redemption_points=200,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def tiers(db, program):
    rows = [
        LoyaltyTierConfig(
            program_id=program.id, name="Base", display_order=0, min_points=0, max_points=999,
            points_multiplier=1.0, discount_percentage=0, is_active=True,
        ),
        LoyaltyTierConfig(
            program_id=program.id, name="Summit", display_order=1, min_points=1000, max_points=None,
            points_multiplier=2.0, discount_percentage=20, is_active=True,
        ),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


@pytest.fixture
def condition_rows(db):
    values = {
        "en": "Tier discounts apply to cash-on-delivery orders only.",
        "uz": "Daraja chegirmalari faqat yetkazib berishda naqd to'lovga qo'llaniladi.",
        "ru": "Скидки уровня применяются только к заказам с оплатой наличными при доставке.",
    }
    for lang, value in values.items():
        db.session.add(
            Translation(
                key="api.loyalty.tier_discount_condition", language=lang, value=value,
                category="api", is_active=True,
            )
        )
    db.session.commit()
    return values


@pytest.mark.integration
@pytest.mark.api
def test_tiers_endpoint_publishes_the_condition_next_to_the_rate(
    client, program, tiers, condition_rows
):
    resp = client.application.test_client().get("/api/v1/loyalty/tiers?lang=en")

    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["tier_count"] == 2
    assert data["tiers"][1]["discount_percentage"] == 20
    assert data["tier_discount_condition"] == condition_rows["en"]


@pytest.mark.integration
@pytest.mark.api
def test_condition_is_served_in_the_requested_language(client, program, tiers, condition_rows):
    """cache_response keys on the language; a shared key would serve a Russian
    customer the English sentence for up to an hour."""
    ru = client.application.test_client().get("/api/v1/loyalty/tiers?lang=ru")

    assert ru.status_code == 200
    assert ru.get_json()["data"]["tier_discount_condition"] == condition_rows["ru"]
