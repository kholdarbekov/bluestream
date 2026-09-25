"""Every backend error code is either translated for staff, handled by name, or
declared unreachable from the staff bot — never silently left to fall back."""
import re
from pathlib import Path

from staff_bot.handlers.base import BaseHandler

ROOT = Path(__file__).resolve().parents[2]
_CODE = re.compile(r"""error_code\s*=\s*["']([A-Z][A-Z0-9_]+)["']|["']error_code["']\s*:\s*["']([A-Z][A-Z0-9_]+)["']""")

# Branched on by name in staff_bot/ before the resolver sees them.
BESPOKE = {
    "LOCATION_REQUIRED": "active_delivery.py share-location prompt",
    "LOCATION_TOO_COARSE": "location.py coarse-location copy",
    "SALES_VISIT_ALREADY_OPEN": "visit.py resume offer (also mapped)",
    "BOTTLE_SESSION_ALREADY_OPEN": "bottle_collection.py /open short copy + session menu (also mapped, for join)",
    "BOTTLE_SESSION_REQUIRED": "orders_pool.py accept: start/join-session buttons (also mapped)",
    "STAFF_INVALID_INVITE_TOKEN": "start.py login refusal copy (also mapped)",
}

# The two shapes a reason may take (Task 12 ruling). A code no staff-bot route
# raises says where it IS raised. A code a staff route can raise, but only for an
# input the bot never sends or in a race, says why — and that such a refusal
# shows the backend's own sentence (the backend-reason fallback), not silence.
_NOT_ON_ROUTE = "not raised on any staff-bot route"
_BOT_NEVER_SENDS = "staff route, but the bot never sends this input"
_RACE_ONLY = "staff route, but race-only"
_FALLBACK = " — backend-reason fallback applies"

