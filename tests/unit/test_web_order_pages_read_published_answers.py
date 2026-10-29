"""The storefront's order scripts draw Pay, Cancel and the status from what the backend publishes.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.3
("Web clients"), rulings F5 and F15; docs/superpowers/specs/2026-10-27-bot-web-parity-design.md §5.3, §8.

- Pay, Skip/Cancel/Decline and the agent's Confirm are drawn on the My orders cards, in the order modal and on the
  tracking page by ONE script, `order-actions.js`, from the order's `customer_actions` (the backend's Pay rule and
  Cancel rule, published). No page script holds a rule of its own (`customer_cancel_via`, `can_customer_cancel`,
  a list of cancellable statuses), and none opens a native dialog.
- `orders.js` and `order-detail-modal.js` labelled `order.status`, which a failed delivery never changes. So an
  order waiting for a new date read "Out for delivery". They label the published `display_status`.
- `order-tracking.js` built `Authorization: Bearer ` from localStorage. The web keeps its token in an httpOnly
  cookie, so the header was always "Bearer null", and Cancel on the tracking page never worked. The writes now go
  through the shared request helper.

These checks are source-level, because `business_app/static/js/` is served as-is and only `admin_ui/` has a JS
runner. That is the convention of tests/unit/test_web_checkout_uses_server_estimate.py. The scripts themselves run in
jsdom in admin_ui/src/__tests__/web/orderSurfaces.test.js, over the answers the real endpoints give
(tests/integration/test_customer_order_actions_fixtures.py), and the pages' PAGE_DATA is driven in
tests/integration/test_web_pages_show_awaiting_new_date.py.
"""

import re
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages"
ORDER_SCRIPTS = ("orders.js", "order-detail-modal.js")
SURFACE_SCRIPTS = ORDER_SCRIPTS + ("order-tracking.js", "order-actions.js")
RETIRED_FIELDS = ("customer_cancel_via", "can_customer_cancel", "is_payable", "can_pay")


def _code(name: str) -> str:
    """The script without its comments, which name the old rule and would defeat the checks."""
    src = (PAGES / name).read_text(encoding="utf-8")
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


@pytest.mark.parametrize("name", SURFACE_SCRIPTS)
def test_no_order_script_decides_what_an_order_may_do_or_opens_a_dialog(name):
    """Spec 2026-10-27 §5.3: what an order may do is the backend's published `customer_actions`. No script reads a
    retired field, keeps a list of cancellable statuses, or asks with a native dialog."""
    code = _code(name)
    for field in RETIRED_FIELDS:
        assert field not in code, f"{name} decides from {field} again"
    assert not re.search(r"subscription_id\s*\?|!\s*fromSubscription|Boolean\(order\.subscription_id\)", code), name
    assert not re.search(r"\[\s*'pending'\s*,\s*'confirmed'\s*\]", code), (
        f"{name} keeps its own list of cancellable statuses again. The backend's answer also "
        "weighs the payment, the driver and the wait for a new date."
    )
    assert not re.search(r"window\.(confirm|prompt|alert)\(|(?<![\w.])(confirm|prompt|alert)\(", code), (
        f"{name} opens a native dialog"
    )


@pytest.mark.parametrize("name", ORDER_SCRIPTS)
def test_the_pages_draw_through_the_one_order_actions_script(name):
    code = _code(name)
    assert "OrderActions.render(" in code, name
    assert "/cancel'" not in code and "retry-payment" not in code, f"{name} sends an order write itself"


def test_order_actions_echoes_what_the_screen_showed():
    """The writes carry the `expected_via` and `expected_amount` the screen was drawn from, so the backend can
    answer 409 when either moved."""
    code = _code("order-actions.js")
    assert "expected_via: cancel.via" in code
    assert "expected_amount: pay.expected_amount" in code
    assert "channel: 'web'" in code


