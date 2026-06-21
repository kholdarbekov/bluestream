"""Regression: outbound map HTTP calls must carry a request timeout.

Prod bug: ``optimize_driver_route_task`` was SIGKILLed at its 120s hard limit
because ``_yandex_geocode`` (and the other map calls) used a bare
``requests.get(...)`` with no ``timeout=``. A single stalled connection hangs
the whole task indefinitely. Every outbound map request must pass a timeout.
"""

from unittest.mock import MagicMock

import pytest

from business_app.services import maps_service as maps_module
from business_app.services.maps_service import MapsService


_YANDEX_OK = {
    "response": {
        "GeoObjectCollection": {
            "featureMember": [
                {
                    "GeoObject": {
                        "Point": {"pos": "69.2401 41.3111"},
                        "metaDataProperty": {
                            "GeocoderMetaData": {"text": "Tashkent", "precision": "exact"}
                        },
                    }
                }
            ]
        }
    }
}


@pytest.mark.unit
class TestMapsServiceRequestTimeout:
    def test_yandex_geocode_passes_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "test-key"

            fake_response = MagicMock()
            fake_response.raise_for_status = MagicMock()
            fake_response.json = MagicMock(return_value=_YANDEX_OK)
            mock_get = MagicMock(return_value=fake_response)
            monkeypatch.setattr(maps_module.requests, "get", mock_get)

            svc.geocode_address("Some Street 1", "Tashkent")

        mock_get.assert_called_once()
        timeout = mock_get.call_args.kwargs.get("timeout")
        assert timeout is not None, "outbound geocode request must pass a timeout="
        assert timeout > 0