# Raised only on paths no staff-bot route reaches. One reason per line.
NOT_STAFF_FACING: dict[str, str] = {
    # Customer / admin auth (middleware/auth_middleware.py is used only by /auth/*; staff routes use
    # require_staff_roles, whose codes are mapped).
    "ACCESS_DENIED": f"{_NOT_ON_ROUTE} (auth_middleware.py:269 verify_user_ownership, used by no route)",
    "ACCOUNT_INACTIVE": f"{_NOT_ON_ROUTE} (auth_middleware.py:78 require_role: /auth/admin/*)",
    "ACCOUNT_MATCH_FOUND": f"{_NOT_ON_ROUTE} (api/auth.py:1921 /auth/telegram-register, customer bot)",
    "AUTH_FAILED": f"{_NOT_ON_ROUTE} (auth_middleware.py:100/153/277: /auth/admin/* and unused decorators)",
    "AUTH_REQUIRED": f"{_NOT_ON_ROUTE} (auth_middleware.py:32/65/128/221/303/495: /auth/platform-status, "
    "/auth/suggest-auto-link, /auth/admin/*)",
    "EMAIL_VERIFICATION_REQUIRED": f"{_NOT_ON_ROUTE} (auth_middleware.py:511 require_verified_user, used by no route)",
    "INSUFFICIENT_PERMISSIONS": f"{_NOT_ON_ROUTE} (auth_middleware.py:90 require_role: /auth/admin/*)",
    "INVALID_OTP": f"{_NOT_ON_ROUTE} (auth_service.py:748: /auth/phone/register/verify)",
    "INVALID_REQUEST": f"{_NOT_ON_ROUTE} (auth_middleware.py:238 verify_user_ownership, used by no route)",
    "INVALID_SESSION": f"{_NOT_ON_ROUTE} (auth_middleware.py:450 unused; token_service.py:438: /auth/validate-token)",
    "OTP_EXPIRED": f"{_NOT_ON_ROUTE} (auth_service.py:722: /auth/phone/register/verify)",
    "OTP_MAX_ATTEMPTS": f"{_NOT_ON_ROUTE} (auth_service.py:613/711/741/868: /auth/phone/register/*, "
    "/auth/phone/resend-otp)",
    "PERMISSION_DENIED": f"{_NOT_ON_ROUTE} (auth_middleware.py:143 require_permission, used by no route)",
    "PHONE_ALREADY_REGISTERED": f"{_NOT_ON_ROUTE} (auth_service.py:590/758/848: customer /auth/phone/*)",
    "RATE_LIMIT_ERROR": f"{_NOT_ON_ROUTE} (auth_middleware.py:315 rate_limit_by_user, used by no route)",
    "RESEND_COOLDOWN": f"{_NOT_ON_ROUTE} (auth_service.py:603/858: /auth/phone/register/init, /auth/phone/resend-otp)",
    "SESSION_VALIDATION_FAILED": f"{_NOT_ON_ROUTE} (auth_middleware.py:469, a decorator no route uses)",
    "TOKEN_BLACKLISTED": f"{_NOT_ON_ROUTE} (token_service.py:426 validate_token_integrity: /auth/validate-token)",
    "TOKEN_EXPIRED": f"{_NOT_ON_ROUTE} (token_service.py:443: /auth/validate-token)",
    "TOKEN_INVALID": f"{_NOT_ON_ROUTE} (auth_middleware.py:358 unused; token_service.py:445: /auth/validate-token)",
    "TOKEN_REVOKED": f"{_NOT_ON_ROUTE} (auth_middleware.py:348 check_token_blacklist, used by no route)",
    "USER_INACTIVE": f"{_NOT_ON_ROUTE} (token_service.py:434: /auth/validate-token)",
    "USER_NOT_FOUND": f"{_NOT_ON_ROUTE} (auth_middleware.py:72/257/502, token_service.py:431: /auth/admin/*, "
    "/auth/validate-token)",
    "USER_NOT_REGISTERED": f"{_NOT_ON_ROUTE} (api/auth.py:1801 /auth/telegram-login, customer bot)",
    "VALIDATION_FAILED": f"{_NOT_ON_ROUTE} (token_service.py:448: /auth/validate-token)",
    "VERIFICATION_CHECK_FAILED": f"{_NOT_ON_ROUTE} (auth_middleware.py:527 require_verified_user, used by no route)",
    "ERROR_CODE": f"{_NOT_ON_ROUTE} (utils/swagger_config.py:57, a placeholder in the API docs, never raised)",
    # Customer ordering and payments.
    "ASL_BELGISI_UNAVAILABLE": f"{_NOT_ON_ROUTE} (api/orders.py:442, customer POST /orders card path)",
    "MARKING_CODES_POOL_SHORT": f"{_NOT_ON_ROUTE} (api/payments.py:326 /payments/create; admin payment-method edit)",
    "PAYMENT_NOT_CANCELLABLE": f"{_NOT_ON_ROUTE} (api/payments.py:694 /payments/<id>/cancel)",
    "PAYMENT_NOT_REFUNDABLE": f"{_NOT_ON_ROUTE} (api/payments.py:1196 /payments/refund)",
    "REPEAT_LEGACY_ORDER_UNSUPPORTED": f"{_NOT_ON_ROUTE} (order_service.py:812: customer /orders/repeat/<id>)",
    "SALES_CONFIRMATION_ACTION_INVALID": f"{_NOT_ON_ROUTE} (agent_order_confirmation_service.py:215: customer "
    "/orders/<id>/agent-confirmation)",
    "SALES_CONFIRMATION_NOT_PENDING": f"{_NOT_ON_ROUTE} (agent_order_confirmation_service.py:210: customer "
    "/orders/<id>/agent-confirmation)",
    # Admin order edits, cash corrections, reports.
    "BOTTLE_CORRECTION_SCOPE_NOT_LIVE": f"{_NOT_ON_ROUTE} (order_edit_service.py:962: admin /orders/<id>/edit)",
    "BOTTLE_SESSION_ACTIVE_CONFLICT": f"{_NOT_ON_ROUTE} (bottle_tracking_service.py:3404: admin order edit)",
    "BOTTLE_SESSION_NOT_REOPENABLE": f"{_NOT_ON_ROUTE} (bottle_tracking_service.py:3387: admin order edit)",
    "ORDER_EDIT_BLOCKED": f"{_NOT_ON_ROUTE} (order_edit_service.py:231: admin /orders/<id>/edit)",
    "CASH_SESSION_ACTIVE_CONFLICT": f"{_NOT_ON_ROUTE} (driver_reconciliation_service.py:823: admin cash edit)",
    "CASH_SESSION_NOT_FORCE_CLOSEABLE": f"{_NOT_ON_ROUTE} (driver_reconciliation_service.py:729: admin force-close)",
    "INVALID_REPORT_TYPE": f"{_NOT_ON_ROUTE} (admin_report_service.py:75: admin /reports/generate)",
    "UNSUPPORTED_REPORT_TYPE": f"{_NOT_ON_ROUTE} (admin_report_service.py:103: admin /reports/generate)",
    "GROCERY_FLAG_BLOCKED_WHILE_LINKED": f"{_NOT_ON_ROUTE} (auth_service.py:1388: admin user and try-out edits)",
    # Admin bottle tools and the place-group / customer-link lifecycle (customer_link_service.py).
    "BOTTLE_DECOUPLED_KEY_REQUIRED": f"{_NOT_ON_ROUTE} (bottle_tracking_service.py:1218: admin place merge only)",
    "BOTTLE_INITIAL_BALANCE_EXISTS": f"{_NOT_ON_ROUTE} (bottle_tracking_service.py:1498: admin_bottles.py:227)",
    "BOTTLE_SCOPE_UNREACHABLE": f"{_NOT_ON_ROUTE} (bottle_tracking_service.py:599: frozen scopes of admin fine "
    "waive/pay and order-edit corrections; staff writes resolve the live scope)",
    "CUSTOMER_LINK_ADDRESS_NOT_FOUND": f"{_NOT_ON_ROUTE} (customer_link_service.py:865/1843/1886/2564: admin)",
    "CUSTOMER_LINK_DISTINCT_CONFLICT": f"{_NOT_ON_ROUTE} (customer_link_service.py:244: admin /users/<id>/link)",
    "CUSTOMER_LINK_GROCERY_ACCOUNT": f"{_NOT_ON_ROUTE} (customer_link_service.py:220: admin /users/<id>/link)",
    "CUSTOMER_LINK_NOT_INDIVIDUAL": f"{_NOT_ON_ROUTE} (customer_link_service.py:229: admin /users/<id>/link)",
    "CUSTOMER_LINK_SELF": f"{_NOT_ON_ROUTE} (customer_link_service.py:211: admin /users/<id>/link)",
    "CUSTOMER_LINK_USER_NOT_FOUND": f"{_NOT_ON_ROUTE} (customer_link_service.py:209/334: admin link/unlink)",
    "MERGE_EXCLUSION_NOT_ELIGIBLE": f"{_NOT_ON_ROUTE} (bottle_tracking_service.py:1982, customer_link_service.py:1270: "
    "admin place-group merge)",
    "MERGE_PREVIEW_STALE": f"{_NOT_ON_ROUTE} (customer_link_service.py:1227: admin place-group merge)",
    "MERGE_REASON_REQUIRED": f"{_NOT_ON_ROUTE} (customer_link_service.py:1185: admin place-group merge)",
    "PLACE_GROUP_ADDRESS_ALREADY_GROUPED": f"{_NOT_ON_ROUTE} (customer_link_service.py:827: admin place groups)",
    "PLACE_GROUP_ADDRESS_NOT_DELETABLE": f"{_NOT_ON_ROUTE} (customer_link_service.py:2081: customer and admin "
    "address DELETE)",
    "PLACE_GROUP_DISSOLVED": f"{_NOT_ON_ROUTE} (customer_link_service.py:1689: admin place groups)",
    "PLACE_GROUP_ENTITY_MEMBER": f"{_NOT_ON_ROUTE} (customer_link_service.py:821: admin place groups)",
    "PLACE_GROUP_GROCERY_MEMBER": f"{_NOT_ON_ROUTE} (customer_link_service.py:814: admin place groups)",
    "PLACE_GROUP_MIN_ADDRESSES": f"{_NOT_ON_ROUTE} (customer_link_service.py:1588: admin place groups)",
    "PLACE_GROUP_NOT_FOUND": f"{_NOT_ON_ROUTE} (customer_link_service.py:1683/1846/1865/1887: admin place groups)",
    "PLACE_GROUP_REASON_REQUIRED": f"{_NOT_ON_ROUTE} (customer_link_service.py:1837: admin place groups)",
    "PLACE_SPLIT_INVALID": f"{_NOT_ON_ROUTE} (customer_link_service.py:1756-1771: admin place-group address removal)",
    # Admin dispatch map.
    "DISPATCH_DUPLICATE_STOP": f"{_NOT_ON_ROUTE} (route_edit_service.py:75: admin dispatch)",
    "DISPATCH_ROUTE_NOT_FOUND": f"{_NOT_ON_ROUTE} (route_edit_service.py:80: admin dispatch)",
    "DISPATCH_ROUTE_STALE": f"{_NOT_ON_ROUTE} (route_edit_service.py:36, admin_dispatch.py:222)",
    "DISPATCH_SAME_DRIVER": f"{_NOT_ON_ROUTE} (route_edit_service.py:210: admin dispatch)",
    "STAFF_DELIVERY_NOT_POOLABLE": f"{_NOT_ON_ROUTE} (staff_service.py:1227: admin dispatch Return to pool only)",
    "STAFF_ORDER_NOT_ACTIVE": f"{_NOT_ON_ROUTE} (staff_service.py:1240: admin dispatch Return to pool only)",
    # Admin sales back office.
    "SALES_AGENT_PROFILE_REQUIRED": f"{_NOT_ON_ROUTE} (staff_service.py:834, outlet_service.py:1359: admin_sales.py)",
    "SALES_CONTACT_NOT_FOUND": f"{_NOT_ON_ROUTE} (outlet_service.py:632: admin outlet-contact edit/delete)",
    "SALES_CONTACT_PRIMARY_REQUIRED": f"{_NOT_ON_ROUTE} (outlet_service.py:646: admin outlet-contact edit)",
    "SALES_EXCEPTION_TYPE_INVALID": f"{_NOT_ON_ROUTE} (exception_feed_service.py:455: admin /sales/exceptions)",
    "SALES_LOST_REASON_INVALID": f"{_NOT_ON_ROUTE} (outlet_service.py:1648: admin /sales/outlets/<id>/mark-lost)",
    "SALES_PAYMENT_TERMS_INVALID": f"{_NOT_ON_ROUTE} (outlet_service.py:1099: admin outlet PUT; the agent PUT drops "
    "payment_terms, staff_sales.py AGENT_EDITABLE_OUTLET_FIELDS)",
    "SALES_PHOTO_NOT_FOUND": f"{_NOT_ON_ROUTE} (visit_service.py:981: admin /sales/visit-photos/<id>/file)",
    "SALES_PHOTO_UNAVAILABLE": f"{_NOT_ON_ROUTE} (visit_service.py:992: admin /sales/visit-photos/<id>/file)",
    "SALES_VISIT_PHOTO_FILTER_INVALID": f"{_NOT_ON_ROUTE} (visit_service.py:860: admin /sales/visits)",
    "STAFF_INVALID_EMPLOYMENT_TYPE": f"{_NOT_ON_ROUTE} (staff_service.py:780: admin /staff/sales-agents)",
    "STAFF_SALES_AGENT_EXISTS": f"{_NOT_ON_ROUTE} (staff_service.py:792: admin POST /staff/sales-agents)",
    # Staff routes the bot reaches, but never with the input or timing these need.
    "BOTTLE_IDEMPOTENCY_KEY_INVALID": f"{_BOT_NEVER_SENDS}: the retry token is uuid4().hex, always inside the "
    f"pattern (bottle_tracking_service.py:285, bottle_collection.py _new_intent_token){_FALLBACK}",
    "BOTTLE_SCOPE_INVALID": f"{_BOT_NEVER_SENDS}: a pre-CHECK-constraint legacy balance row, not a driver action "
    f"(bottle_tracking_service.py:2027){_FALLBACK}",
    "BOTTLE_SCOPE_LOCK_NOT_HELD": f"{_BOT_NEVER_SENDS}: a lock-coverage probe that only a backend programming error "
    f"trips (bottle_tracking_service.py:563){_FALLBACK}",
    "BOTTLE_SESSION_JOIN_OWN": f"{_BOT_NEVER_SENDS}: /bottles/sessions/joinable excludes the caller's sessions and "
    f"available-drivers excludes the owner (bottle_tracking_service.py:3501/3631){_FALLBACK}",
    "INVITE_MEMBER_REQUIRED": f"{_BOT_NEVER_SENDS}: the invite always carries the picked driver's id from the button "
    f"(staff.py:1514, bottle_session.py execute_invite_driver){_FALLBACK}",
    "STAFF_COLLECTION_AMOUNT_REQUIRED": f"{_BOT_NEVER_SENDS}: cash_collection.py posts only after checking the "
    f"customer and amount (staff.py:685){_FALLBACK}",
    "STAFF_CUSTOMER_ID_REQUIRED": f"{_BOT_NEVER_SENDS}: cash_collection.py posts only after checking the customer "
    f"and amount (staff.py:683){_FALLBACK}",
    "STAFF_INVALID_ACCURACY": f"{_BOT_NEVER_SENDS}: horizontal_accuracy is Telegram's own 0-1500 m value, never "
    f"NaN or negative (staff_service.py:2069/2074){_FALLBACK}",
    "DELIVERY_DATE_REQUIRED": f"{_BOT_NEVER_SENDS}: re-dispatch always reschedules to today "
    f"(order_schedule_service.py:697, staff_service.py:1486){_FALLBACK}",
    "ORDER_RESCHEDULE_REASON_TOO_LONG": f"{_BOT_NEVER_SENDS}: the re-dispatch POST carries no reason "
    f"(order_schedule_service.py:612, api_client.redispatch_delivery){_FALLBACK}",
    "SALES_CONTACT_ROLE_INVALID": f"{_BOT_NEVER_SENDS}: a new outlet's contact is always role 'owner' and the bot "
    f"never calls the agent contacts POST (outlet_service.py:516, new_outlet.py){_FALLBACK}",
    "SALES_OUTLET_CLASS_INVALID": f"{_BOT_NEVER_SENDS}: the class keyboard sends only A/B/C or nothing "
    f"(outlet_service.py:488/1065, new_outlet.py){_FALLBACK}",
    "SALES_OUTLET_NAME_REQUIRED": f"{_BOT_NEVER_SENDS}: the typed name is stripped and re-asked when empty "
    f"(outlet_service.py:455, new_outlet.py){_FALLBACK}",
    "SALES_OUTLET_TYPE_INVALID": f"{_BOT_NEVER_SENDS}: the type comes from a keyboard checked against OUTLET_TYPES "
    f"(outlet_service.py:452, new_outlet.py){_FALLBACK}",
    "SALES_WINDOW_INVALID": f"{_BOT_NEVER_SENDS}: raised by the outlet PUT (outlet_service.py:1044/1056), which "
    f"the bot never calls{_FALLBACK}",
    "ORDER_NOT_FOUND": f"{_RACE_ONLY}: re-dispatch reads the delivery first (STAFF_DELIVERY_NOT_FOUND); this needs "
    f"an admin to delete the order inside the lock window (order_schedule_service.py:661){_FALLBACK}",
}