@pytest.mark.parametrize("name", ORDER_SCRIPTS)
def test_the_status_pill_labels_the_published_display_status(name):
    code = _code(name)
    assert "status_labels || {})[order.display_status]" in code, name
    assert "(order.status)" not in code, (
        f"{name} labels or styles order.status again, which still reads 'out_for_delivery' "
        "while the order waits for a new date"
    )


@pytest.mark.parametrize("name", ORDER_SCRIPTS)
def test_the_label_survives_the_deploy_window(name):
    """Spec §9, "Rolling safety": nginx serves the new script at `git pull`, while a worker may
    still hold the old template (no `status_labels`) and the old API sends no `display_status`.
    The label degrades to the order's own status instead of throwing and blanking the list.
    Cancel gets no such fallback: hidden is the safe default, and the rule is the backend's."""
    code = _code(name)
    assert "(PAGE_DATA.i18n.status_labels || {})" in code, name
    assert re.search(r"capitalize\w+\(order\.display_status \|\| order\.status\)", code), name


def test_the_tracking_page_writes_through_the_shared_request_helper():
    """The storefront's writes go through base.js `apiRequest` (cookie credentials, the CSRF header, X-Platform)."""
    for name in ("order-tracking.js", "order-actions.js"):
        code = _code(name)
        assert "localStorage" not in code and "fetch(" not in code, (
            f"{name}: the web keeps its token in an httpOnly cookie. A hand-built request carries no CSRF "
            "header, and a Bearer header read from localStorage is 'Bearer null'."
        )
    assert "window.apiRequest('/orders/'" in _code("order-actions.js")
    assert "OrderActions.render(" in _code("order-tracking.js")


# --------------------------------------------------------------------------- the payment return pages (Task 10b)

PAYMENT_PAGE_SCRIPTS = ("payment-cancelled.js", "payment-pending.js")


@pytest.mark.parametrize("name", PAYMENT_PAGE_SCRIPTS)
def test_the_payment_return_scripts_use_the_cookie_session_and_no_native_dialog(name):
    """The web keeps its session in httpOnly cookies, so a stored token is always `Bearer null`: the pages send
    through the shared request helper, and open no native dialog."""
    code = _code(name)
    assert "localStorage" not in code and "Authorization" not in code, f"{name} builds its own bearer header again"
    assert "fetch(" not in code, f"{name} sends a request itself instead of through apiRequest / OrderActions"
    assert not re.search(r"window\.(confirm|prompt|alert)\(|(?<![\w.])(confirm|prompt|alert)\(", code), name


def test_the_cancel_page_draws_its_pay_through_the_one_order_actions_script():
    code = _code("payment-cancelled.js")
    assert "OrderActions.render(" in code
    assert "retry-payment" not in code, "the cancel page sends Pay itself"
    # The page shows the status and Pay only (spec §4.5): it hands the renderer the Pay answer alone.
    assert "customer_actions: { pay:" in code and "cancel" not in code


def test_the_payment_return_routes_publish_answers_and_decide_nothing():
    routes = (Path(__file__).resolve().parents[2] / "business_app" / "frontend" / "routes.py").read_text("utf-8")
    assert "order_is_payable_online" not in routes, "the cancel page works out Pay itself again"
    html = (
        Path(__file__).resolve().parents[2] / "business_app" / "templates" / "frontend" / "payment_cancelled.html"
    ).read_text("utf-8")
    assert "'Pending Payment'" not in html, "the cancel page names every order Pending Payment again"
    assert "order_status_label(display_status)" in html


def test_checkout_keeps_the_published_wait_and_offers_the_cash_rescue_through_order_actions():
    code = _code("checkout.js")
    assert "OrderActions.openPaymentWhenReady(data.data" in code
    assert "OrderActions.showCashRescue(" in code and "ASL_BELGISI_UNAVAILABLE" in code
    assert "onRetry: placeOrderWithClick" in code
    assert "window.location.href = data.data.payment_url" not in code, "checkout opens Click without the wait"
    assert "payment_ready_at" not in code, "checkout works the wait out from the instant instead of reading seconds"
