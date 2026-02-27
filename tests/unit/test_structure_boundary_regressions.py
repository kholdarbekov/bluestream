"""Static structure regression checks for API/service boundaries."""

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAFF_API_FILE = ROOT / "business_app" / "api" / "staff.py"
BOT_API_FILE = ROOT / "business_app" / "api" / "bot.py"
DECORATORS_FILE = ROOT / "business_app" / "utils" / "decorators.py"
API_DIR = ROOT / "business_app" / "api"
STAFF_BOT_SEARCH_USER_HANDLER = ROOT / "staff_bot" / "handlers" / "operator" / "search_user.py"
STAFF_BOT_CREATE_ORDER_HANDLER = ROOT / "staff_bot" / "handlers" / "operator" / "create_order.py"
STAFF_BOT_SEARCH_UTIL = ROOT / "staff_bot" / "utils" / "search.py"
API_BOUNDARY_SCORE_BUDGET = {
    "__init__.py": 0,
    "addresses.py": 18,
    "admin.py": 482,
    "analytics.py": 25,
    "auth.py": 36,
    "blog.py": 6,
    "bot.py": 0,
    "carts.py": 6,
    "delivery.py": 27,
    "loyalty.py": 0,
    "notifications.py": 0,
    "orders.py": 10,
    "payments.py": 19,
    "products.py": 14,
    "session_management.py": 6,
    "staff.py": 0,
    "subscriptions.py": 32,
    "translations.py": 4,
}
BOUNDARY_PATTERNS = (
    re.compile(r"\.query\b"),
    re.compile(r"db\.session"),
    re.compile(r"from business_app\.models"),
)


def test_staff_api_module_is_syntax_valid():
    """Guard against accidental syntax regressions in staff API module."""
    ast.parse(STAFF_API_FILE.read_text(encoding="utf-8"))


def test_staff_api_uses_shared_staff_decorator_and_address_helpers():
    """Staff API should import shared decorators/helpers instead of local definitions."""
    text = STAFF_API_FILE.read_text(encoding="utf-8")

    assert "from business_app.utils.decorators import require_staff_roles" in text
    assert "from business_app.utils.address_helpers import get_address_label, get_address_line" in text
    assert "def require_staff_roles(" not in text
    assert "def _address_line(" not in text
    assert "def _address_label(" not in text


def test_staff_api_operator_flows_delegate_to_staff_service():
    """Operator routes should not perform direct model/session access in API layer."""
    text = STAFF_API_FILE.read_text(encoding="utf-8")

    assert "StaffService.get_recent_operator_orders(" in text
    assert "StaffService.add_client_address(" in text
    assert "StaffService.get_client_addresses(" in text
    assert "from business_app.models.order import Order" not in text
    assert "from business_app.models.user import User, UserAddress" not in text
    assert "from business_app import db" not in text


def test_bot_api_uses_shared_webhook_signature_decorator():
    """Bot API should use shared webhook signature decorator from utils module."""
    text = BOT_API_FILE.read_text(encoding="utf-8")

    assert (
        "from business_app.utils.decorators import require_auth, require_admin, verify_webhook_signature"
        in text
    )
    assert "@verify_webhook_signature()" in text
    assert "def verify_webhook_signature(" not in text


def test_decorators_module_contains_shared_staff_and_webhook_guards():
    """Shared decorators module should contain centralized guards used by APIs."""
    text = DECORATORS_FILE.read_text(encoding="utf-8")

    assert "def require_staff_roles(" in text
    assert "def verify_webhook_signature(" in text


def test_staff_bot_search_type_detection_is_shared():
    """Search-type inference should live in shared staff bot utility module."""
    search_user_text = STAFF_BOT_SEARCH_USER_HANDLER.read_text(encoding="utf-8")
    create_order_text = STAFF_BOT_CREATE_ORDER_HANDLER.read_text(encoding="utf-8")
    shared_util_text = STAFF_BOT_SEARCH_UTIL.read_text(encoding="utf-8")

    assert "from utils.search import detect_search_type" in search_user_text
    assert "from utils.search import detect_search_type" in create_order_text
    assert "def _detect_search_type(" not in search_user_text
    assert "def _detect_search_type(" not in create_order_text
    assert "def detect_search_type(" in shared_util_text


def test_api_modules_do_not_define_local_decorators():
    """
    Reusable decorators must live in utility modules, not API route modules.
    """
    offenders = []
    for path in API_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "from functools import wraps" in text or "@wraps(" in text:
            offenders.append(path.name)

    assert not offenders, (
        "Local decorators detected in API modules. Move them to shared decorators utils: "
        f"{offenders}"
    )


def test_api_boundary_coupling_scores_do_not_regress():
    """
    Guardrail: prevent boundary coupling regressions in API modules.
    """
    unknown_files = []
    regressions = []

    for path in sorted(API_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        score = sum(len(pattern.findall(text)) for pattern in BOUNDARY_PATTERNS)

        if path.name not in API_BOUNDARY_SCORE_BUDGET:
            unknown_files.append((path.name, score))
            continue

        budget = API_BOUNDARY_SCORE_BUDGET[path.name]
        if score > budget:
            regressions.append((path.name, score, budget))

    assert not regressions, (
        "API boundary coupling increased over baseline budgets: "
        f"{regressions}"
    )
    assert not unknown_files, (
        "New API files require explicit boundary budget registration: "
        f"{unknown_files}"
    )