def _backend_codes() -> set[str]:
    codes = set()
    for path in (ROOT / "business_app").rglob("*.py"):
        for a, b in _CODE.findall(path.read_text(errors="ignore")):
            codes.add(a or b)
    return codes


def test_every_backend_error_code_is_classified():
    unclassified = sorted(
        _backend_codes()
        - set(BaseHandler.API_ERROR_CODE_KEY_MAP)
        - set(BESPOKE)
        - set(NOT_STAFF_FACING)
    )
    assert not unclassified, (
        "these backend error codes are neither translated for staff, handled by "
        "name, nor declared unreachable from the staff bot:\n  " + "\n  ".join(unclassified)
    )


def test_not_staff_facing_does_not_hide_a_mapped_code():
    assert not set(NOT_STAFF_FACING) & set(BaseHandler.API_ERROR_CODE_KEY_MAP)


def test_every_not_staff_facing_reason_says_why():
    """A reason names where the code is raised, or why a staff route that can raise
    it never does so for the bot — and then that staff see the backend's sentence."""
    for code, reason in NOT_STAFF_FACING.items():
        if reason.startswith(_NOT_ON_ROUTE):
            continue
        assert reason.startswith((_BOT_NEVER_SENDS, _RACE_ONLY)), f"{code}: {reason}"
        assert reason.endswith(_FALLBACK), f"{code}: {reason}"


def test_declarations_name_real_backend_codes():
    """A code renamed or deleted in the backend leaves no stale declaration behind."""
    stale = sorted((set(BESPOKE) | set(NOT_STAFF_FACING)) - _backend_codes())
    assert not stale, "declared here but raised nowhere in business_app/:\n  " + "\n  ".join(stale)
