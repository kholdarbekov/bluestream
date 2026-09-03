"""Admin loyalty-tier writes must bust the cached public GET /api/v1/loyalty/tiers
response — the page the browser, the bot, and this batch's /loyalty-guide
worked example all read live tier rates from.

`cache_response` (business_app/utils/decorators.py) keys the response cache on
`response:{language}:{request.path}:{query_string}:{hash(...)}`. The admin
tier write endpoints (create/update/delete) call
`invalidate_cache("loyalty:tiers")` after committing. A plain string with no
`*` takes `invalidate_cache`'s exact-key branch, which deletes a literal
`loyalty:tiers` key that nothing ever writes — a no-op dressed as a fix.

These tests prove the SECOND read reflects the write, not merely that
`invalidate_cache()` was called — a call-occurrence assertion would stay green
against exactly this no-op.
"""

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig


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
def summit(db, program):
    tier = LoyaltyTierConfig(
        program_id=program.id, name="Summit", display_order=0, min_points=0,
        max_points=None, points_multiplier=1.0, discount_percentage=20, is_active=True,
    )
    db.session.add(tier)
    db.session.commit()
    return tier


@pytest.mark.integration
@pytest.mark.api
def test_admin_tier_rate_update_busts_the_cached_public_rate(
    client, db, admin_auth_headers, program, summit
):
    """The owner sets production tier percentages through the admin UI right
    after deploy. Without a real cache bust, GET /tiers keeps serving the OLD
    rate for up to an hour — advertising a wrong rate on every public surface
    that reads it."""
    public_client = client.application.test_client()

    first = public_client.get("/api/v1/loyalty/tiers?lang=en")
    assert first.status_code == 200
    cached_tier = next(t for t in first.get_json()["data"]["tiers"] if t["name"] == "Summit")
    assert cached_tier["discount_percentage"] == 20

    update = client.put(
        f"/api/v1/admin/loyalty/tiers/{summit.id}",
        headers=admin_auth_headers,
        json={"discount_percentage": 7},
    )
    assert update.status_code == 200, update.get_data(as_text=True)

    second = public_client.get("/api/v1/loyalty/tiers?lang=en")
    assert second.status_code == 200
    updated_tier = next(t for t in second.get_json()["data"]["tiers"] if t["name"] == "Summit")
    assert updated_tier["discount_percentage"] == 7, (
        "GET /api/v1/loyalty/tiers still served the pre-update rate: the admin "
        "write's invalidate_cache() call never reached the cached "
        "response:*:/api/v1/loyalty/tiers* key."
    )


@pytest.mark.integration
@pytest.mark.api
def test_admin_tier_create_busts_the_cached_public_list(client, db, admin_auth_headers, program):
    """Same guarantee for tier CREATE: a brand-new tier must show up on the
    very next public read, not only after the cache TTL expires."""
    public_client = client.application.test_client()

    first = public_client.get("/api/v1/loyalty/tiers?lang=en")
    assert first.status_code == 200
    assert first.get_json()["data"]["tier_count"] == 0

    create = client.post(
        "/api/v1/admin/loyalty/tiers",
        headers=admin_auth_headers,
        json={
            "program_id": program.id,
            "name": "Diamond",
            "min_points": 20000,
            "discount_percentage": 12,
        },
    )
    assert create.status_code == 201, create.get_data(as_text=True)

    second = public_client.get("/api/v1/loyalty/tiers?lang=en")
    assert second.status_code == 200
    assert second.get_json()["data"]["tier_count"] == 1, (
        "the new tier was created but the cached public list still shows the "
        "pre-create snapshot."
    )


@pytest.mark.integration
@pytest.mark.api
def test_admin_tier_delete_busts_the_cached_public_list(
    client, db, admin_auth_headers, program, summit
):
    """Same guarantee for tier DELETE (hard delete: no LoyaltyPoints rows
    reference this tier, so the endpoint removes it outright)."""
    public_client = client.application.test_client()

    first = public_client.get("/api/v1/loyalty/tiers?lang=en")
    assert first.get_json()["data"]["tier_count"] == 1

    delete = client.delete(f"/api/v1/admin/loyalty/tiers/{summit.id}", headers=admin_auth_headers)
    assert delete.status_code == 200, delete.get_data(as_text=True)

    second = public_client.get("/api/v1/loyalty/tiers?lang=en")
    assert second.get_json()["data"]["tier_count"] == 0, (
        "the tier was deleted but the cached public list still lists it."
    )
