"""Integration tests for loyalty tier NAME translations (admin CRUD + GET shape).

Mirrors the streak-rule translation tests: the admin tier modal sets per-language
names via ``translations.name.{en,ru,uz}``; the GET list must surface them (as
``{"name": {lang: value}}``) so the edit form can pre-fill, the same convenient
single-modal flow the streak rules already use.
"""
import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig


def _default_program(db) -> LoyaltyProgram:
    program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
    if not program:
        program = LoyaltyProgram(name="Default Program", is_active=True, is_default=True)
        db.session.add(program)
        db.session.commit()
    return program


@pytest.mark.integration
@pytest.mark.api
class TestLoyaltyTierTranslations:
    def test_create_tier_with_translations_reflected_in_get(self, client, admin_auth_headers, db):
        program = _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/tiers",
            headers=admin_auth_headers,
            json={
                "program_id": program.id,
                "name": "Diamond",
                "min_points": 20000,
                "translations": {"name": {"en": "Diamond", "ru": "Алмаз", "uz": "Olmos"}},
            },
        )
        assert resp.status_code == 201, resp.get_data(as_text=True)

        list_resp = client.get(
            f"/api/v1/admin/loyalty/tiers?program_id={program.id}", headers=admin_auth_headers
        )
        assert list_resp.status_code == 200
        tiers = list_resp.get_json()["data"]["tiers"]
        target = next(t for t in tiers if t["name"] == "Diamond")
        assert "translations" in target
        assert isinstance(target["translations"], dict)
        assert target["translations"]["name"].get("ru") == "Алмаз"
        assert target["translations"]["name"].get("uz") == "Olmos"

    def test_update_tier_translations_reflected_in_get(self, client, admin_auth_headers, db):
        program = _default_program(db)
        tier = LoyaltyTierConfig(
            program_id=program.id, name="Starter", display_order=0,
            min_points=0, points_multiplier=1.0, discount_percentage=0, is_active=True,
        )
        db.session.add(tier)
        db.session.commit()

        resp = client.put(
            f"/api/v1/admin/loyalty/tiers/{tier.id}",
            headers=admin_auth_headers,
            json={"translations": {"name": {"ru": "Старт", "uz": "Boshlovchi"}}},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        list_resp = client.get(
            f"/api/v1/admin/loyalty/tiers?program_id={program.id}", headers=admin_auth_headers
        )
        target = next(t for t in list_resp.get_json()["data"]["tiers"] if t["id"] == tier.id)
        assert target["translations"]["name"].get("ru") == "Старт"
