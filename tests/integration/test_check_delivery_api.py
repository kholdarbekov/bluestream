"""Public delivery-coverage check endpoint (powers the checker + agents)."""
from unittest.mock import patch

import pytest

IN_ZONE = (41.31, 69.28)          # central Tashkent
OUT_OF_ZONE = (39.6270, 66.9750)  # Samarkand — outside coverage


@pytest.mark.integration
@pytest.mark.api
class TestCheckDeliveryApi:
    def test_latlng_in_zone_is_deliverable(self, client):
        r = client.get(f"/api/public/check-delivery?lat={IN_ZONE[0]}&lng={IN_ZONE[1]}")
        assert r.status_code == 200
        body = r.get_json()
        assert body["is_deliverable"] is True
        assert body["coverage"]["coverageUrl"] == "/coverage"
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert "public" in r.headers.get("Cache-Control", "")

    def test_latlng_out_of_zone_not_deliverable(self, client):
        r = client.get(f"/api/public/check-delivery?lat={OUT_OF_ZONE[0]}&lng={OUT_OF_ZONE[1]}")
        assert r.status_code == 200
        assert r.get_json()["is_deliverable"] is False

    def test_address_is_geocoded_then_checked(self, client):
        fake = {"latitude": IN_ZONE[0], "longitude": IN_ZONE[1], "formatted_address": "Amir Temur 1, Tashkent"}
        with patch("business_app.services.maps_service.MapsService.geocode_address", return_value=fake):
            r = client.get("/api/public/check-delivery?address=Amir+Temur+1")
        assert r.status_code == 200
        body = r.get_json()
        assert body["is_deliverable"] is True
        assert body["formatted_address"] == "Amir Temur 1, Tashkent"

    def test_ungeocodable_address_is_graceful_200(self, client):
        with patch("business_app.services.maps_service.MapsService.geocode_address", return_value=None):
            r = client.get("/api/public/check-delivery?address=nowhere-zzz")
        assert r.status_code == 200
        body = r.get_json()
        assert body["is_deliverable"] is None
        assert body["reason"] == "not_geocoded"

    def test_geocoder_failure_is_graceful_200(self, client):
        with patch("business_app.services.maps_service.MapsService.geocode_address", side_effect=Exception("boom")):
            r = client.get("/api/public/check-delivery?address=anything")
        assert r.status_code == 200
        assert r.get_json()["is_deliverable"] is None

    def test_missing_input_is_reachable_200_for_agents(self, client):
        # A bare GET to the *advertised* tool must be reachable (200) and
        # self-describing — agents discover it via the api-catalog and a 400
        # would fail the advertised-path reachability guard.
        r = client.get("/api/public/check-delivery")
        assert r.status_code == 200
        body = r.get_json()
        assert body["is_deliverable"] is None
        assert body["reason"] == "missing_input"
        assert body["coverage"]["coverageUrl"] == "/coverage"

    def test_invalid_coordinates_is_400(self, client):
        r = client.get("/api/public/check-delivery?lat=abc&lng=xyz")
        assert r.status_code == 400
        body = r.get_json()
        assert body["is_deliverable"] is None
        assert body["reason"] == "invalid_coordinates"

    def test_is_public_no_jwt_required(self, app):
        isolated = app.test_client(use_cookies=False)
        r = isolated.get(f"/api/public/check-delivery?lat={IN_ZONE[0]}&lng={IN_ZONE[1]}")
        assert r.status_code == 200

    def test_non_default_session_language_still_returns_json_200(self, app):
        # Regression: this JSON endpoint lives on frontend_bp, so it was being
        # caught by the HTML language redirect that 302s frontend.* GETs when the
        # resolved language is non-default (from a Session cookie / user pref /
        # Accept-Language). An agent or browser carrying a language cookie must
        # still get JSON 200 here — never a 302 to ?lang=xx. (This was also the
        # cause of a session-scoped-client ordering flake in the suite.)
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["language"] = "en"  # non-default (default is uz)
        r = c.get(f"/api/public/check-delivery?lat={IN_ZONE[0]}&lng={IN_ZONE[1]}")
        assert r.status_code == 200
        assert r.get_json()["is_deliverable"] is True


@pytest.mark.integration
@pytest.mark.api
def test_products_feed_service_area_is_precise(client, db):
    r = client.get("/api/public/products.json")
    assert r.status_code == 200
    sa = r.get_json()["serviceArea"]
    assert sa["country"] == "UZ"
    assert sa["city"] == "Tashkent"
    assert sa["coverageUrl"] == "/coverage"
    assert "Yunusabad" in sa["districts"]
    assert len(sa["districts"]) == 12
