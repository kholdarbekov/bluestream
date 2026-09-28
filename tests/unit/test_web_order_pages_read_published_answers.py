"""The storefront's order scripts draw Cancel and the status from what the backend publishes.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.3
("Web clients"), rulings F5 and F15.

- `orders.js` (the order list and its detail) and `order-detail-modal.js` (the account
  page's modal) drew Cancel for any pending or confirmed order. The backend now owns that
  rule and publishes it as `can_customer_cancel`.
- Both scripts labelled `order.status`, which a failed delivery never changes. So an order
  waiting for a new date read "Out for delivery".
- `order-tracking.js` built `Authorization: Bearer ` from localStorage. The web keeps its
  token in an httpOnly cookie, so the header was always "Bearer null", and Cancel on the
  tracking page never worked.

These checks are source-level, because `business_app/static/js/` is served as-is and only
`admin_ui/` has a JS runner. That is the convention of
tests/unit/test_web_checkout_uses_server_estimate.py. The pages, the PAGE_DATA these
scripts read and the cancel request they send are driven in
tests/integration/test_web_pages_show_awaiting_new_date.py.
"""

import re
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages"
ORDER_SCRIPTS = ("orders.js", "order-detail-modal.js")


def _code(name: str) -> str:
    """The script without its comments, which name the old rule and would defeat the checks."""
    src = (PAGES / name).read_text(encoding="utf-8")
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


@pytest.mark.parametrize("name", ORDER_SCRIPTS)
def test_cancel_is_drawn_from_the_published_answer(name):
    code = _code(name)
    assert "order.can_customer_cancel" in code, f"{name} no longer draws Cancel from can_customer_cancel"
    assert not re.search(r"\[\s*'pending'\s*,\s*'confirmed'\s*\]", code), (
        f"{name} keeps its own list of cancellable statuses again. The backend's answer also "
        "weighs the payment, the driver and the wait for a new date."
    )


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


def test_the_tracking_page_cancels_through_the_shared_request_helper():
    code = _code("order-tracking.js")
    assert "apiRequest('/orders/' + PAGE_DATA.order_id + '/cancel'" in code
    assert "localStorage" not in code and "fetch(" not in code, (
        "the web keeps its token in an httpOnly cookie. A hand-built request carries no CSRF "
        "header, and a Bearer header read from localStorage is 'Bearer null'."
    )
