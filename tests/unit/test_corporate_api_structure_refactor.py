"""Structure regression tests for corporate API/service boundaries."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADMIN_API_FILE = ROOT / "business_app" / "api" / "admin.py"
STAFF_API_FILE = ROOT / "business_app" / "api" / "staff.py"


def test_admin_corporate_routes_delegate_to_service_factory():
    text = ADMIN_API_FILE.read_text(encoding="utf-8")

    assert "def list_corporate_contracts(" in text
    assert "def create_corporate_contract(" in text
    assert "def topup_corporate_contract(" in text
    assert "def preview_corporate_contract_overlaps(" in text
    assert "service = get_corporate_contract_service()" in text
    assert "CorporateContract.query" not in text
    assert "CorporatePrepaymentLedger.query" not in text


def test_staff_corporate_balance_route_stays_thin_and_service_driven():
    text = STAFF_API_FILE.read_text(encoding="utf-8")

    assert "def get_client_corporate_balance(" in text
    assert "service = get_corporate_contract_service()" in text
    assert "from business_app.models.corporate" not in text
    assert "db.session" not in text
