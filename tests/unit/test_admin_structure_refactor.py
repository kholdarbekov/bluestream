"""Static regression checks for admin API/service structure boundaries."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADMIN_API_FILE = ROOT / "business_app" / "api" / "admin.py"
REPORT_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_report_service.py"
BULK_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_bulk_action_service.py"


def test_admin_api_delegates_report_generation_to_service_layer():
    text = ADMIN_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.services.admin_report_service import AdminReportService" in text
    assert "AdminReportService.generate(" in text
    assert "AdminReportService.format_report(" in text

    assert "def _generate_sales_summary_report(" not in text
    assert "def _generate_customer_report(" not in text
    assert "def _generate_product_performance_report(" not in text
    assert "def _generate_delivery_report(" not in text
    assert "def _generate_financial_summary_report(" not in text
    assert "def _generate_user_activity_report(" not in text
    assert "def _generate_inventory_report(" not in text
    assert "def _generate_subscription_report(" not in text
    assert "def _generate_loyalty_report(" not in text
    assert "def _format_report_as_csv(" not in text
    assert "def _format_report_as_excel(" not in text


def test_admin_api_delegates_bulk_actions_to_service_layer():
    text = ADMIN_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.services.admin_bulk_action_service import AdminBulkActionService" in text
    assert "AdminBulkActionService.get_valid_actions(" in text
    assert "AdminBulkActionService.perform(" in text

    assert "def _bulk_action_users(" not in text
    assert "def _bulk_action_orders(" not in text
    assert "def _bulk_action_products(" not in text
    assert "def _bulk_action_reviews(" not in text
    assert "def _bulk_action_subscriptions(" not in text
    assert "def _bulk_action_deliveries(" not in text


def test_admin_service_modules_exist_with_expected_entrypoints():
    report_text = REPORT_SERVICE_FILE.read_text(encoding="utf-8")
    bulk_text = BULK_SERVICE_FILE.read_text(encoding="utf-8")

    assert "class AdminReportService:" in report_text
    assert "def generate(" in report_text
    assert "def format_report(" in report_text

    assert "class AdminBulkActionService:" in bulk_text
    assert "def get_valid_actions(" in bulk_text
    assert "def perform(" in bulk_text
