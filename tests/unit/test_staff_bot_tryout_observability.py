"""Regression checks for staff bot diagnostics around tryout creation failures."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAFF_API_CLIENT = ROOT / "staff_bot" / "api_client.py"
STAFF_TRYOUT_HANDLER = ROOT / "staff_bot" / "handlers" / "tryouts.py"


def test_staff_api_client_logs_unsuccessful_backend_responses():
    """Non-2xx backend responses should be logged with endpoint and status details."""
    text = STAFF_API_CLIENT.read_text(encoding="utf-8")

    assert "def _log_unsuccessful_response" in text
    assert "Staff API request failed: method=%s endpoint=%s status=%s error_code=%s error=%s payload_type=%s" in text


def test_staff_api_client_follows_redirects_and_uses_canonical_products_path():
    """Staff bot product fetches should not fail on Flask trailing-slash redirects."""
    text = STAFF_API_CLIENT.read_text(encoding="utf-8")

    assert "follow_redirects=True" in text
    assert "'/api/v1/products/'" in text


def test_tryout_location_flow_logs_product_loading_failures():
    """Tryout creation should log product-fetch failures after geolocation succeeds."""
    text = STAFF_TRYOUT_HANDLER.read_text(encoding="utf-8")

    assert "Tryout product load failed after address capture" in text
    assert "getattr(response, 'status_code', None)" in text
    assert "getattr(response, 'error_code', None)" in text
