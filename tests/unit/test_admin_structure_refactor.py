"""Static regression checks for admin API/service structure boundaries."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADMIN_API_FILE = ROOT / "business_app" / "api" / "admin.py"
REPORT_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_report_service.py"
BULK_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_bulk_action_service.py"
DELIVERY_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_delivery_service.py"
LOYALTY_SERVICE_FILE = ROOT / "business_app" / "services" / "admin_loyalty_service.py"


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


def test_admin_api_delegates_delivery_management_to_service_layer():
    text = ADMIN_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.services.admin_delivery_service import AdminDeliveryService" in text
    assert "AdminDeliveryService.list_deliveries(" in text
    assert "AdminDeliveryService.update_delivery(" in text
    assert "AdminDeliveryService.reassign_delivery(" in text
    assert "def _serialize_admin_delivery(" not in text
    assert "def _get_admin_delivery_summary(" not in text


def test_admin_service_modules_exist_with_expected_entrypoints():
    report_text = REPORT_SERVICE_FILE.read_text(encoding="utf-8")
    bulk_text = BULK_SERVICE_FILE.read_text(encoding="utf-8")
    delivery_text = DELIVERY_SERVICE_FILE.read_text(encoding="utf-8")
    loyalty_text = LOYALTY_SERVICE_FILE.read_text(encoding="utf-8")

    assert "class AdminReportService:" in report_text
    assert "def generate(" in report_text
    assert "def format_report(" in report_text

    assert "class AdminBulkActionService:" in bulk_text
    assert "def get_valid_actions(" in bulk_text
    assert "def perform(" in bulk_text

    assert "class AdminDeliveryService:" in delivery_text
    assert "def list_deliveries(" in delivery_text
    assert "def update_delivery(" in delivery_text
    assert "def reassign_delivery(" in delivery_text
    assert "def serialize_delivery(" in delivery_text

    assert "class AdminLoyaltyService:" in loyalty_text
    assert "def list_members(" in loyalty_text
    assert "def get_member_detail(" in loyalty_text
    assert "def list_programs(" in loyalty_text
    assert "def list_rewards(" in loyalty_text
    assert "def get_analytics(" in loyalty_text


def test_admin_api_delegates_loyalty_management_to_service_layer():
    text = ADMIN_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.services.admin_loyalty_service import AdminLoyaltyService" in text
    assert "AdminLoyaltyService.list_members(" in text
    assert "AdminLoyaltyService.get_member_detail(" in text
    assert "AdminLoyaltyService.list_programs(" in text
    assert "AdminLoyaltyService.list_rewards(" in text
    assert "AdminLoyaltyService.get_analytics(" in text
