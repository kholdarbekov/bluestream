"""POST /addresses/geocode and /addresses/reverse-geocode return the short address.

Only the outbound Nominatim HTTP call is stubbed; the real MapsService and formatter run.
"""

from unittest.mock import MagicMock

import pytest

from business_app.services import maps_service as maps_module

pytestmark = [pytest.mark.integration, pytest.mark.api]

RAW = "54, Ahmad Donish ko'chasi, Yunusobod 11 mavzesi, Yunusobod, Yunusobod Tumani, Toshkent, 100000, Oʻzbekiston"
SHORT = "54, Ahmad Donish ko'chasi, Yunusobod 11 mavzesi, Yunusobod, Toshkent"

# Captured live Nominatim /reverse body for the owner's point (41.3714355, 69.2922179).
NOMINATIM_REVERSE = {
    "place_id": 1,
    "osm_id": 2,
    "display_name": RAW,
    "address": {
        "house_number": "54",
        "road": "Ahmad Donish ko'chasi",
        "neighbourhood": "Yunusobod 11 mavzesi",
        "suburb": "Yunusobod",
        "district": "Yunusobod Tumani",
        "city": "Toshkent",
        "postcode": "100000",
        "country": "Oʻzbekiston",
    },
}
NOMINATIM_SEARCH = [{"lat": "41.3650", "lon": "69.2850", "display_name": RAW, "osm_id": 2, "place_id": 1}]


@pytest.fixture
def osm_http(app, monkeypatch):
    monkeypatch.setitem(app.config, "MAPS_PROVIDER", "osm")
    calls = []

    def install(payload):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        monkeypatch.setattr(maps_module.requests, "get", lambda *a, **k: calls.append((a, k)) or response)

    install.calls = calls
    return install


def test_reverse_geocode_returns_short_address_and_same_district(client, auth_headers, osm_http):
    osm_http(NOMINATIM_REVERSE)

    response = client.post(
        "/api/v1/addresses/reverse-geocode",
        json={"latitude": 41.3714355, "longitude": 69.2922179},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["formatted_address"] == SHORT
    # District extraction still reads the raw component, untouched by the formatter.
    assert data["district"] == "Yunusobod Tumani"
    args, kwargs = osm_http.calls[0]
    assert args[0].endswith("/reverse")
    assert kwargs["params"]["lat"] == 41.3714355
    assert kwargs["params"]["lon"] == 69.2922179


def test_geocode_returns_short_address(client, auth_headers, osm_http):
    osm_http(NOMINATIM_SEARCH)

    response = client.post(
        "/api/v1/addresses/geocode",
        json={"address": "54 Ahmad Donish"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "latitude": 41.365,
        "longitude": 69.285,
        "formatted_address": SHORT,
    }
    args, kwargs = osm_http.calls[0]
    assert args[0].endswith("/search")
    assert "54 Ahmad Donish" in kwargs["params"]["q"]
