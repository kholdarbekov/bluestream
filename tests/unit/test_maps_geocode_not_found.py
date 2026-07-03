"""Geocode 'address not found' is expected bad user input, not a server fault.

Before: all three providers raised ``ExternalServiceError`` for a no-results
geocode, the ``geocode_address`` wrapper re-wrapped it, and the route logged at
ERROR + returned 503 — so every unresolvable address a user typed produced an
ERROR log and a server-error status (the intended 404 branch was dead code).

After: a no-results geocode raises ``NotFoundError`` (mapped to 404); the route
logs it at INFO and returns 404. Genuine provider faults still raise
``ExternalServiceError`` → ERROR + 503.
"""

import logging
from unittest.mock import MagicMock

import pytest

from business_app.services import maps_service as maps_module
from business_app.services.maps_service import MapsService
from business_app.utils.exceptions import ExternalServiceError, NotFoundError


def _fake_response(json_payload, status_code=200):
    resp = MagicMock(name="fake_response")
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    resp.status_code = status_code
    resp.text = ""
    return resp


@pytest.mark.unit
class TestGeocodeNotFoundClassification:
    def test_google_no_results_raises_notfound(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            monkeypatch.setattr(
                maps_module.requests,
                "get",
                MagicMock(return_value=_fake_response({"status": "ZERO_RESULTS", "results": []})),
            )

            with pytest.raises(NotFoundError):
                svc.geocode_address("nowhere at all", city="Tashkent")

    def test_provider_http_fault_still_raises_externalservice(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            resp = _fake_response({"status": "OK", "results": []})
            resp.raise_for_status = MagicMock(side_effect=RuntimeError("boom 500"))
            monkeypatch.setattr(maps_module.requests, "get", MagicMock(return_value=resp))

            with pytest.raises(ExternalServiceError):
                svc.geocode_address("somewhere", city="Tashkent")


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def app_logs(app):
    handler = _ListHandler()
    prev = app.logger.level
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        app.logger.removeHandler(handler)
        app.logger.setLevel(prev)


def _rec(records, needle):
    return next((r for r in records if needle in r.getMessage()), None)


@pytest.mark.unit
class TestGeocodeRouteStatusAndLevel:
    def test_not_found_returns_404_and_logs_info(self, app, client, auth_headers, app_logs, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.geocode_address",
            MagicMock(side_effect=NotFoundError("Address not found")),
        )

        resp = client.post("/api/v1/addresses/geocode", json={"address": "nowhere"}, headers=auth_headers)

        assert resp.status_code == 404
        rec = _rec(app_logs, "Geocode")
        assert rec is not None, "expected a geocode log line"
        assert rec.levelno == logging.INFO

    def test_provider_error_returns_503_and_logs_error(self, app, client, auth_headers, app_logs, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.maps_service.MapsService.geocode_address",
            MagicMock(side_effect=ExternalServiceError("Geocoding failed: upstream 500")),
        )

        resp = client.post("/api/v1/addresses/geocode", json={"address": "somewhere"}, headers=auth_headers)

        assert resp.status_code == 503
        rec = _rec(app_logs, "Geocoding failed")
        assert rec is not None, "expected a geocode-failure log line"
        assert rec.levelno == logging.ERROR
